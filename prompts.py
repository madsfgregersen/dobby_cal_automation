"""
Summarization prompts for the Airspeed sync.

HOW TO EDIT
-----------
This file is the tuning surface for meeting summaries. To change how summaries
read, edit the text inside the triple-quoted strings below and commit. You do
NOT need to touch airspeed_sync.py.

Rules of the road:
  * Keep the three keys in PROMPTS exactly as they are: "sales",
    "customer_success", "internal". The pipeline picks one by meeting type.
  * Leave FORMAT_INSTRUCTION's "## " (section header) and "- " (bullet)
    convention intact — the code parses those markers to build the Notion
    blocks. You can reword it, but if you drop that convention the summary
    formatting in Notion will degrade to plain paragraphs.
  * Everything else — sections, tone, length, wording — is yours to change.
"""

# Appended to every prompt. Normalizes Claude's output so the pipeline can
# render it as clean Notion blocks. Edit with care (see note above).
FORMAT_INSTRUCTION = (
    "Format the summary in Markdown: use '## ' for each section header and "
    "'- ' for bullet points. A line that begins with '- [ ] ' renders as a "
    "checkbox to-do in Notion — use it for items meant to be ticked off. Do not "
    "use bold, italics, or other markup. Do not add a title or any preamble — "
    "start directly with the first section header. Omit any section that has "
    "nothing substantive to report."
)

SALES = """You are summarizing a Dobby sales call for our team. Structure the \
output under these exact sections with emoji headers: 🎯 Meeting Overview \
(purpose, attendees, duration), 🔑 Key Discussion Points (narrative, not \
bullets — what was actually discussed and how), 😣 Pain Points Raised, 💰 \
Budget & Timeline, 🏆 Decision Makers, ⚔️ Competitors Mentioned, 🚧 Objections \
& Risks, ✅ Next Steps (owner + deadline for each), 💬 Verbatim Highlights (2-4 \
direct quotes, attributed). Pull only what's explicitly stated or clearly \
implied — write "not discussed" rather than guessing at any section. Keep Key \
Discussion Points as flowing prose (3-6 sentences); every other section as \
short bullets, except items under ✅ Next Steps, which you write as Markdown \
task-list lines beginning '- [ ] ' so they become checkboxes. Assume the \
reader is a Dobby AE or CS lead who wasn't on the call and needs to act on \
this within 5 minutes."""

CUSTOMER_SUCCESS = """From the transcript, write a concise, skimmable summary \
focused on what a CS lead needs to act on. Use participants' real names. Be \
specific and factual — never invent detail. Ignore small talk, greetings, and \
technical join issues. Write in English even if parts of the call are in \
another language. Structure it under exactly these sections, in this order: \
Commitments & next steps (each as owner → what → when, including any agreed \
follow-ups); Decisions & confirmations; Open items & risks (outstanding \
requirements, customer requests, blockers, and risks to delivery). Under \
'Commitments & next steps', write every item as a Markdown task-list line \
beginning '- [ ] ' so it becomes a checkbox; use plain '- ' bullets in the \
other two sections. Keep every point to a single short sentence — enough to \
stand on its own, but no filler or elaboration. Omit any section that has \
nothing substantive."""

INTERNAL = """From the transcript, write a concise, skimmable summary focused \
on what each team member needs to act on. Use participants' real names. Be \
specific and factual — never invent detail. Ignore small talk, greetings, and \
technical join issues. Write in English even if parts of the call are in \
another language. Structure it under: Decisions & confirmations; Risks & \
blockers; Next steps. Under 'Next steps', write every item as a Markdown \
task-list line beginning '- [ ] ' so it becomes a checkbox; use plain '- ' \
bullets in the other two sections. Keep every point to a single short \
sentence — enough to stand on its own, but no filler or elaboration. Omit any \
section that has nothing substantive."""

PROMPTS = {
    "sales": SALES,
    "customer_success": CUSTOMER_SUCCESS,
    "internal": INTERNAL,
}
