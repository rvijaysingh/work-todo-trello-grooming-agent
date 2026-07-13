## Task: in-scope archive candidates

You are given in-scope cards (Today / Inbox / Triage / Next Few Days). Decide which
are NO LONGER NEEDED and should be moved to the Agent Archive list. Use the spine.

{inscope_json}

A card is "no longer needed" if ANY holds:
- its workstream is marked Done (or Winding down) on the spine;
- an event or deadline it depends on has clearly passed with nothing left to do;
- it is titled "[Owner: Name]" (a delegated/handed-off item).

Return one object ONLY for cards that ARE no longer needed:
- "card_id": the card id (only ids from the data above)
- "reason": one short line naming which test it meets (shown to the user verbatim)
- "confidence": integer 0–100
- "borderline": true if the user should confirm before archiving

Archived cards move to the top of the Agent Archive list (visible 60 days, then
Trello's restorable archive) — never deletion. Omit cards that are still needed.

Return: {{"archives": [ ... ]}}
