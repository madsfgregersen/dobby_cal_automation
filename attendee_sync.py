#!/usr/bin/env python3
"""
Attendee Sync — adds the meeting recorder (support@dobby.io) to team meetings.

For every user in CALENDAR_USERS, this looks a week ahead at the meetings they
*organize*, and — for real meetings with a video link — silently adds
support@dobby.io as an attendee so the recorder joins automatically. It patches
the organizer's own copy of the event (the only copy that can add a guest) and
sends no notification e-mail (`sendUpdates=none`).

Design mirrors sync.py: keyless auth (Workload Identity Federation + domain-wide
delegation, no key files), a windowed read of each calendar, and no database.
It's idempotent — a meeting that already has support@dobby.io is skipped — so
it's safe to run on a schedule and safe to re-run.

DRY-RUN by default: it logs every meeting it *would* patch without changing
anything. Set DRY_RUN=false to enable live patches.

Runs on a schedule in GitHub Actions.
"""

import json
import os
import sys
import time
import datetime as dt
import urllib.parse

import requests
import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest

import directory_users

# ----------------------------------------------------------------- CONFIG ---

SERVICE_ACCOUNT_EMAIL = "meeting-sync@dobby-workspace-automations.iam.gserviceaccount.com"
# Write scope — required to patch events. Must be authorized on the service
# account's domain-wide delegation in Google Workspace Admin, alongside the
# read-only scope the notes sync uses.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"

# The recorder identity added to meetings. Overridable via env for flexibility.
RECORDER_EMAIL = os.environ.get("RECORDER_EMAIL", "support@dobby.io").lower()

WINDOW_DAYS = 7          # how far ahead to look
MIN_PARTICIPANTS = 2     # skip solo blocks / personal holds

# Team calendars to process. Defaults to the same CALENDAR_USERS the notes sync
# uses; ATTENDEE_SYNC_USERS can override it (handy for a pilot subset) without
# touching the shared CALENDAR_USERS list.
DEFAULT_CALENDAR_USERS = [
    "mg@dobby.io",
    "dm@dobby.io",
]

# Safety switch. DRY-RUN logs what it would do and writes nothing. Anything
# other than an explicit false/0/no keeps it in dry-run.
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() not in ("false", "0", "no")

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

def _g_request(method, url, token, params=None, json_body=None):
    """Google API call with a light retry on rate-limit / transient errors."""
    headers = {"Authorization": f"Bearer {token}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(5):
        r = requests.request(
            method, url, headers=headers, params=params,
            data=json.dumps(json_body) if json_body is not None else None,
            timeout=30,
        )
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", 1 + attempt)))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def window_bounds():
    now = dt.datetime.now(dt.timezone.utc)
    return now.isoformat(), (now + dt.timedelta(days=WINDOW_DAYS)).isoformat()


def list_events(user_email, token, time_min, time_max):
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
    }
    events = []
    page_token = None
    while True:
        if page_token:
            params["pageToken"] = page_token
        data = _g_request("GET", base, token, params=params)
        events.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return events


def get_event(calendar_id, token, event_id):
    url = (
        "https://www.googleapis.com/calendar/v3/calendars/"
        f"{urllib.parse.quote(calendar_id)}/events/{urllib.parse.quote(event_id)}"
    )
    return _g_request("GET", url, token)


def patch_add_recorder(calendar_id, token, event_id, existing_attendees):
    """Add the recorder to the event's attendee list. A patch replaces the whole
    attendees array, so we resend the existing attendees and append the recorder.
    sendUpdates=none keeps it silent — no e-mail to the organizer or guests."""
    url = (
        "https://www.googleapis.com/calendar/v3/calendars/"
        f"{urllib.parse.quote(calendar_id)}/events/{urllib.parse.quote(event_id)}"
    )
    body = {"attendees": existing_attendees + [{"email": RECORDER_EMAIL}]}
    return _g_request("PATCH", url, token, params={"sendUpdates": "none"}, json_body=body)


# --------------------------------------------------------------- CORE LOOP ---

def _attendee_emails(event):
    """Real invited people (drop resources like rooms)."""
    return [
        a.get("email", "").lower()
        for a in event.get("attendees", [])
        if a.get("email") and not a.get("resource")
    ]


