## Task: recovery triage

You are given cards recovered from Scratch/Archive lists. Use the spine to tell
live workstreams from dead ones.

{recovery_json}

For each card, return one disposition object with:
- "card_id": the card id (only ids from the data above)
- "disposition": one of
  - "today"          — spine clearly supports doing it today
  - "next_few_days"  — soon, spine-supported
  - "this_week"      — this week, spine-supported
  - "inbox"          — ambiguous context (default)
  - "merge"          — duplicate of an active card (set "merge_into")
  - "archive"        — no longer needed. The "no longer needed" test is: the
    workstream is marked Done on the spine, OR an event/deadline has passed with
    nothing left to do, OR the card is titled "[Owner: Name]". Archived cards move
    to the top of the Agent Archive list — describe as "moved to Trello's archive
    (restorable)", never deletion.
- "merge_into": target active card id when disposition is "merge" (else null)
- "confidence": integer 0–100 — your confidence in this disposition
- "borderline": true if the user should confirm before archiving/merging
- "reason": one line (shown to the user verbatim)

Return: {{"dispositions": [ ... ]}}
