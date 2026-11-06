#!/usr/bin/env python3
"""
Notetaker Dispatch — sends the Airspeed notetaker to meetings that OTHER
companies organize.

The notetaker joins automatically only when the recorder (support@dobby.io) is
on the invite. attendee_sync.py adds it to meetings Dobby people organize, but a
guest can't add people to someone else's meeting. So when an external company
invites a Dobby user to its Teams / Zoom / Meet call, nothing tells Airspeed
about it. This script closes that gap: it watches every Dobby calendar and,
just before such a meeting starts, asks Airspeed to send the notetaker to the
meeting link directly (POST /v1/call_bots).

A meeting is dispatched when, on a Dobby user's calendar, it:
  * is organized OUTSIDE Dobby (Dobby-organized meetings are attendee_sync's job),
  * has been accepted (or tentatively accepted) by that user,
  * has a Teams / Zoom / Meet link, is not cancelled and not marked private,
  * has at least two people and doesn't already include the recorder,
  * doesn't contain the opt-out tag [no-notetaker] in its title or description,
  * starts between JOIN_EARLY_MIN before and JOIN_LATE_MIN after now.

Each meeting link is sent once, however many colleagues are invited: the
calendar copies are marked with a private extended property (visible only on
that person's own copy), so later polls and later runs skip it. The invite's
attendee list is passed along, so the recording carries real e-mail addresses
even when guests join Teams without one — which also helps airspeed_sync.py
file the summary on the right Notion page.

DRY-RUN by default: logs what it would send and changes nothing. Set DRY_RUN to
false to go live. Loops (polling every POLL_SECONDS) until RUN_SECONDS elapse,
so one run covers the whole Cloud Scheduler interval.

Logs are public (public repo), so meeting links (which carry passcodes) and
external e-mail addresses are never logged.
"""

import html
import os
import re
import sys
import time
import datetime as dt
import urllib.parse
from zoneinfo import ZoneInfo

import requests

import attendee_sync as gcal        # keyless Google auth + calendar helpers
import directory_users

# ----------------------------------------------------------------- CONFIG ---

GLYPHIC_BASE = "https://api.glyphic.ai/v1"
GLYPHIC_API_KEY = os.environ.get("GLYPHIC_API_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

RECORDER_EMAIL = (os.environ.get("RECORDER_EMAIL") or "support@dobby.io").lower()
INTERNAL_DOMAINS = {"dobby.io"}
OPT_OUT_TAG = "[no-notetaker]"
MARKER_KEY = "dobbyNotetakerSentAt"     # private extended property on each copy

JOIN_EARLY_MIN = 2       # send the notetaker up to 2 min before the start
JOIN_LATE_MIN = 10       # ...and still send it up to 10 min after the start
MIN_PARTICIPANTS = 2
POLL_SECONDS = 60
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "1680"))   # ~28 min, < the 30-min trigger

LOCAL_TZ = "Europe/Copenhagen"
ACTIVE_START_HOUR = 6    # inclusive, local time
ACTIVE_END_HOUR = 22     # exclusive, local time

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() not in ("false", "0", "no")

# Meeting links we can hand to Airspeed. Teams links other than the join links
# (e.g. "meetingOptions" in the invite footer) are deliberately not matched.
MEETING_URL_RE = re.compile(
    r"https://(?:"
    r"teams\.microsoft\.com/(?:l/meetup-join|meet)/"
    r"|teams\.live\.com/meet/"
    r"|[\w.-]*zoom\.us/(?:j|my|w)/"
    r"|meet\.google\.com/[a-z]{3}-"
    r")[^\s<>\"')\]]+",
    re.IGNORECASE,
)

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


# ---------------------------------------------------------------- EVENTS ----

def _domain(email):
    return (email or "").lower().rsplit("@", 1)[-1]


def meeting_url(ev):
    """The join link: the conference entry point if Google parsed one, else the
    first join link found in the location or description."""
    conf = ev.get("conferenceData") or {}
    for ep in conf.get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    if ev.get("hangoutLink"):
        return ev["hangoutLink"]
    for field in ("location", "description"):
        m = MEETING_URL_RE.search(html.unescape(ev.get(field) or ""))
        if m:
            return m.group(0).rstrip(".,;")
    return None


