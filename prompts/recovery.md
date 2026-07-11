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
  - "archive"        — clearly obsolete (e.g. done-workstream logistics)
- "merge_into": target active card id when disposition is "merge" (else null)
- "reason": one line

Return: {{"dispositions": [ ... ]}}