def qualifies(ev, user_email):
    """A meeting we should add the recorder to:
      * organized by this user (only the organizer's copy can add a guest),
      * not cancelled,
      * a real meeting with a video link (skips personal blocks / OOO / focus),
      * at least MIN_PARTICIPANTS people,
      * the recorder isn't already on it."""
    if ev.get("status") == "cancelled":
        return False
    if ev.get("visibility") in ("private", "confidential"):
        return False        # respect events the organizer marked Private
    organizer = ev.get("organizer") or {}
    if not (organizer.get("self") or organizer.get("email", "").lower() == user_email.lower()):
        return False
    if not (ev.get("hangoutLink") or ev.get("conferenceData")):
        return False
    emails = _attendee_emails(ev)
    if len(emails) < MIN_PARTICIPANTS:
        return False
    if RECORDER_EMAIL in emails:
        return False
    return True


def get_target_users(adc_token):
    """USER_SOURCE=directory discovers all active users live (whole company);
    otherwise use the manual ATTENDEE_SYNC_USERS / CALENDAR_USERS list. If
    discovery fails (e.g. the Directory scope hasn't propagated), fall back to
    the manual list with a warning rather than taking the whole run down."""
    raw = os.environ.get("ATTENDEE_SYNC_USERS") or os.environ.get("CALENDAR_USERS", "")
    manual = [u.strip() for u in raw.split(",") if u.strip()] or DEFAULT_CALENDAR_USERS
    if os.environ.get("USER_SOURCE", "list").strip().lower() == "directory":
        try:
            users = directory_users.list_active_users(adc_token)
            log(f"discovered {len(users)} active users via Directory API")
            return users
        except Exception as e:
            log(f"WARNING: Directory discovery failed ({e}); "
                f"falling back to manual list of {len(manual)} users")
            return manual
    return manual


def main():
    adc = google_adc_token()
    users = get_target_users(adc)
    time_min, time_max = window_bounds()
    log(f"attendee sync {'(DRY-RUN)' if DRY_RUN else '(LIVE)'} — "
        f"{len(users)} calendars, window {time_min} .. {time_max}, recorder {RECORDER_EMAIL}")

    summary = {"would_patch": 0, "patched": 0, "already": 0, "skipped": 0, "errors": 0}

    # --- Discover: which events/series need the recorder. For a recurring
    # occurrence we target the series master (recurringEventId) so one patch
    # covers the whole series; each target is handled once. ---
    tokens = {}
    targets = {}   # target_event_id -> {user, title, start}
    for user in users:
        try:
            tokens[user] = delegated_user_token(adc, user)
            for ev in list_events(user, tokens[user], time_min, time_max):
                if not qualifies(ev, user):
                    continue
                target_id = ev.get("recurringEventId") or ev.get("id")
                targets.setdefault(target_id, {
                    "user": user,
                    "title": ev.get("summary") or "(no title)",
                    "start": ev["start"].get("dateTime") or ev["start"].get("date"),
                })
        except Exception as e:
            log(f"ERROR reading calendar for {user}: {e}")
            summary["errors"] += 1

    # --- Patch each target authoritatively (re-fetch the event so we append to
    # its real, current attendee list). ---
    for target_id, meta in targets.items():
        user = meta["user"]
        try:
            token = tokens.get(user) or delegated_user_token(adc, user)
            full = get_event(user, token, target_id)
            if full.get("status") == "cancelled":
                summary["skipped"] += 1
                continue
            attendees = full.get("attendees", []) or []
            if RECORDER_EMAIL in [a.get("email", "").lower() for a in attendees]:
                summary["already"] += 1
                continue
            where = f"'{meta['title']}' @ {meta['start']} (organizer {user}, event {target_id})"
            if DRY_RUN:
                log(f"[DRY-RUN] would add {RECORDER_EMAIL} to {where}")
                summary["would_patch"] += 1
                continue
            patch_add_recorder(user, token, target_id, attendees)
            log(f"added {RECORDER_EMAIL} to {where}")
            summary["patched"] += 1
        except Exception as e:
            log(f"ERROR patching event {target_id} (organizer {user}): {e}")
            summary["errors"] += 1

    log(f"done: {summary}")

    if summary["errors"]:
        slack_notify(
            f":warning: Attendee Sync finished with {summary['errors']} error(s).\n"
            f"{summary}\n```\n" + "\n".join(_log_lines[-15:]) + "\n```"
        )
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        slack_notify(
            f":rotating_light: Attendee Sync crashed: {e}\n```\n"
            + "\n".join(_log_lines[-15:]) + "\n```"
        )
        sys.exit(1)