def humans(ev):
    """Invited people (no rooms/resources), without the recorder."""
    return [a for a in ev.get("attendees", []) or []
            if a.get("email") and not a.get("resource")
            and a["email"].lower() != RECORDER_EMAIL]


def event_start(ev):
    s = (ev.get("start") or {}).get("dateTime")
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def qualifies(ev, now):
    """Return the meeting link if this calendar copy should get the notetaker."""
    if ev.get("status") == "cancelled":
        return None
    if ev.get("visibility") in ("private", "confidential"):
        return None
    organizer = (ev.get("organizer") or {}).get("email", "")
    if not organizer or _domain(organizer) in INTERNAL_DOMAINS:
        return None                      # Dobby-organized: attendee_sync's job
    start = event_start(ev)
    if not start:
        return None                      # all-day event
    if not (start - dt.timedelta(minutes=JOIN_EARLY_MIN) <= now
            <= start + dt.timedelta(minutes=JOIN_LATE_MIN)):
        return None
    me = next((a for a in ev.get("attendees", []) or [] if a.get("self")), None)
    if not me or me.get("responseStatus") not in ("accepted", "tentative"):
        return None
    attendees = ev.get("attendees", []) or []
    if any((a.get("email") or "").lower() == RECORDER_EMAIL for a in attendees):
        return None                      # Airspeed already joins via the invite
    if len(humans(ev)) < MIN_PARTICIPANTS:
        return None
    text = f"{ev.get('summary') or ''}\n{ev.get('description') or ''}".lower()
    if OPT_OUT_TAG in text:
        return None
    if ((ev.get("extendedProperties") or {}).get("private") or {}).get(MARKER_KEY):
        return None                      # already sent from this copy
    return meeting_url(ev)


def mark_sent(user, token, ev, stamp):
    """Record the dispatch on this user's own copy (private extended property;
    no notification, invisible to other guests)."""
    url = ("https://www.googleapis.com/calendar/v3/calendars/"
           f"{urllib.parse.quote(user)}/events/{urllib.parse.quote(ev['id'])}")
    private = dict(((ev.get("extendedProperties") or {}).get("private")) or {})
    private[MARKER_KEY] = stamp
    gcal._g_request("PATCH", url, token, params={"sendUpdates": "none"},
                    json_body={"extendedProperties": {"private": private}})


# --------------------------------------------------------------- AIRSPEED ---

def send_notetaker(link, ev):
    """POST /v1/call_bots. Returns 'sent', 'already' (409) or raises."""
    participants = [{"name": a.get("displayName"), "email": a["email"].lower()}
                    for a in humans(ev)]
    body = {"meeting_url": link, "title": ev.get("summary") or None,
            "participants": participants}
    for attempt in range(4):
        r = requests.post(f"{GLYPHIC_BASE}/call_bots", json=body,
                          headers={"X-API-Key": GLYPHIC_API_KEY}, timeout=30)
        if r.status_code in (200, 201):
            return "sent"
        if r.status_code == 409:
            return "already"
        if r.status_code in (429, 503) or r.status_code >= 500:
            time.sleep(2 + attempt * 3)
            continue
        if r.status_code == 402:
            raise RuntimeError("Airspeed recording limit reached (402)")
        r.raise_for_status()
    raise RuntimeError(f"Airspeed call_bots kept failing (HTTP {r.status_code})")


# ------------------------------------------------------------------ USERS ---

def get_users(adc):
    """NOTETAKER_USERS (comma list) pins a subset; otherwise USER_SOURCE=directory
    covers every active Workspace user, falling back to CALENDAR_USERS."""
    pinned = [u.strip() for u in os.environ.get("NOTETAKER_USERS", "").split(",") if u.strip()]
    if pinned:
        return pinned
    manual = [u.strip() for u in os.environ.get("CALENDAR_USERS", "").split(",") if u.strip()]
    if os.environ.get("USER_SOURCE", "list").strip().lower() == "directory":
        try:
            return directory_users.list_active_users(adc)
        except Exception as e:
            log(f"WARNING: Directory discovery failed ({e}); using CALENDAR_USERS")
    return manual


