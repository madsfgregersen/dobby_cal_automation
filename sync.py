#!/usr/bin/env python3
"""
Meeting Sync — Step 1 of Dobby's automated meeting management.

Reads every team member's Google Calendar a week ahead and creates/updates a
matching record in the Notion "Meeting Notes DB", stamped with the Google
Calendar event ID as the join key. It touches only machine-owned fields;
human prep (the page body, Attendees, Area, Category, Project) is never altered.

Auth is keyless: Workload Identity Federation gets us the service account,
domain-wide delegation lets that account read each user's calendar. No key files.

Runs hourly in GitHub Actions. Safe to run repeatedly — it's idempotent on the
Calendar Event ID.
"""

import json
import os
import sys
import time
import datetime as dt
import re
import urllib.parse

import requests
import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest

# ----------------------------------------------------------------- CONFIG ---

SERVICE_ACCOUNT_EMAIL = "meeting-sync@dobby-workspace-automations.iam.gserviceaccount.com"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

MEETINGS_DB_ID = "280db974-b757-8050-b523-c091e4c3ffd3"    # Meeting Notes DB
CUSTOMERS_DB_ID = "280db974-b757-80f3-a1a0-db38f8c584d4"   # Customer Database

# Attendees on these domains do NOT count as "external". An event with only
# internal attendees (standups, 1:1s) is skipped.
INTERNAL_DOMAINS = {"dobby.io"}

WINDOW_DAYS = 7          # how far ahead to sync
NOTION_VERSION = "2022-06-28"

# Team calendars to read. Managed via the CALENDAR_USERS GitHub Actions
# *variable* (comma-separated emails) so the list can change without a code
# edit. The list below is only a fallback.
DEFAULT_CALENDAR_USERS = [
    "mg@dobby.io",
    "dm@dobby.io",
]

# Exact Notion property names (must match the DB precisely).
P_TITLE = "Meeting name"
P_DATE = "Date & Time"
P_PARTICIPANTS = "Participants"
P_EVENT_ID = "Calendar Event ID"
P_STATUS = "Status"
P_CUSTOMER = "Customer"
P_CUST_NAME = "Name"
P_CUST_EMAIL = "Email"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
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
    except Exception as e:  # never let notification failure mask the real error
        print(f"slack notify failed: {e}", flush=True)


# ---------------------------------------------------- GOOGLE AUTH (KEYLESS) --

def google_adc_token():
    """Access token for the service account itself, via WIF/ADC."""
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(GoogleAuthRequest())
    return creds.token


