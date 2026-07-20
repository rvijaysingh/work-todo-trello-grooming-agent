# LESSONS.md — work-todo-trello-grooming-agent

Operational findings from building and debugging the agent. Newest first.

## Trello 400 class (CRLF + byte limits) — second agent bitten
- **Symptom:** a limited_test run applied all its real board actions (archives,
  label swaps, recoveries, ~20 proposals) and then crashed with a Trello
  `400 Bad Request` on `POST /1/cards` when publishing the Grooming Report card —
  turning a successful run into a failure (no snapshot saved, failure counter bumped).
- **Root cause (cross-repo — the gmail-to-trello agent hit the same class):** Trello
  rejects card bodies that (a) exceed the **16384-BYTE** description limit — a busy
  run produced a 19,885-char report — and (b) contain bare control chars / `\r\n`
  line endings. A character-count truncation is *not* enough: multibyte chars
  (em-dash `—`, ellipsis `…` = 3 bytes each) mean 16k chars can still exceed 16k
  bytes. The un-guarded write also let the exception propagate out of the whole
  pipeline *after* the irreversible board writes.
- **Fix:**
  1. `execute.sanitize_card_text` normalizes `\r\n`/`\r` → `\n` and strips C0
     controls (except tab/newline) + DEL; the `BoardMutator` applies it to **all**
     card-bound text (comments, descriptions, card names).
  2. The report **card** gets a CONDENSED render (`build_report(card=True)`: header,
     At a glance, Today plan, Still overdue, Awaiting your decision in full;
     Recently archived / Done automatically / Health stats collapsed to counts; plus
     a `Full report: logs\grooming_report.txt` pointer), then **byte-aware**
     truncation (`_byte_truncate`, no split multibyte char, `…truncated` marker).
     The full report is always on disk.
  3. `publish_report_card` is best-effort (try/except, returns published bool); a
     failure logs + emails but the run counts as **success** when all pipeline
     actions succeeded. Re-running is idempotent for the crash-after-execute case:
     `generate_proposals` now skips any fingerprint with an already-**open** proposal
     (the rejection ledger only covered rejected/expired ones).
- **Consider:** move `sanitize_card_text` + the byte-truncation into
  `agent-shared-library` so every agent that writes Trello cards inherits it (two
  agents bitten now). Regressions:
  `test_july19_update.py::{test_sanitize_card_text_normalizes_crlf_and_strips_controls,
  test_card_report_condensed_fits_and_summarizes, test_byte_truncate_respects_limit_and_marks,
  test_open_proposal_not_duplicated_on_rerun}`.
- **Env note:** this session's bare `python` resolved to **3.14.3** while the project
  targets **3.13**; always invoke `py -3.13` (…\Python313\python.exe) for tests and
  runs. The suite is green on both, but 3.13 is the supported interpreter.

## Completed-workstream cards were promoted (report review, July 19)
- **Symptom:** a "Sales Summit recap" card was promoted (Increase Time-Sensitivity)
  even though the Sales Summit workstream is **Complete** on the spine — the exact
  opposite of what a finished workstream should trigger.
- **Root cause:** `matched_high_workstream` only checked for **Active** High/High
  workstreams and nothing prevented promotion when a card matched a Complete / Done
  / Paused workstream. The reprioritization pass had no "terminal workstream" veto,
  so a promotion signal (a P0 label) carried the card upward regardless.
- **Fix (code-enforced, not the LLM):** `signals.matched_terminal_workstream` +
  `reprioritize.promotion_veto` block promotion for terminal-workstream matches,
  high staleness, and reflection cards; terminal/stale cards route to a
  `[Move to Archive]`/`[Move to Backlog]` proposal instead. Regression:
  `test_july19_update.py::test_complete_workstream_never_promoted_routes_to_archive`.
  Lesson: a promotion path must check the *disqualifiers* (workstream finished,
  need already passed) in code, not rely on the LLM to infer them.