# --------------------------------------------------------------- CORE LOOP ---

class TokenCache:
    def __init__(self, adc):
        self.adc, self.tokens = adc, {}

    def get(self, user):
        tok, minted = self.tokens.get(user, (None, 0))
        if not tok or time.time() - minted > 45 * 60:
            tok = gcal.delegated_user_token(self.adc, user)
            self.tokens[user] = (tok, time.time())
        return tok


def in_active_hours():
    return ACTIVE_START_HOUR <= dt.datetime.now(ZoneInfo(LOCAL_TZ)).hour < ACTIVE_END_HOUR


def poll(users, tokens, sent_links, summary):
    now = dt.datetime.now(dt.timezone.utc)
    t_min = (now - dt.timedelta(minutes=JOIN_LATE_MIN + 5)).isoformat()
    t_max = (now + dt.timedelta(minutes=JOIN_EARLY_MIN + 1)).isoformat()

    # Group qualifying calendar copies by meeting link, so a meeting several
    # colleagues attend gets ONE notetaker.
    groups = {}
    for user in users:
        try:
            token = tokens.get(user)
            for ev in gcal.list_events(user, token, t_min, t_max):
                link = qualifies(ev, now)
                if link:
                    groups.setdefault(link, []).append((user, token, ev))
        except Exception as e:
            log(f"ERROR reading calendar for {user}: {e}")
            summary["errors"] += 1

    for link, copies in groups.items():
        user0, _, ev0 = copies[0]
        start_local = event_start(ev0).astimezone(ZoneInfo(LOCAL_TZ)).strftime("%a %H:%M")
        where = (f"'{ev0.get('summary') or '(no title)'}' {start_local} "
                 f"[{urllib.parse.urlsplit(link).hostname}, organizer @{_domain(ev0['organizer']['email'])}, "
                 f"for {', '.join(sorted({u for u, _, _ in copies}))}]")
        if link in sent_links:
            continue
        if DRY_RUN:
            log(f"[DRY-RUN] would send notetaker to {where}")
            sent_links.add(link)
            summary["would_send"] += 1
            continue
        try:
            result = send_notetaker(link, ev0)
            sent_links.add(link)
            summary["sent" if result == "sent" else "already"] += 1
            log(f"notetaker {'sent to' if result == 'sent' else 'already in'} {where}")
        except Exception as e:
            log(f"ERROR sending notetaker to {where}: {e}")
            summary["errors"] += 1
            continue
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        for user, token, ev in copies:
            try:
                mark_sent(user, token, ev, stamp)
            except Exception as e:
                # Non-fatal: in-run dedupe (sent_links) and Airspeed's own
                # duplicate guard still apply; only a later run could resend.
                log(f"WARNING: couldn't mark {user}'s copy as sent: {e}")


def main():
    if not in_active_hours():
        log("outside active hours — nothing to do")
        return
    if not GLYPHIC_API_KEY:
        log("FATAL: GLYPHIC_API_KEY must be set")
        sys.exit(1)

    adc = gcal.google_adc_token()
    users = get_users(adc)
    tokens = TokenCache(adc)
    log(f"notetaker dispatch {'(DRY-RUN)' if DRY_RUN else '(LIVE)'} — "
        f"{len(users)} calendars, polling every {POLL_SECONDS}s for {RUN_SECONDS}s")

    summary = {"would_send": 0, "sent": 0, "already": 0, "errors": 0}
    sent_links = set()
    deadline = time.time() + RUN_SECONDS
    while True:
        poll(users, tokens, sent_links, summary)
        if time.time() + POLL_SECONDS >= deadline or not in_active_hours():
            break
        time.sleep(POLL_SECONDS)

    log(f"done: {summary}")
    if summary["errors"]:
        slack_notify(
            f":warning: Notetaker Dispatch finished with {summary['errors']} error(s).\n"
            f"{summary}\n```\n" + "\n".join(_log_lines[-15:]) + "\n```"
        )
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        slack_notify(f":rotating_light: Notetaker Dispatch crashed: {e}\n```\n"
                     + "\n".join(_log_lines[-15:]) + "\n```")
        sys.exit(1)