def delegated_user_token(adc_token, user_email):
    """
    Mint an access token that acts *as user_email* (domain-wide delegation),
    without a key file:
      1. build a JWT claim set impersonating the user,
      2. have the service account sign it (IAM Credentials signJwt),
      3. exchange the signed JWT for a user-scoped access token.
    """
    now = int(time.time())
    claims = {
        "iss": SERVICE_ACCOUNT_EMAIL,
        "sub": user_email,
        "scope": CALENDAR_SCOPE,
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    sign_url = (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{SERVICE_ACCOUNT_EMAIL}:signJwt"
    )
    r = requests.post(
        sign_url,
        headers={"Authorization": f"Bearer {adc_token}"},
        json={"payload": json.dumps(claims)},
        timeout=30,
    )
    r.raise_for_status()
    signed_jwt = r.json()["signedJwt"]

    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed_jwt,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


# --------------------------------------------------------------- CALENDAR ----

def window_bounds():
    now = dt.datetime.now(dt.timezone.utc)
    return now.isoformat(), (now + dt.timedelta(days=WINDOW_DAYS)).isoformat()


def list_events(user_email, user_token, time_min, time_max):
    base = (
        "https://www.googleapis.com/calendar/v3/calendars/"
        f"{urllib.parse.quote(user_email)}/events"
    )
    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",      # expand recurring into per-occurrence events
        "orderBy": "startTime",
        "maxResults": 250,
        "showDeleted": "true",       # so cancelled occurrences can be marked
    }
    events = []
    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(
            base,
            headers={"Authorization": f"Bearer {user_token}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        events.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return events


# ------------------------------------------------------------------ NOTION ---

def notion_request(method, url, **kwargs):
    """Notion call with basic retry on rate-limit / transient errors."""
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    for attempt in range(5):
        r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        if r.status_code == 429 or r.status_code >= 500:
            wait = float(r.headers.get("Retry-After", 1 + attempt))
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def parse_domains(text):
    """'@equiserve.co.za; @branduka.co.za' -> {'equiserve.co.za','branduka.co.za'}"""
    out = set()
    for part in re.split(r"[;,\s]+", text or ""):
        d = part.strip().lstrip("@").lower()
        if d and "." in d:
            out.add(d)
    return out


def load_customer_domain_map():
    """Build {domain: {'id': page_id, 'name': customer}} from the Customer DB."""
    domain_map = {}
    url = f"https://api.notion.com/v1/databases/{CUSTOMERS_DB_ID}/query"
    payload = {"page_size": 100}
    while True:
        data = notion_request("POST", url, data=json.dumps(payload))
        for page in data.get("results", []):
            props = page.get("properties", {})
            name = "".join(
                t.get("plain_text", "")
                for t in props.get(P_CUST_NAME, {}).get("title", [])
            )
            email_text = "".join(
                t.get("plain_text", "")
                for t in props.get(P_CUST_EMAIL, {}).get("rich_text", [])
            )
            for domain in parse_domains(email_text):
                # first writer wins; warn on genuine collisions between customers
                if domain in domain_map and domain_map[domain]["id"] != page["id"]:
                    log(f"WARNING: domain {domain} maps to multiple customers "
                        f"({domain_map[domain]['name']} & {name})")
                    continue
                domain_map[domain] = {"id": page["id"], "name": name}
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    log(f"loaded {len(domain_map)} customer domains")
    return domain_map


def find_meeting_by_event_id(event_id):
    url = f"https://api.notion.com/v1/databases/{MEETINGS_DB_ID}/query"
    payload = {
        "filter": {"property": P_EVENT_ID, "rich_text": {"equals": event_id}},
        "page_size": 1,
    }
    data = notion_request("POST", url, data=json.dumps(payload))
    results = data.get("results", [])
    return results[0] if results else None


def _rich_text(value):
    return [{"type": "text", "text": {"content": value[:2000]}}] if value else []


def create_meeting(event_id, title, start, end, participants, customer_id):
    props = {
        P_TITLE: {"title": [{"type": "text", "text": {"content": title[:2000]}}]},
        P_DATE: {"date": {"start": start, "end": end}},
        P_PARTICIPANTS: {"rich_text": _rich_text(participants)},
        P_EVENT_ID: {"rich_text": _rich_text(event_id)},
        P_STATUS: {"select": {"name": "Planned"}},
    }
    if customer_id:
        props[P_CUSTOMER] = {"relation": [{"id": customer_id}]}
    notion_request(
        "POST",
        "https://api.notion.com/v1/pages",
        data=json.dumps({"parent": {"database_id": MEETINGS_DB_ID}, "properties": props}),
    )


def update_meeting(page, title, start, end, participants, customer_id):
    """Update machine-owned fields only. Never touch Status. Fill Customer
    only when it's currently empty (protects any human correction)."""
    props = {
        P_TITLE: {"title": [{"type": "text", "text": {"content": title[:2000]}}]},
        P_DATE: {"date": {"start": start, "end": end}},
        P_PARTICIPANTS: {"rich_text": _rich_text(participants)},
    }
    existing_customer = page.get("properties", {}).get(P_CUSTOMER, {}).get("relation", [])
    if customer_id and not existing_customer:
        props[P_CUSTOMER] = {"relation": [{"id": customer_id}]}
    notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page['id']}",
        data=json.dumps({"properties": props}),
    )


