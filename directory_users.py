#!/usr/bin/env python3
"""
Shared helper: list the domain's active users via the Admin SDK Directory API.

Used by sync.py and attendee_sync.py when USER_SOURCE=directory, so the team
list is discovered live instead of hand-maintained in CALENDAR_USERS. New hires
and leavers are then picked up automatically.

Requirements:
  * the scope https://www.googleapis.com/auth/admin.directory.user.readonly on
    the service account's domain-wide delegation, and
  * an admin user to impersonate (Directory users.list needs directory-read
    admin rights) — set via DIRECTORY_ADMIN.
"""

import json
import os
import time

import requests

SERVICE_ACCOUNT_EMAIL = "meeting-sync@dobby-workspace-automations.iam.gserviceaccount.com"
DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"

# Admin account to impersonate for the enumeration. `or` (not a default arg) so
# an env var set to an empty string still falls back rather than breaking.
DIRECTORY_ADMIN = os.environ.get("DIRECTORY_ADMIN") or "mg@dobby.io"
# Always excluded, plus anything in the USER_EXCLUDE env (comma-separated).
RECORDER_EMAIL = (os.environ.get("RECORDER_EMAIL") or "support@dobby.io").lower()


def _admin_token(adc_token, admin_email):
    """Mint a Directory-scoped token acting as an admin, keyless (signJwt)."""
    now = int(time.time())
    claims = {
        "iss": SERVICE_ACCOUNT_EMAIL,
        "sub": admin_email,
        "scope": DIRECTORY_SCOPE,
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


def list_active_users(adc_token, admin_email=None):
    """Return the primary emails of all non-suspended users in the domain,
    excluding the recorder and any emails listed in USER_EXCLUDE."""
    admin_email = admin_email or DIRECTORY_ADMIN
    token = _admin_token(adc_token, admin_email)

    exclude = {RECORDER_EMAIL}
    exclude |= {e.strip().lower() for e in os.environ.get("USER_EXCLUDE", "").split(",") if e.strip()}

    emails = []
    page_token = None
    while True:
        params = {
            "customer": "my_customer",
            "maxResults": 500,
            "projection": "basic",
            "orderBy": "email",
            "query": "isSuspended=false",
        }
        if page_token:
            params["pageToken"] = page_token
        r = requests.get(
            "https://admin.googleapis.com/admin/directory/v1/users",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for u in data.get("users", []):
            email = (u.get("primaryEmail") or "").lower()
            if email and not u.get("suspended") and email not in exclude:
                emails.append(email)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return emails
