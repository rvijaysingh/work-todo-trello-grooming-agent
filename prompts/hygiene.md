## Task: hygiene pass

You are given in-scope cards: names flagged by heuristics (priority), all in-scope
names (you may nominate additional renames), and card ids with dead due dates.

{hygiene_json}

For each card that needs cleanup, return one edit object with:
- "card_id": the card id (only ids from the data above)
- "new_name": cleaned name — fix typos, capitalize, verb-first where natural,
  preserve person names; use only words/entities present in the source card or
  spine. Omit if no rename is needed.
- "new_desc": lightly restructured description (optional; never delete content)
- "clear_due": true if this card's dead due date should be cleared
- "remove_labels": list of label names to remove (optional)

Return: {{"edits": [ ... ]}}
