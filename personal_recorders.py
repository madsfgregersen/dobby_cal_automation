"""
Personal recorders — Dobby people whose OWN Airspeed notetaker joins their
meetings automatically (their calendar is connected in Airspeed).

One recorder per meeting, decided by who HOSTS it:
  * hosted by a personal recorder -> their own notetaker records it, and
    attendee_sync.py keeps support@dobby.io off the invite (removing it if
    it's already there);
  * hosted by anyone else -> support@ (or notetaker_dispatch.py for meetings
    hosted outside Dobby) records it.

This relies on the Airspeed recording preference "Only record meetings where
the individual is the host" being ON (and locked) for these people. Otherwise
their notetaker also joins meetings they merely attend, where support@ is
already recording — and two personal recorders invited to the same meeting
would both join (the 4 Nov "KIP" duplicate).

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


def host(ev, recorders=None):
    """The personal recorder hosting this meeting (whose own notetaker will
    record it), or None. A host who declined their own meeting doesn't count."""
    recorders = PERSONAL_RECORDERS if recorders is None else recorders
    organizer = ((ev.get("organizer") or {}).get("email") or "").lower()
    if organizer not in recorders:
        return None
    for a in ev.get("attendees", []) or []:
        if (a.get("email") or "").lower() == organizer and a.get("responseStatus") == "declined":
            return None
    return organizer
