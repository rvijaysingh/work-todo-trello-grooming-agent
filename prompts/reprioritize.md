## Task: reprioritization (Mark More / Less Time-sensitive) — per-candidate verdict

You are reshaping Today and Next Few Days AFTER duplicates, archives, and hygiene
have already been cleaned this run. **Python has already done the hard part:** it
pre-ranked a shortlist of candidates and attached the facts for each. Your job is
to return exactly ONE verdict per candidate — `move` or `keep` — not to hunt the
whole board. Code re-verifies every signal you claim, enforces the confidence
floor and the hard exemptions, adds the placement-conflict note, and applies the
cap, so you do not need to hold back out of caution.

### Rules (from the spine's Notes for the agent)
- **List vs label:** a card on Today or Next Few Days means the user PLANS to work
  it then. Only the three time labels — "1. Today (must do)", "2. Next Few Days
  (must do)", "3. This Week (must do)" — mean it MUST happen then. Never infer a
  hard deadline from list placement alone.
- **Priority labels** (set by the meeting-notes agent): **"P0 - High"** = same-day
  (promote toward Today); **"P1 - Medium"** = next 1–3 days (promote toward Next
  Few Days). Treat them as promotion evidence.
- **Placement wins:** the user's placement outweighs spine priority; recently
  placed cards are protected (the code enforces this — you needn't second-guess).

### Candidates (each with pre-computed facts)
{repri_json}

- `promote_candidates` carry `verified_signals` (already confirmed by code) and a
  `suggested_target` list. If the card genuinely belongs higher, verdict `move`
  toward `suggested_target` and list the signals you rely on. Otherwise `keep`.
- `demote_candidates` come only from over-target lists, weakest first, each with a
  `weakness_score` and its `weakness` components. When `overflow_today` /
  `overflow_nfd` is large, these lists are overloaded — demote the weakest toward
  target (Today → Next Few Days, Next Few Days → This Week) using the `"weak"`
  signal. `keep` a card only when it is genuinely NOT weak. Never demote a
  `has_today_mustdo` card unless you include a `label_change` downgrading that
  label with a stated reason.

### Output — one verdict per candidate, none omitted
Return `{{"verdicts": [ ... ]}}` with an entry for EVERY candidate above. Each:
- "card_id": the candidate's id
- "verdict": "move" or "keep"
- For "keep": "reason" (one line).
- For "move": "direction" ("up"|"down"), "target_list" (destination list name),
  "signals" (subset of the card's facts you rely on: "priority_label",
  "due_in_window", "workstream_high", or "weak"), "confidence" (0–100), "reason"
  (one line; name any conflict with placement), and optional "label_change"
  ({{"action": "add"|"upgrade", "label": ...}} or {{"action": "downgrade",
  "from": ..., "to": ...}}).
- Optional "conflicts_placement": true if the move contradicts the user's placement.

Do not omit any candidate — a missing verdict is flagged in the run report.
