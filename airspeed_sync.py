#!/usr/bin/env python3
"""
Airspeed Sync — Step 3 of Dobby's automated meeting management.

Pulls recently-processed Airspeed (Glyphic) calls and joins each to its Step 1
Notion meeting record, writing the summary, recording link, action items, and
flipping Status to Completed. Airspeed exposes no Google Calendar event ID, so
the join is deterministic on start-time proximity + shared participants/domain.

A call that matches no existing record is created as a new record (orphan),
attributed to a customer by domain where possible.

Runs hourly in GitHub Actions. Idempotent on the Airspeed Call ID.
"""

import json
import os
import sys
import time
import re
import datetime as dt

import requests

# ----------------------------------------------------------------- CONFIG ---

GLYPHIC_BASE = "https://api.glyphic.ai/v1"

MEETINGS_DB_ID = "280db974-b757-8050-b523-c091e4c3ffd3"    # Meeting Notes DB
CUSTOMERS_DB_ID = "280db974-b757-80f3-a1a0-db38f8c584d4"   # Customer Database

INTERNAL_DOMAINS = {"dobby.io"}
LOOKBACK_DAYS = 3               # how far back to scan Airspeed for new calls
MATCH_TOLERANCE_MIN = 30        # start-time window for matching to a record
MAX_LIST_PAGES = 5              # safety cap on Airspeed list pagination
NOTION_VERSION = "2022-06-28"

# Exact Notion property names.
P_TITLE = "Meeting name"
P_DATE = "Date & Time"
P_PARTICIPANTS = "Participants"
P_CAL_EVENT_ID = "Calendar Event ID"
P_AIRSPEED_ID = "Airspeed Call ID"
P_STATUS = "Status"
P_CUSTOMER = "Customer"
P_AREA = "Area"
P_CATEGORY = "Category"
P_SUMMARY = "Summary"
P_RECORDING = "Recording"
P_CUST_NAME = "Name"
P_CUST_EMAIL = "Email"

CUSTOMER_AREA = "Customer Success"
CUSTOMER_CATEGORY = ["Customer call"]

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
GLYPHIC_API_KEY = os.environ.get("GLYPHIC_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# --------------------------------------------------------------- LOGGING ----

_log_lines = []


def log(msg):
    line = f"[{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {msg}"
    print(line, flush=True)
    _log_lines.append(line)


def slack_notify(text):
    if not SLACK_WEBHOOK_URL:
        return
    try:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=15)
    except Exception as e:
        print(f"slack notify failed: {e}", flush=True)


# ---------------------------------------------------------------- GLYPHIC ---

def glyphic_get(path, params=None):
    url = f"{GLYPHIC_BASE}{path}"
    headers = {"X-API-Key": GLYPHIC_API_KEY}
    for attempt in range(5):
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 1 + attempt)))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def list_recent_call_ids(cutoff):
    """Page through /calls/, returning IDs of calls plausibly within the
    lookback window. Uses preview start_time to pre-filter when present;
    otherwise the per-call detail fetch filters precisely later."""
    ids = []
    cursor = None
    for _ in range(MAX_LIST_PAGES):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = glyphic_get("/calls/", params)
        rows = data.get("data", [])
        for row in rows:
            cid = row.get("id")
            if not cid:
                continue
            prev_start = row.get("start_time")
            if prev_start:
                try:
                    if parse_dt(prev_start) < cutoff:
                        continue  # too old, skip without fetching detail
                except Exception:
                    pass
            ids.append(cid)
        cursor = (data.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    return ids


# ------------------------------------------------------------------ NOTION ---

def notion_request(method, url, **kwargs):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    for attempt in range(5):
        r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 1 + attempt)))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def parse_domains(text):
    out = set()
    for part in re.split(r"[;,\s]+", text or ""):
        d = part.strip().lstrip("@").lower()
        if d and "." in d:
            out.add(d)
    return out


