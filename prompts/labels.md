## Task: stale "must do" label disposition

You are given in-scope cards carrying a stale time-based label (e.g. "1. Today
(must do)") — the card is not in the list the label implies and the label is old.
For each, decide whether to SWAP the label to the tier matching its real situation
or REMOVE it. (Cards that are no longer needed are archived by a separate pass and
are not included here.)

{labels_json}

For each card return:
- "card_id": the card id (only ids from the data above)
- "disposition": one of
  - "swap"   — its workstream is Active AND Time-sensitive on the spine. Move the
    label to the matching tier: set "target_label" to "2. Next Few Days (must do)"
    (higher urgency / High priority) or "3. This Week (must do)" (this week).
  - "remove" — otherwise (not time-sensitive, or context unclear).
- "target_label": required for "swap"; one of the two labels above, exactly.
- "reason": one short line (shown verbatim). The old label is noted in a comment.
- "confidence": integer 0–100
- "borderline": true if the user should confirm first

Return: {{"labels": [ ... ]}}
