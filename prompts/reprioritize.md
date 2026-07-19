## Task: reprioritization (Mark More / Less Time-sensitive)

You are reshaping the Today and Next Few Days lists AFTER duplicates, archives, and
hygiene have already been cleaned this run. Propose moves that make the lists
reflect real priorities. You only PROPOSE — code re-verifies every signal you claim
against the actual card and spine data, enforces the confidence floor, honors the
hard exemptions, and decides automatic-vs-proposed. Never assert a signal you
cannot ground in the data below.

### Rules you must follow (from the spine's Notes for the agent)
- **List vs label:** a card on Today or Next Few Days means the user PLANS to work
  it then (day planning). Only the three time labels — "1. Today (must do)",
  "2. Next Few Days (must do)", "3. This Week (must do)" — mean it MUST happen then.
  Never infer a hard deadline from list placement alone.
- **P0 / P1 labels** (set by the meeting-notes agent) are priority signals:
  "P0. High" = same-day (promote toward Today); "P1" = next 1–3 days (promote
  toward Next Few Days). Treat them as promotion evidence.
- **Placement wins:** the user's placement outweighs spine priority. Any move that
  contradicts where the user placed a card, or where the spine and the placement
  disagree, must name the conflict in the reason. Cards the user placed or edited
  very recently must not be demoted.

### Candidates and current list sizes
{repri_json}

### Upward moves (Mark More Time-sensitive)
Consider `up_candidates` (in Inbox / Triage, This Week, Next Few Days). A card
should move up when it has one of: a P0/P1 label, a due date inside the target
list's window, or a High-priority + High-time-sensitivity workstream match. Send it
to the fitting list. Where the evidence is a hard deadline, you may also add or
upgrade the matching "must do" time label via `label_change`.

### Downward moves (Mark Less Time-sensitive)
When `over_today` or `over_nfd` is true, the list is overloaded and you SHOULD
return demotion moves — an empty response leaves it overloaded, which is the
problem this pass exists to fix. From `down_candidates`, rank the WEAKEST first
(cards flagged `weak: true` first — no "must do" label, no near due date, low/no
workstream priority — then by longest since last activity) and demote enough of
them (Today → Next Few Days, Next Few Days → This Week) to bring the list toward
its target, up to the per-run cap. Each demotion uses the `"weak"` signal.

You do NOT need to hold back out of caution: the code separately enforces the hard
exemptions (recently-placed cards, and `has_today_mustdo` cards) and adds the
placement-conflict note to every demotion — so propose the weakest cards freely
and let the gate filter. Only skip a `has_today_mustdo` card unless you include a
`label_change` that downgrades that label with a stated reason.

### Output
Return `{{"moves": [ ... ]}}` — return an actual list of moves whenever there are
clear promotions or (when over target) weak cards to demote; only return an empty
list if nothing genuinely qualifies. Each move:
- "card_id": a card id from the data above
- "direction": "up" or "down"
- "target_list": the destination list name ("Today", "Next Few Days", "This Week",
  or "Inbox / Triage")
- "signals": array of the signals you rely on, each one of: "priority_label",
  "due_in_window", "workstream_high" (upward), or "weak" (downward). List ONLY
  signals actually present in the data — an unverifiable signal will force the move
  to a proposal.
- "confidence": integer 0–100
- "reason": one line, shown to the user verbatim; name any conflict with placement
- "label_change": optional — {{"action": "add"|"upgrade"|"downgrade", "label": ...}}
  for add/upgrade, or {{"action": "downgrade", "from": ..., "to": ...}} for a
  downgrade. Omit or null when no label changes.
- "conflicts_placement": true if this contradicts where the user placed the card