def load_customer_domain_map():
    domain_map = {}
    url = f"https://api.notion.com/v1/databases/{CUSTOMERS_DB_ID}/query"
    payload = {"page_size": 100}
    while True:
        data = notion_request("POST", url, data=json.dumps(payload))
        for page in data.get("results", []):
            props = page.get("properties", {})
            email_text = "".join(
                t.get("plain_text", "")
                for t in props.get(P_CUST_EMAIL, {}).get("rich_text", [])
            )
            for domain in parse_domains(email_text):
                domain_map.setdefault(domain, page["id"])
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return domain_map


def already_ingested(call_id):
    url = f"https://api.notion.com/v1/databases/{MEETINGS_DB_ID}/query"
    payload = {"filter": {"property": P_AIRSPEED_ID, "rich_text": {"equals": call_id}},
               "page_size": 1}
    data = notion_request("POST", url, data=json.dumps(payload))
    return bool(data.get("results"))


def candidate_records(start):
    lower = (start - dt.timedelta(minutes=MATCH_TOLERANCE_MIN)).isoformat()
    upper = (start + dt.timedelta(minutes=MATCH_TOLERANCE_MIN)).isoformat()
    url = f"https://api.notion.com/v1/databases/{MEETINGS_DB_ID}/query"
    payload = {
        "filter": {"and": [
            {"property": P_DATE, "date": {"on_or_after": lower}},
            {"property": P_DATE, "date": {"on_or_before": upper}},
        ]},
        "page_size": 100,
    }
    data = notion_request("POST", url, data=json.dumps(payload))
    return data.get("results", [])


def record_participant_emails(page):
    text = "".join(
        t.get("plain_text", "")
        for t in page.get("properties", {}).get(P_PARTICIPANTS, {}).get("rich_text", [])
    )
    return {e.strip().lower() for e in re.split(r"[;,\s]+", text) if "@" in e}


# ------------------------------------------------------------------ HELPERS --

