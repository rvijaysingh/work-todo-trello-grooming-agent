## Task: dead-due classification

You are given `cards` — in-scope cards whose due date is well past. For EVERY card
in the list (do not skip any), classify its due date against the spine and the
card's own text.

{due_json}

Return one object per card with:
- "card_id": the card id (only ids from the data above)
- "due_status": exactly one of
  - "still_matters"     — its workstream is Active on the spine, OR the card text
    shows a task that still must happen. The date is left untouched and the card
    is surfaced to the user.
  - "no_longer_matters" — the workstream is Done, the event/deadline has passed
    with nothing left to do, or the card is titled "[Owner: Name]".
- When "no_longer_matters", optionally set a NEW date — but ONLY if one is
  actually written in the card text or given as a workstream deadline on the
  spine; never guess:
  - "new_due": the new ISO 8601 date, or null to clear the date instead.
  - "new_due_source": the exact verbatim substring (from the card text or spine)
    you read that date from. Required whenever "new_due" is set; it is checked
    against the source and the re-date is dropped (date cleared) if not found.
- "reason": one short line (shown to the user verbatim).
- "confidence": integer 0–100.
- "borderline": true if the user should confirm before the agent acts.

Every card in the input MUST appear exactly once in the output.

Return: {{"classifications": [ ... ]}}