## Spine read 404'd: Notion token key drifted from the .env layout
- **Symptom:** every run logged "Spine read failed … continuing without spine";
  the board was groomed with no workstream context (label swaps couldn't cite the
  spine, reprioritization couldn't match workstreams). The Notion API returned 404
  on the spine page.
- **Root cause:** `.env.json` moved this agent's Notion token to a per-agent key
  `notion.integration_token_work_trello_grooming_agent`, but `load_credentials`
  still read the flat `notion.integration_token`, which held a **different**
  integration's token that lacks access to the spine page → 404. Same failure
  family as the rent-checker's flat Anthropic-key bug (CLAUDE.md): reading a
  credential from a path the file no longer populates.
- **Fix:** resolve the Notion token with explicit precedence — agent-specific key
  first, then the shared `notion.integration_token`, then a loud startup failure
  naming **both** paths — mirroring the nested `anthropic_api_keys.<agent>` lookup.
  Log which KEY was selected at INFO (never the token value). Lesson: per-agent
  credential keys must be read by their agent-specific path with a named fallback,
  never a bare shared field. Regression: `test_credentials.py`.
- **Compounding fix:** a spine-read failure was also being swallowed ("continue
  without spine"), so the board was silently groomed on stale assumptions. A failed
  spine read now runs in DEGRADED mode — skip the archive, time-label, dead-due, and
  reprioritization passes; keep merges and Inbox-only recovery; show a report banner;
  send an alert. Regression: `test_spine_failure.py`.

## PATTERN: LLM bulk-generators silently return nothing (2nd occurrence)
- **Symptom:** the reprioritization pass logged 22 promote / 138 demote candidates
  on a board 35 cards over target (Today 50/15, Next Few Days 88/20), invoked the
  judge, got a clean parseable LLM response (no truncation, no fallback) — and
  produced **0 moves, 0 proposals**. Two rounds of prompt wording ("you SHOULD
  demote", "don't hold back") changed nothing.
- **Root cause — same shape as the dead-due bug below.** The judge was a *bulk
  generator*: handed ~160 candidates and asked to emit a `moves` array, it was free
  to return `[]`, and did. A model that must *originate* a list under a
  placement-wins / "ignoring leaves everything where you put it" framing will
  reliably choose the empty, safe output. This is the second time the pattern bit
  us (first: the dead-due pass returned no classifications). **Treat it as a
  standing rule, not a one-off:** an LLM that can answer "nothing" for the whole
  batch eventually will.
- **Fix — the documented principle: pre-compute in Python, make the LLM a
  per-item validator, never a bulk generator.**
  1. Deterministic pre-ranking in `reprioritize.build_candidates`: demotions are
     limited to the weakest `min(2*cap, overflow)` cards per over-target list via a
     Python `_weakness` score (no must-do label / no near due / no High workstream /
     days idle); promotions are pre-filtered to cards with ≥1 code-verified signal.
  2. The judge receives that shortlist with per-card facts and MUST return one
     verdict per candidate — `move` or `keep` — so "do nothing" is an explicit,
     per-card choice, not a silent empty array.
  3. Candidates the model omits are counted and **surfaced in the report's Health
     stats** ("Reprioritization: N moves against M overflow (… K unverdicted)"), so
     a silent zero is visible instead of invisible. Per-item-scaled token budget
     (120/candidate) prevents truncation.
  The existing code gate (signal verification, confidence floor, exemptions, cap)
  still filters the `move` verdicts unchanged. Regressions:
  `test_reprioritize.py::test_over_target_yields_demotion_end_to_end`,
  `::test_keep_everything_surfaces_silent_zero_in_health`,
  `::test_unverdicted_candidates_surfaced_in_health`.

## Reprioritization promotion signals never fired: label-name mismatch
- **Symptom:** the first live dry-run of the reprioritization pass proposed and
  executed zero P0/P1 promotions even though many Inbox/Next-Few-Days cards carry
  priority labels. The Today plan section rendered (Today 50/15) but no upward
  moves keyed on a priority label.
- **Root cause:** the spine's "Problem 5" prose names the labels `"P0. High"` and
  `"P1"`, so `priority_labels` shipped with exactly those keys. The live Fira board
  actually names them **`"P0 - High"`** and **`"P1 - Medium"`** (matching the
  `.env` keys `fira_p0_high_label_id` / `fira_p1_medium_label_id`). Signal
  verification is an exact `label_name in priority_labels` check, so the real
  labels never matched — the promotion signal could never verify.
- **Fix:** the `priority_labels` default now maps **both** spellings
  (`"P0. High"`/`"P0 - High"` → today, `"P1"`/`"P1 - Medium"` → next_few_days).
  Because `priority_labels` is config (never hardcoded), the mapping is auditable
  and Vijay can adjust it if the board relabels. Lesson: verify signal keys against
  the *live board's* label names, not the spec's prose spelling.

## Duplicates were archived as "redundant copies" instead of merged
- **Symptom:** a run produced zero merges while three identical "Connect with Mac
  on IC-to-account mapping" cards surfaced as in-scope "no longer needed" archive
  proposals whose reasons said "duplicate/redundant". Archiving a redundant copy
  discards it without consolidating its text into a survivor — the exact data loss
  the merge invariant exists to prevent.
- **Root cause:** the merge>archive precedence was only enforced via
  `skip_ids=merge_claimed` — cards the cluster pass tagged as a duplicate cluster.
  When the cluster pass missed the duplicates (zero merges), nothing stopped the
  in-scope archive pass from classifying them as "no longer needed / redundant",
  and there was no guard rejecting a duplicate-shaped archive reason. The archive
  prompt also never told the model that duplicates belong to the merge pass.
- **Fix:** enforced in code, not just the prompt. `execute.is_duplicate_archive_reason`
  matches duplicate/redundant/copy reasons, and `execute_inscope_archive` now skips
  (never archives, never proposes) any verdict with such a reason, logging a note;
  the pipeline also excludes those cards from `archive_claimed` so they stay
  eligible to merge. `prompts/inscope_archive.md` now explicitly excludes duplicates.
  Regressions: `test_inscope_archive.py::test_inscope_archive_skips_duplicate_reasoned_verdict`
  and `test_e2e.py::test_e2e_duplicate_reasoned_archive_dropped_without_merge`.

## Obsolete in-scope cards were date-fixed instead of archived
- **Symptom:** a dry-run queued four "[Owner: Marah/Matt/Hunter]" cards for
  due-date fixes. Those are delegated/handed-off items — archive candidates — so
  spending a date fix on them is wrong and leaves the board cluttered.
- **Root cause:** the "no longer needed" archive test ran only on the recovery
  batch (Scratch lists), never on in-scope lists, so obsolete in-scope cards fell
  through to the date/label passes.
- **Fix:** in-scope cards are now evaluated against the test each run — `[Owner:]`
  titles deterministically (`guardrails.is_owner_titled`) plus an LLM pass for
  Done-workstream / passed-deadline cards — and matches are archived (governed by
  `tier1_recovery_archive` + `auto_min_confidence`, capped by
  `max_inscope_archives_per_run`). A strict per-card precedence (merge > archive >
  date/label/title) means a merged/archived card gets no other fix that run.
  Regression: `test_inscope_archive.py::test_inscope_owner_card_archived_not_date_fixed`.

## Date fixes were not label-neutral
- **Root cause:** the dead-due handler added the `Agent: Auto-Updated` label when
  clearing/re-dating a due date, coupling a date change to a label change.
- **Fix:** date fixes now touch only the due date (old date noted in a comment)
  and never add or remove a label. Regression:
  `test_due.py::test_date_handler_is_label_neutral_on_clear`.

## Stale "must do" labels were removal-only
- **Root cause:** a stale time-based label was always removed, even when its
  workstream was still active and time-sensitive — losing a legitimate signal.
- **Fix:** three-way disposition — archive (if no longer needed), swap to the
  matching tier (`2. Next Few Days` / `3. This Week`) when the workstream is
  Active AND Time-sensitive on the spine, else remove. Requires new spine
  `Priority` / `Time-sensitive` workstream attributes (default Normal / No).

## Pre-fix dry-runs contaminated the diff → phantom "rejections" at go-live
- **Symptom:** a clean dry-run reported "Rejections detected this run: 4" even
  though no live agent action had ever happened, so there was nothing to reject.
- **Root cause:** dry-runs *before* the persistence-gating fix wrote their
  simulated end-state to SQLite — an 845-row `snapshots` row plus 12 `actions`,
  1 `proposals`, and recovery/archive ledger rows — while the board was never
  changed. The next run's `detect_implicit_rejections` diffed the unchanged board
  against that simulated snapshot and recorded actions and read three merges
  (losers "in" the archive ledger/snapshot but still in their original board
  lists → pull-back) plus one stale `open` proposal (whose card never got the
  `Agent: Proposed` label → read as label-removed) as four user reversals.
- **Fix:** (1) dry-run already persists nothing now; (2) added
  `storage.reset_state` + `main.py --reset-state` to wipe all run/diff state
  (snapshots, actions, proposals, rejections, ledgers, kv) before go-live —
  deletes only agent state, never board data; (3) ran it to clear the real db.
  Regression: `test_state_hygiene.py::test_dry_run_then_next_run_detects_zero_rejections`.
- **Also corrected:** an earlier status message claimed the weekly spine-review
  card was "already open." It was not — no card exists on the board (creation is
  gated behind `not dry_run` and no live run has occurred). The real reason the
  reminder was skipped was the contaminated `kv.last_reminder_week` marker a
  pre-fix dry-run persisted; clearing state restores "would create" on dry-run.

## _post_snapshot dropped desc/label-name/due → phantom rejections after live actions
- **Root cause:** the post-run snapshot replayed the mutator log but only updated
  name, label **ids**, list, and clear-due. It ignored `set_description`,
  `set_due`, and the label **names** the snapshot actually stores. After a real
  merge/rename/relabel the saved snapshot therefore disagreed with the board on
  desc/labels, and the next run misread that as a user edit — a phantom rejection
  that would permanently suppress a legitimate action at go-live.
- **Fix:** `_post_snapshot` now applies `set_description`, `set_due`, and maps
  label ids → names on `set_labels` (BoardMutator.set_description records the
  desc). Regression: `test_state_hygiene.py::test_post_snapshot_reflects_desc_labels_due`.

## Dead-due classification starved in the shared hygiene call
- **Symptom:** the live dry-run escalated all 8 overdue in-scope cards as "Not
  classified this run" — the hygiene LLM returned renames but no `due_status`.
- **Root cause:** renames and dead-due classification shared one LLM call with a
  2000-token budget; on a large in-scope set the model spent the budget on
  renames and truncated/omitted the due classifications, so every dead-due card
  fell to the escalation fallback. Additionally, the due loop acted on any card
  merely present in `dead_due_ids`, so a rename verdict for a dead-due card could
  clear its date.
- **Fix:** dead-due classification is now its own LLM pass (`prompts/due.md`,
  `judge.classify_due`) with a per-card-scaled budget (`min(8000, max(3000, 120·N))`)
  that requires a `due_status` per card. The due loop now acts only on entries
  carrying an explicit due decision; unclassified dead-dues still escalate (the
  fallback, now reserved for genuinely unclassifiable cards). Regressions:
  `test_llm_schema.py::test_classify_due_is_own_call_and_classifies_every_card`,
  `test_due.py::test_rename_verdict_does_not_clear_dead_due_date`.

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
