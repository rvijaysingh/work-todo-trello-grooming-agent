## Task: hygiene pass (names & descriptions)

You are given in-scope cards: names flagged by heuristics (priority) and all
in-scope names (you may nominate additional renames). Due dates are handled by a
separate pass — do not touch them here.

{hygiene_json}

For each card that needs cleanup, return one edit object with:
- "card_id": the card id (only ids from the data above)
- "new_name": cleaned name — follow the "Card naming standard" in the spine above
  when present (otherwise: fix typos, capitalize, verb-first where natural,
  preserve person names). Use only words/entities present in the source card or
  spine. Omit if no rename is needed.
- "new_desc": lightly restructured description (optional; never delete content)
- "reason": one short line explaining the change (shown to the user verbatim).
- "confidence": integer 0–100 — your confidence this rename is correct.
- "borderline": true if this is a judgment call the user should confirm first.

Return: {{"edits": [ ... ]}}