def parse_dt(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _rt(value):
    return [{"type": "text", "text": {"content": (value or "")[:2000]}}] if value else []


def _chunks(text, size=1900):
    """Notion caps a single rich-text run at 2000 chars; split long text."""
    text = text or ""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _heading(text):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def _bullet(text):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def _paragraph(text):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def build_body_children(call):
    """The blocks appended to the meeting body:
        ## Action points
        - <next step>
        ## Summary
        <summary>
    """
    children = [_heading("Action points")]
    steps = [i.get("value", "") for i in call.get("insights", [])
             if i.get("name") == "next_steps" and i.get("value")]
    if steps:
        children += [_bullet(s) for s in steps]
    else:
        children.append(_paragraph("No action points captured."))

    children.append(_heading("Summary"))
    children += [_paragraph(c) for c in _chunks(call.get("summary"))]
    return children


def append_body(page_id, children):
    notion_request("PATCH", f"https://api.notion.com/v1/blocks/{page_id}/children",
                   data=json.dumps({"children": children}))


def call_emails_and_domains(call):
    emails = {p["email"].lower() for p in call.get("participants", []) if p.get("email")}
    domains = {e.split("@")[-1] for e in emails}
    for c in call.get("companies", []):
        if c.get("domain"):
            domains.add(c["domain"].lower())
    return emails, domains


# --------------------------------------------------------------- CORE LOOP ---

def completion_props(call):
    """Properties written on both match and orphan. Summary stays in the
    property too (the AI foundation queries it directly); the body carries the
    human-readable copy. Action points live only in the body."""
    props = {
        P_SUMMARY: {"rich_text": _rt(call.get("summary"))},
        P_STATUS: {"select": {"name": "Completed"}},
        P_AIRSPEED_ID: {"rich_text": _rt(call["id"])},
    }
    if call.get("url_link"):
        props[P_RECORDING] = {"url": call["url_link"]}
    return props


def update_record(page, call):
    # Set properties first — this claims the call via Airspeed Call ID, so a
    # retry after a mid-failure won't re-append duplicate body blocks — then
    # append the Action points / Summary blocks to the body.
    notion_request("PATCH", f"https://api.notion.com/v1/pages/{page['id']}",
                   data=json.dumps({"properties": completion_props(call)}))
    append_body(page["id"], build_body_children(call))


def create_orphan(call, domain_map):
    emails, domains = call_emails_and_domains(call)
    external = domains - INTERNAL_DOMAINS
    matched = {domain_map[d] for d in external if d in domain_map}
    customer_id = next(iter(matched)) if len(matched) == 1 else None

    start = parse_dt(call["start_time"])
    end = start + dt.timedelta(seconds=call.get("duration") or 0)

    props = completion_props(call)
    props[P_TITLE] = {"title": [{"type": "text",
                                 "text": {"content": (call.get("title") or "(no title)")[:2000]}}]}
    props[P_DATE] = {"date": {"start": start.isoformat(), "end": end.isoformat()}}
    props[P_PARTICIPANTS] = {"rich_text": _rt("; ".join(sorted(emails)))}
    if customer_id:
        props[P_CUSTOMER] = {"relation": [{"id": customer_id}]}
        props[P_AREA] = {"select": {"name": CUSTOMER_AREA}}
        props[P_CATEGORY] = {"multi_select": [{"name": c} for c in CUSTOMER_CATEGORY]}
    notion_request("POST", "https://api.notion.com/v1/pages",
                   data=json.dumps({"parent": {"database_id": MEETINGS_DB_ID},
                                    "properties": props,
                                    "children": build_body_children(call)}))


def best_match(call, candidates):
    """Score candidates by shared participants/domain; require a positive
    signal so we never mis-file onto a same-slot but unrelated meeting."""
    call_emails, call_domains = call_emails_and_domains(call)
    best, best_score = None, 0
    for page in candidates:
        rec_emails = record_participant_emails(page)
        rec_domains = {e.split("@")[-1] for e in rec_emails}
        score = 10 * len(call_emails & rec_emails) + len(call_domains & rec_domains)
        if score > best_score:
            best, best_score = page, score
    return best if best_score > 0 else None


def handle_call(call, domain_map, summary):
    call_id = call["id"]
    status = call.get("status", {})
    status_code = status.get("code") if isinstance(status, dict) else status
    if status_code != "completed":
        summary["skipped"] += 1
        return
    if not call.get("start_time"):
        summary["skipped"] += 1
        return

    candidates = candidate_records(parse_dt(call["start_time"]))
    match = best_match(call, candidates)
    if match:
        update_record(match, call)
        summary["matched"] += 1
    else:
        create_orphan(call, domain_map)
        summary["orphans"] += 1


def main():
    if not NOTION_TOKEN or not GLYPHIC_API_KEY:
        log("FATAL: NOTION_TOKEN and GLYPHIC_API_KEY must both be set")
        sys.exit(1)

    summary = {"matched": 0, "orphans": 0, "skipped": 0, "already": 0, "errors": 0}
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS)

    domain_map = load_customer_domain_map()
    call_ids = list_recent_call_ids(cutoff)
    log(f"found {len(call_ids)} recent Airspeed calls to consider")

    for call_id in call_ids:
        try:
            if already_ingested(call_id):
                summary["already"] += 1
                continue
            call = glyphic_get(f"/calls/{call_id}")
            if call.get("start_time") and parse_dt(call["start_time"]) < cutoff:
                summary["skipped"] += 1
                continue
            handle_call(call, domain_map, summary)
        except Exception as e:
            log(f"ERROR on call {call_id}: {e}")
            summary["errors"] += 1

    log(f"done: {summary}")
    if summary["errors"]:
        slack_notify(
            f":warning: Airspeed Sync finished with {summary['errors']} error(s).\n"
            f"{summary}\n```\n" + "\n".join(_log_lines[-15:]) + "\n```"
        )
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        slack_notify(
            f":rotating_light: Airspeed Sync crashed: {e}\n```\n"
            + "\n".join(_log_lines[-15:]) + "\n```"
        )
        sys.exit(1)