def mark_cancelled(page):
    notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page['id']}",
        data=json.dumps({"properties": {P_STATUS: {"select": {"name": "Cancelled"}}}}),
    )


# --------------------------------------------------------------- CORE LOOP ---

def handle_event(ev, domain_map, summary):
    event_id = ev.get("id")
    if not event_id:
        summary["skipped"] += 1
        return

    existing = find_meeting_by_event_id(event_id)

    # Cancellations: mark, never delete. Skip if we never had a record.
    if ev.get("status") == "cancelled":
        if existing:
            mark_cancelled(existing)
            summary["cancelled"] += 1
        else:
            summary["skipped"] += 1
        return

    attendees = [a for a in ev.get("attendees", []) if not a.get("resource")]
    emails = [a["email"] for a in attendees if a.get("email")]
    domains = {e.split("@")[-1].lower() for e in emails if "@" in e}
    external = domains - INTERNAL_DOMAINS

    # All meetings are processed, internal ones included. External domains are
    # only used to match a customer; an internal-only meeting simply gets no
    # customer and a blank Customer field.
    matched = {domain_map[d]["id"] for d in external if d in domain_map}
    customer_id = next(iter(matched)) if len(matched) == 1 else None
    if len(matched) > 1:
        log(f"event {event_id}: matched multiple customers, leaving Customer blank")

    title = ev.get("summary") or "(no title)"
    start = ev["start"].get("dateTime") or ev["start"].get("date")
    end = ev["end"].get("dateTime") or ev["end"].get("date")
    participants = "; ".join(sorted(emails))

    if existing:
        update_meeting(existing, title, start, end, participants, customer_id)
        summary["updated"] += 1
    else:
        create_meeting(event_id, title, start, end, participants, customer_id)
        summary["created"] += 1


def get_calendar_users():
    raw = os.environ.get("CALENDAR_USERS", "")
    users = [u.strip() for u in raw.split(",") if u.strip()]
    return users or DEFAULT_CALENDAR_USERS


def main():
    if not NOTION_TOKEN:
        log("FATAL: NOTION_TOKEN is not set")
        sys.exit(1)

    summary = {"created": 0, "updated": 0, "cancelled": 0, "skipped": 0, "errors": 0}

    adc = google_adc_token()
    domain_map = load_customer_domain_map()
    users = get_calendar_users()
    time_min, time_max = window_bounds()
    log(f"syncing {len(users)} calendars, window {time_min} .. {time_max}")

    # Collect across all users, deduped by event id (invited events share an id
    # across attendees' calendars, so this avoids redundant writes).
    events_by_id = {}
    for user in users:
        try:
            token = delegated_user_token(adc, user)
            for ev in list_events(user, token, time_min, time_max):
                eid = ev.get("id")
                if not eid:
                    continue
                # Prefer a copy that actually carries attendees.
                if eid not in events_by_id or (
                    ev.get("attendees") and not events_by_id[eid].get("attendees")
                ):
                    events_by_id[eid] = ev
        except Exception as e:
            log(f"ERROR reading calendar for {user}: {e}")
            summary["errors"] += 1

    for ev in events_by_id.values():
        try:
            handle_event(ev, domain_map, summary)
        except Exception as e:
            log(f"ERROR on event {ev.get('id')}: {e}")
            summary["errors"] += 1

    log(f"done: {summary}")

    if summary["errors"]:
        slack_notify(
            f":warning: Meeting Sync finished with {summary['errors']} error(s).\n"
            f"{summary}\n```\n" + "\n".join(_log_lines[-15:]) + "\n```"
        )
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        slack_notify(
            f":rotating_light: Meeting Sync crashed: {e}\n```\n"
            + "\n".join(_log_lines[-15:]) + "\n```"
        )
        sys.exit(1)
