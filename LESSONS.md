# LESSONS.md — work-todo-trello-grooming-agent

Operational findings from building and debugging the agent. Newest first.

## Executed recoveries never reached the report (result.applied vs result.recoveries)
- **Symptom:** the first dry-run report showed zero recovery dispositions even
  though the batch ran and cards were (would be) moved.
- **Root cause:** `execute_recovery` appended routed/archived cards only to
  `result.recoveries`, but the report's "Done automatically" section iterates
  `result.applied`. Recoveries were executed but invisible.
- **Fix:** every executed recovery now also appends a typed entry to
  `result.applied` (`recovery_route` / `recovery_archive` / `recovery_merge`) with
  origin + destination, so the report renders it. Regression:
  `test_recovery_routing_appears_in_applied_for_report`.

## Recovery batch could silently produce zero dispositions
- **Root cause:** if the LLM returned no disposition for a batch card, nothing
  happened to it — a non-empty batch could drain to nothing with no signal.
- **Fix:** `main._default_recovery` fills any un-disposed batch card with the
  design's ambiguous-context default (Inbox / Triage). Regression:
  `test_recovery_batch_defaults_undisposed_cards_to_inbox`.

## Overdue due dates silently dropped when the LLM didn't classify them
- **Symptom:** many 14+ day overdue in-scope cards produced no escalation and no
  fix.
- **Root cause:** the dead-due handler only acted on cards the hygiene LLM
  returned a `due_status` for; unclassified dead-dues fell through. The hygiene
  payload also sent only bare ids (no due date / text), so the model rarely
  classified them.
- **Fix:** the payload now includes each dead-due card's name, due date, and a
  description snippet, and the prompt requires a `due_status` for every one.
  Deterministically, any dead-due in-scope card the LLM did not classify is
  escalated to "Still overdue" (never silently dropped). Regression:
  `test_overdue_card_never_silently_dropped`,
  `test_every_dead_due_produces_escalation_or_fix`.

## Dry-run persisted internal state (reminder slot, ledgers, proposals, snapshot)
- **Symptom:** the dry-run report said the weekly spine-review card was "created";
  more subtly, a dry-run consumed the once-per-week reminder slot and wrote
  proposal / recovery / archive rows and a post-run snapshot — any of which would
  wrongly alter the next *live* run.
- **Root cause:** board writes were correctly gated behind `mutator.dry_run`, but
  SQLite persistence was not. `create_card` for the reminder made zero Trello
  calls (correct), yet the report used past-tense wording and the `kv`
  `last_reminder_week` marker was still set.
- **Fix:** dry-run is now zero board writes **and** zero persistent-state changes —
  every `storage.*` write in the execution path and the post-run `save_snapshot`
  is gated on `not dry_run`. The report uses "would create / would move / would
  remove / would rename / would clear" in dry-run. Regressions:
  `test_dry_run_reminder_and_all_writes_zero_trello_calls`,
  `test_dry_run_persists_no_state`, `test_dry_run_report_uses_would_wording`.
- **Note:** several unit tests had used a dry-run mutator purely to avoid real
  Trello calls while asserting DB effects; they now use `dry_run=False` with a
  mock Trello, matching the clarified contract.

## Report entries must be actionable (titles + URLs, never bare ids)
- **Root cause:** proposals rendered as "[merge] proposal #1" and applied actions
  as `type card_id` — unactionable, and archive wording didn't distinguish moving
  a card *into* the Agent Archive list from the eventual 60-day Trello archive.
- **Fix:** proposals now show truncated title (~60 chars), URL, a plain-language
  action, the reason, and `Confidence: NN%`; all entries render title+URL via
  `report._card_ref`; stale-label entries name the removed label; and the two
  archive stages use distinct wordings — `archive_list_wording` ("moved to the
  Agent Archive list (visible 60 days)") vs `TRELLO_ARCHIVE_WORDING` ("moved to
  Trello's archive (restorable)"). Covered by `tests/test_report.py`.

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
