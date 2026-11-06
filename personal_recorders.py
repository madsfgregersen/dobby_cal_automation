"""
Personal recorders — Dobby people whose OWN Airspeed notetaker joins their
meetings automatically (their calendar is connected in Airspeed).

When one of them hosts a meeting, or has accepted it, their personal notetaker
records it, so the shared recorder (support@dobby.io) steps back to avoid two
notetakers in the same call:
  * attendee_sync.py doesn't add support@ to that meeting, and removes it if it
    is already on the invite;
  * notetaker_dispatch.py doesn't send the notetaker to it.

"Accepted" (not tentative) matches the Airspeed recording preference
"Only record meetings that have been accepted", which should be ON and locked
for this to be reliable.

Config (repo variables):
  PERSONAL_RECORDERS  comma-separated e-mails, e.g. "mh@dobby.io, dm@dobby.io".
                      Set it to "none" to switch the step-back off entirely.
  STEP_BACK_DRY_RUN   anything but false/0/no keeps step-back in preview mode:
                      it only logs what it would change, and support@ keeps
                      recording exactly as before.
"""

import os


def _parse(raw):
    return {e.strip().lower() for e in (raw or "").split(",") if "@" in e}


PERSONAL_RECORDERS = _parse(os.environ.get("PERSONAL_RECORDERS", ""))

STEP_BACK_DRY_RUN = (
    os.environ.get("STEP_BACK_DRY_RUN", "true").strip().lower() not in ("false", "0", "no")
)


def present(ev, recorders=None):
    """Sorted personal recorders whose notetaker will join this meeting: the
    host (unless they declined their own meeting) or a guest who accepted."""
    recorders = PERSONAL_RECORDERS if recorders is None else recorders
    if not recorders:
        return []
    status = {(a.get("email") or "").lower(): a.get("responseStatus")
              for a in ev.get("attendees", []) or []}
    organizer = ((ev.get("organizer") or {}).get("email") or "").lower()
    found = set()
    if organizer in recorders and status.get(organizer) != "declined":
        found.add(organizer)
    found |= {e for e, s in status.items() if e in recorders and s == "accepted"}
    return sorted(found)
