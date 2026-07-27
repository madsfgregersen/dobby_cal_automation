# Meeting Sync — Step 1

Creates and keeps up-to-date a Notion **Meeting Notes DB** record for every
external meeting on the team's Google Calendars, stamped with the Google
Calendar event ID as the join key for Steps 2 (Airspeed) and 3 (follow-up).

Runs hourly in GitHub Actions with **no key files** — auth is Workload Identity
Federation (GitHub → Google) plus domain-wide delegation (read everyone's
calendar).

## Repository layout

Copy the files into the `madsfgregersen/dobby_cal_automation` repo like this:

```
.github/workflows/meeting-sync.yml   <- the workflow (this is meeting-sync.yml)
sync.py
requirements.txt
```

## One remaining Google grant (required — do this first)

Keyless domain-wide delegation needs the service account to be able to sign JWTs
for itself. Run this once in Cloud Shell (or `gcloud` locally):

```
gcloud iam service-accounts add-iam-policy-binding \
  meeting-sync@dobby-workspace-automations.iam.gserviceaccount.com \
  --member="serviceAccount:meeting-sync@dobby-workspace-automations.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

Without this, the job authenticates fine but every calendar read fails with a
permission error. (Console alternative: IAM & Admin → IAM → Grant access →
principal = the service account → role = **Service Account Token Creator**.)

## GitHub configuration

**Secrets** (repo → Settings → Secrets and variables → Actions → Secrets):
- `NOTION_TOKEN` — the internal integration secret (starts `ntn_`/`secret_`).
- `SLACK_WEBHOOK_URL` — *optional*. An incoming-webhook URL; the job pings it
  only when a run has errors, so failures don't pass silently.

**Variable** (same page → Variables):
- `CALENDAR_USERS` — comma-separated team emails whose calendars to read,
  e.g. `mg@dobby.io,dm@dobby.io,ng@dobby.io`. Editing this Variable changes the
  team list with no code change. (If unset, the fallback list in `sync.py` is
  used.)

## Testing before you trust it

1. Do the Google grant and set the secrets/variable above.
2. Push the three files.
3. Actions tab → **Meeting Sync** → **Run workflow** (manual trigger).
4. Check the run log for the `done: {...}` summary line and confirm the records
   appear in the Meeting Notes DB with the right customer and a populated
   Calendar Event ID.

Start with just your own email in `CALENDAR_USERS` for the first run, eyeball
the results, then widen to the whole team.

## What it writes — and what it never touches

**Machine-owned (overwritten each run):**
- `Meeting name`, `Date & Time`, `Participants` (attendee emails)
- `Calendar Event ID` — the join key, set once
- `Status` — set to `Planned` on creation; set to `Cancelled` if the event is
  cancelled. **Never otherwise changed** — so a `Completed` set later by Step 3
  is safe.
- `Customer` — set from the attendee-domain match; on later runs only filled if
  still empty, so a manual correction is never overwritten.

**Human-owned (never touched):** the page body (agenda, decision points,
things-to-remember), plus `Attendees`, `Area`, `Category`, `Project`, `Summary`.

## Behaviour notes

- **Scope:** every meeting in the window is processed, internal ones included.
  Attendee domains are used only to match a customer; an internal-only meeting
  just gets a blank `Customer`.
- **No customer match:** the record is still created, with `Customer` left blank
  for manual assignment. (The five customers without email handles will land
  here until their handles are filled.)
- **Cancellations:** marked `Cancelled`, never deleted — prep is preserved.
- **Idempotent:** keyed on the Calendar Event ID, so a missed hourly run
  self-heals on the next one, and invited events shared across calendars are
  written once.

## Known limitations (fine for v1)

- A **hard-deleted standalone** event (not just cancelled) may not reappear in
  the window, so its record can stay `Planned`. The meeting simply won't happen;
  prep is preserved. A future switch to incremental sync tokens would close this.
- **Team list** is a manual Variable. Auto-discovery via the Admin SDK Directory
  API is the upgrade path (needs an extra delegation scope).
- **Recurring meetings** expand to per-occurrence event IDs — good, each
  occurrence gets its own record. Step 2 must confirm Airspeed carries that same
  per-occurrence ID.
