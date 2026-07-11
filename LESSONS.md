# LESSONS.md — work-todo-trello-grooming-agent

Operational findings from building and debugging the agent. Newest first.

## Docs-lock referenced a shared-library capability that did not exist
- **Symptom:** Phase 2 implementation was blocked at the start because the locked
  design docs (design.md §Environment, config.md `local_tz_offsets`) instructed the
  agent to use "the stdlib UTC-offset/DST helper from agent-shared-library" — but no
  such helper existed anywhere in the shared library's package exports.
- **Root cause:** the design spec asserted a *home* for the capability (named the
  package it should live in) without verifying that the package actually exported or
  even contained it. The docs-lock phase treated a planned/assumed capability as an
  existing one.
- **Guard:** future Phase 1 docs-lock prompts must verify that every shared-library
  capability a doc depends on actually exists — importable/exported from the
  installed package (grep the package's `__init__` exports or `import` it), not just
  named in the design narrative — before the docs are locked. When a required
  capability is absent, the docs-lock output must flag it as a build dependency
  (add it to the shared library first, or point at where it really lives) rather
  than asserting it as present.
- **Resolution for this build:** ported the helper into
  `agent_shared.infra.timeutil` (0.2.0) before implementation — see the timeutil
  entry below.

## Time handling: the shared UTC-offset/DST helper did not exist yet
- **Symptom / root cause:** docs/design.md and docs/config.md specified that all
  wall-clock window math (no-touch, quarantine/label expiry, dead-due, proposal
  timeout) use "the stdlib-only UTC-offset helper with DST auto-switching from
  agent-shared-library." No such helper existed in the shared library.
- **Resolution:** ported the granola-to-notion agent's local `timezone_helpers`
  into `agent_shared.infra.timeutil` (v0.2.0): stdlib only, no `zoneinfo`; takes
  `standard`/`daylight` offsets as params + `dst_enabled` (default True); the US
  DST rule (2nd Sun Mar 02:00 → 1st Sun Nov 02:00) is encoded in the helper, not
  config. `guardrails.local_now/hours_since/days_since` drive every window off it.
- **Guard:** `tests/test_timezone.py::test_no_zoneinfo_dependency` fails the build
  if any agent module imports `zoneinfo`.

## Clearing a Trello due date via the shared client
- **Root cause:** `TrelloClient.update_card` only sends the `due` field when
  `due_date is not None`, so passing `None` cannot clear an existing due date.
- **Resolution:** `BoardMutator.clear_due` calls `update_card(card_id, due_date="")`.
  The Trello API treats an empty string as "clear the due date."

## Card badges (attachments / checklists) — RESOLVED in agent-shared-library 0.2.1
- **Original root cause:** `TrelloClient.get_list_cards` returned `TrelloCard`
  objects that dropped the raw `badges` field, so `has_attachments` /
  `has_checklist` — needed for the design §5.3 forced-Tier-2 merge rule — could not
  be populated from runtime list reads; only `build_board` (fixture/raw payload)
  read badges directly.
- **Resolution:** agent-shared-library **0.2.1** adds `attachment_count` (int) and
  `has_checklist` (bool) to `TrelloCard`, populated by `_parse_card` from the Trello
  `badges` object and `idChecklists`. `phases.snapshot_diff.card_from_trello` now
  maps these onto the agent's `Card`, so the forced-Tier-2 rule fires on the real
  runtime parsing path. Verified by `tests/test_badges_runtime.py`, which drives raw
  Trello dicts through the shared `_parse_card` (not fixture badges).
- The string-containment merge invariant remains the primary safety net regardless.

## No per-label application timestamp from Trello without action history
- **Root cause:** Trello does not return when a given label was applied to a card
  from a normal card/list read.
- **Resolution:** used `card.dateLastActivity` as the age proxy for both
  `Agent: Auto-Updated` label expiry and stale time-based-label detection — an
  untouched agent-labeled card's last activity approximates when the agent last
  touched it. If Vijay edits the card, the diff catches it as a rejection first.

## pytest 9.1.1 capture teardown noise in the shared-library suite
- **Symptom:** running the *shared-library* suite without `-s` can raise
  `ValueError: I/O operation on closed file` during pytest's global-capture
  teardown (a pre-existing interaction, unrelated to this agent). All 306 shared
  tests pass functionally with `-s`. The agent's own suite is unaffected and runs
  clean under the canonical `python -m pytest tests/ -x`.
