## Task: hygiene pass

You are given in-scope cards: names flagged by heuristics (priority), all in-scope
names (you may nominate additional renames), and `dead_dues` — cards more than the
dead-due window past, each with its id, name, current due date, and a description
snippet.

{hygiene_json}

IMPORTANT: return a `due_status` for EVERY card in `dead_dues` (do not skip any) —
each overdue card must resolve to an escalation (still_matters) or a fix
(no_longer_matters). A card you do not classify is left untouched and escalated.

For each card that needs cleanup, return one edit object with:
- "card_id": the card id (only ids from the data above)
- "new_name": cleaned name — follow the "Card naming standard" in the spine above
  when present (otherwise: fix typos, capitalize, verb-first where natural,
  preserve person names). Use only words/entities present in the source card or
  spine. Omit if no rename is needed.
- "new_desc": lightly restructured description (optional; never delete content)
- For a card with a dead due date, classify it against the spine:
  - "due_status": "still_matters" if its workstream is Active OR the card text
    shows a task that still must happen; "no_longer_matters" otherwise.
  - When "no_longer_matters", set a new date ONLY if one is actually written in
    the card text or given as a workstream deadline in the spine — never guess:
      - "new_due": the new ISO 8601 date, or null to clear the date instead.
      - "new_due_source": the exact verbatim substring (from the card text or
        spine) you read that date from. Required whenever "new_due" is set; it is
        checked against the source and the re-date is dropped if not found.
- "reason": one short line explaining the change (shown to the user verbatim).
- "confidence": integer 0–100 — your confidence this action is correct.
- "borderline": true if this is a judgment call the user should confirm first.

Return: {{"edits": [ ... ]}}
