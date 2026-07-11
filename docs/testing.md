# testing.md — work-todo-trello-grooming-agent

Test plan locked before implementation per the PDLC in `CLAUDE.md`. This is the
input for generating fixture data files and the pytest suite in a single pass.
Authoritative behavior source: `docs/design.md`; parameters: `docs/config.md`.

---

## Testing strategy

Per the global and project standards:

- **All external dependencies are mocked.** Trello API, the Anthropic LLM, the
  Ollama fallback, Notion (spine), and Gmail SMTP are never called. There are no
  network calls in the suite.
- **The whole suite runs in under 5 seconds.**
- **Determinism.** No wall-clock reliance: "now" is injected (a fixed reference
  timestamp fixture), so window math (`no_touch_hours`, `quarantine_days`,
  `proposal_timeout_days`, `dead_due_days`, `optimistic_label_days`) is
  reproducible. `zoneinfo` is not used (crashes on this stack); the stdlib
  UTC-offset helper is fed `local_tz_offsets` and its DST branch is exercised
  directly.
- **Test granularity:** unit (single function), integration (phase interaction),
  edge case (boundary conditions). Every branching condition in business logic
  gets at least one positive and one negative test.
- **Naming:** `test_{function}_{scenario}_{expected_result}`
  (e.g., `test_dead_due_within_window_left_alone`).
- **The mutation boundary is the assertion surface.** Trello write methods
  (create/update/move/label/comment/archive) are mock objects; guardrail and
  dry-run tests assert on whether — and how many times — they were called, never
  on a live board.
- **Layered LLM mock:** the LLM client is replaced by a stub returning canned
  structured-JSON responses from fixture files, so Phase 3 output is fixed and
  Phase 4 (validation + execution) is tested against known inputs. A separate set
  of malformed canned responses drives schema-validation tests. `TimeoutError`
  (builtin) and the library's exception types are both simulated to verify the
  fallback path.

Every major workflow (dedup merge, hygiene, recovery) also gets at least one
happy-path end-to-end test that runs mock data through the full five-phase
pipeline.

---

## Fixture design

### Primary fixture: anonymized Trello board export

A single JSON file (`tests/fixtures/board_export.json`) shaped like a Trello
board REST payload, anonymized from the real 2026-07-06 Fira export (names,
descriptions, and people replaced with synthetic equivalents; structure and
edge cases preserved). Required shape:

```
{
  "id": "<board id>",
  "shortLink": "RwdXsia3",
  "lists": [
    { "id": "<list id>", "name": "Today", "closed": false, "pos": 1 },
    { "id": "...", "name": "Inbox / Triage", ... },
    { "id": "...", "name": "Next Few Days", ... },
    { "id": "...", "name": "This Week", ... },
    { "id": "...", "name": "Agent: Merged/Removed", ... },      // quarantine
    { "id": "...", "name": "Scratch 6-24", ... },
    { "id": "...", "name": "Scratch 6-3", ... },
    { "id": "...", "name": "ARCHIVE Scratch 5-12", ... },       // excluded by rename
    { "id": "...", "name": "Archive - April 12", ... },         // out of scope (no ^Scratch)
    { "id": "...", "name": "Scratch 7-1", ... }                 // new sweep, auto-included
  ],
  "labels": [
    { "id": "...", "name": "1. Today (must do)", "color": "red" },
    { "id": "...", "name": "2. Next Few Days (must do)", ... },
    { "id": "...", "name": "3. This Week (must do)", ... },
    { "id": "...", "name": "Agent: Auto-Updated", ... },
    { "id": "...", "name": "Agent: Proposed", ... },
    { "id": "...", "name": "Colin", ... },                      // person labels
    { "id": "...", "name": "Logan", ... }
  ],
  "cards": [
    {
      "id": "<card id>",
      "idList": "<list id>",
      "name": "...",
      "desc": "...",
      "due": "2026-06-01T00:00:00.000Z",   // or null
      "dateLastActivity": "2026-06-20T...",
      "idLabels": ["...", "..."],
      "labels": [ { "name": "Colin" }, ... ],
      "badges": { "attachments": 0, "checkItems": 0 },
      "shortLink": "abc123",
      "closed": false
    }
  ],
  "comments": [   // Trello "commentCard" actions, per card
    { "id": "...", "idCard": "...", "text": "yes", "date": "2026-07-10T...", "memberName": "Vijay" }
  ]
}
```

The fixture must include, by construction, at least one card exercising each of
these conditions (so tests can select cards by intent, not by hardcoded id):

| Condition | Purpose |
|---|---|
| Exact-name duplicate pair, both in edit scope | Tier 1 merge |
| Near-exact duplicate pair (typo variant) | Tier 1 merge |
| Duplicate pair with **different person labels** | forced Tier 2 |
| Duplicate pair with **conflicting due dates** | forced Tier 2 |
| Duplicate cluster whose best survivor is in a Scratch list (outside edit scope) | forced Tier 2 |
| Losing card carrying an **attachment** and one with a **checklist** | forced Tier 2 (§5.3) |
| `related-but-distinct` pair | cross-link, no merge |
| Card overdue > 14 days | dead-due clear (Tier 1) |
| Card overdue 1–13 days | left alone |
| Time-based label on card not in matching list, applied > 7 days ago | stale-label removal |
| Time-based label applied < 7 days ago | not removed |
| Name containing a pipe `\|` | heuristic-flagged rename |
| Name shorter than 4 chars, and one longer than 100 chars | heuristic-flagged (length bounds) |
| Entirely-lowercase name | heuristic-flagged |
| Name with a consecutive double space, and one with leading/trailing whitespace | heuristic-flagged |
| Wide-track pair sharing one entity keyword AND one person label but low Jaccard | entity+person block path |
| Wide-track pair below `wide_block_jaccard` with no shared entity+person | blocking negative case |
| Card edited by Vijay < 12h before "now" | never-touch |
| The "Grooming Report" card | never-touch |
| Card with an open `Agent: Proposed` label | never-touch |
| `Agent: Proposed` card with a "yes"/"approve" comment | approval parsing |
| `Agent: Proposed` card with a "no" comment / label removed | rejection parsing |
| `Agent: Proposed` card older than `proposal_timeout_days` | proposal timeout |
| `Agent: Auto-Updated` card Vijay has since edited | implicit rejection |
| `Agent: Auto-Updated` label older than 7 days, untouched | label expiry |
| Quarantined card untouched ≥ 7 days | auto-archive |
| Quarantined card dragged back to a list | quarantine pull-back rejection |
| Scratch cards spanning newest→oldest lists for recovery ordering | recovery batch |
| ≥ 4 recovery cards judged Today-worthy | `recovery_today_max` cap → NFD demotion |

### Supporting fixtures

| Fixture | Shape / purpose |
|---|---|
| `prior_snapshot.sql` / seeded SQLite | Rows in `snapshots`, `actions`, `proposals`, `rejections`, `recovery_ledger` (per design §7.1) representing "last run," so the diff (Phase 1) has something to diff against. |
| `rejection_ledger.json` | Pre-existing fingerprints to test "never re-proposed" consultation (§6). |
| `recovery_ledger.json` | Already-processed scratch card ids to test "nothing re-triaged." |
| `spine.json` | Stub Notion spine: workstreams with statuses (incl. one `Done` for archive-candidate routing), a **People** section (names appended to `entity_keywords_seed` at runtime), and notes. |
| `llm_responses/*.json` | Canned **valid** structured responses for cluster adjudication, hygiene, and recovery triage (drives Phase 4). |
| `llm_responses_malformed/*.json` | Markdown-fenced JSON, trailing prose, invalid JSON, items referencing unknown card ids, and a name referencing an invented person — for schema-validation and name-validation tests. |
| `now.txt` / constant | The fixed reference "now" injected everywhere for window math. |
| `agent_config.test.json` | A copy of the example config with a known reference date, used for all tests unless a test overrides a field (e.g., flipping `tier1_stale_label_removal`). |

---

## Test case table

Each row is one or more pytest cases. "Validation" is how the expected result is
checked (string comparison, call-count assertion, DB-row assertion, etc.).

### 1. Snapshot & diff — implicit rejection detection (§3 Phase 1)

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_diff_user_edited_touched_card_records_rejection` | Card had `Agent: Auto-Updated` last run; name/desc changed since | Rejection row written (source=`edit`); `Agent: Auto-Updated` removed; card treated as final (not re-proposed) | DB-row assert on `rejections`; Trello remove-label call asserted |
| `test_diff_manual_label_removal_treated_as_rejection` | Vijay removed `Agent: Auto-Updated` himself | Rejection recorded identically; action not re-proposed | DB-row assert; rejection fingerprint present |
| `test_diff_quarantine_pullback_records_rejection` | Card moved out of `Agent: Merged/Removed` since last run | Rejection recorded (source=`edit`/pull-back); card not re-quarantined | DB-row assert; no re-move call |
| `test_diff_unchanged_touched_card_no_rejection` | Touched card untouched by Vijay | No rejection; label allowed to age toward expiry | DB negative assert |
| `test_diff_new_scratch_list_added_to_recovery_scope` | New `Scratch 7-1` list present | Recovery source set includes it automatically | scope resolution assert |
| `test_diff_fingerprint_matches_action_and_sorted_card_ids` | Rejection fingerprint composition | fingerprint = action type + sorted card ids (+ new name for renames) | string-equality on fingerprint |

### 2. Comment-based approval parsing on `Agent: Proposed` (§4 Tier 2)

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_approval_comment_yes_executes_proposed_action` | Comment "yes" on a Proposed card | Proposed action executed this run; proposal status → `approved` | Trello mutation call asserted; DB `proposals.status` |
| `test_approval_comment_approve_executes` | Comment "approve" (case-insensitive) | Executed | call assert |
| `test_approval_comment_no_rejects` | Comment "no" | No execution; status → `rejected`; fingerprinted | negative call assert; DB assert |
| `test_approval_label_removed_rejects` | `Agent: Proposed` label removed | Treated as rejection | DB assert |
| `test_approval_unrelated_comment_ignored` | Comment "thanks, will look later" | No execution; proposal stays `open` | negative assert |
| `test_proposal_timeout_expires_and_fingerprints` | Proposal older than `proposal_timeout_days` (7) | Summarized in report, status → `expired`, fingerprinted as rejected | DB assert; report content assert |
| `test_proposal_timeout_independent_of_quarantine_days` | `proposal_timeout_days` set != `quarantine_days` | Proposal timeout uses `proposal_timeout_days` only | boundary assert with divergent values |

### 3. Duplicate candidate blocking (§2 Phase 2, deterministic, no LLM)

Jaccard is computed on normalized name tokens (lowercased, punctuation stripped,
stopwords removed).

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_narrow_track_passes_all_in_scope_names` | in-scope card set | All in-scope names handed to LLM wholesale (no pre-filter drops) | count assert on LLM input |
| `test_narrow_track_preclusters_hint_at_narrow_hint_jaccard` | Two in-scope cards with Jaccard ≥ `narrow_hint_jaccard` (0.4) | Grouped into a hint cluster | cluster membership assert |
| `test_narrow_track_below_hint_jaccard_not_clustered` | Two in-scope cards with Jaccard < 0.4, no shared person | Not pre-clustered (still sent wholesale) | negative cluster assert |
| `test_wide_track_blocks_pair_at_wide_block_jaccard` | Scratch vs in-scope pair with Jaccard ≥ `wide_block_jaccard` (0.5) | Pair sent to LLM | LLM-input membership assert |
| `test_wide_track_blocks_shared_entity_plus_person_low_jaccard` | Shares ≥1 entity keyword AND ≥1 person label, Jaccard below threshold | Blocked/sent anyway | assert |
| `test_wide_track_entity_only_not_blocked` | Shares an entity keyword but no person label, low Jaccard | **Not** sent | negative assert |
| `test_wide_track_below_threshold_not_sent` | Jaccard below 0.5, no shared entity+person | Pair **not** sent to LLM | negative membership assert |
| `test_spine_person_names_appended_to_entity_keywords` | Spine People section has names | Blocking uses seed keywords **plus** spine person names | keyword-set assert |
| `test_weekly_sweep_on_sunday_sends_all_open_names` | Run day == `weekly_sweep_day` | Full-board name list sent (semantic sweep) | LLM-input assert |
| `test_weekly_sweep_skipped_off_day` | Non-sweep day | No full-board sweep call | negative assert |

### 4. Tier assignment — every forced-tier rule (§4 table + §5.3)

Each row: LLM proposes an action; Python applies the deterministic floor/ceiling.

| Test | Rule (§4/§5.3) | Expected tier |
|---|---|---|
| `test_tier_exact_name_merge_forced_tier1` | Exact/near-exact name-match merge | Tier 1 |
| `test_tier_rename_forced_tier1` | Rename / description restructuring | Tier 1 |
| `test_tier_desc_restructure_forced_tier1` | Description restructuring only | Tier 1 |
| `test_tier_merge_diff_person_labels_forced_tier2` | Merge across different person labels | Tier 2 |
| `test_tier_merge_conflicting_due_forced_tier2` | Merge with conflicting due dates/owners | Tier 2 |
| `test_tier_merge_survivor_outside_edit_scope_forced_tier2` | Survivor lives outside edit scope | Tier 2 |
| `test_tier_merge_loser_has_attachment_forced_tier2` | Losing card has an attachment | Tier 2 |
| `test_tier_merge_loser_has_checklist_forced_tier2` | Losing card has a checklist | Tier 2 |
| `test_tier_recovery_archive_default_tier2` | Recovery archive proposal, `tier1_recovery_archive` = false | Tier 2 |
| `test_tier_recovery_archive_tier1_when_flag_set` | Recovery archive proposal, `tier1_recovery_archive` = true | Tier 1 |
| `test_tier_stale_label_removal_default_tier2` | Stale label removal, `tier1_stale_label_removal` = false | Tier 2 |
| `test_tier_stale_label_removal_tier1_when_flag_set` | Stale label removal, `tier1_stale_label_removal` = true | Tier 1 |
| `test_tier_card_edited_within_12h_never_touched` | Card edited by Vijay < `no_touch_hours` | No action at all (neither tier) |
| `test_tier_llm_high_confidence_cannot_override_forced_tier2` | LLM says Tier 1 but a forced-Tier-2 condition holds | Python floor wins → Tier 2 |

### 5. Guardrails (§6, enforced in Python)

| Test | Guardrail | Expected result | Validation |
|---|---|---|---|
| `test_cap_max_merges_per_run_enforced` | `max_merges_per_run` (10) | 11th merge not executed; deferred | executed-merge count == 10 |
| `test_cap_max_renames_per_run_enforced` | `max_renames_per_run` (15) | ≤ 15 renames executed | count assert |
| `test_cap_max_renames_prioritizes_flagged_names` | More rename candidates than the cap, mix of heuristic-flagged and LLM-only | Heuristic-flagged names renamed first; non-flagged deferred | ordering/selection assert |
| `test_cap_max_recoveries_per_run_enforced` | `max_recoveries_per_run` (15) | ≤ 15 recoveries | count assert |
| `test_cap_max_proposals_open_stops_generation` | `max_proposals_open` (20) | No new Tier 2 proposals generated once 20 open | negative generation assert |
| `test_merge_invariant_survivor_contains_all_source_text` | Merge content invariant | Survivor desc contains every source card's original name **and** desc (string containment) before any loser is quarantined | `assert name in survivor_desc and desc in survivor_desc` |
| `test_merge_invariant_violation_blocks_move` | Survivor desc missing a source fragment | Merge aborted; **no** card moved to quarantine; logged | negative move-call assert |
| `test_no_touch_window_blocks_edit` | Card edited < 12h ago | Card skipped entirely (no rename/merge/label) | negative mutation assert |
| `test_never_touch_grooming_report_card` | Grooming Report card | Never edited/merged/moved | negative assert |
| `test_never_touch_open_proposed_card` | Card with open `Agent: Proposed` | Not edited during hygiene/dedup | negative assert |
| `test_never_touch_out_of_scope_card` | Card outside edit scope | No mutation applied there | negative assert |
| `test_rejection_ledger_consulted_before_proposal` | Fingerprint already in ledger | Proposal/action suppressed (never re-proposed) | negative generation assert |
| `test_llm_name_validation_rejects_invented_entity` | LLM name references a person/entity not in sources or spine | Name rejected; falls back to safe title; logged | string/name-source assert |

### 6. Name-quality heuristics (§5.1, prioritization filter)

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_name_flag_pipe_character` | Name contains `\|` | Heuristic-flagged | flag assert |
| `test_name_flag_too_short` | Name length < `name_min_length` (4) | Flagged | flag assert |
| `test_name_flag_too_long` | Name length > `name_max_length` (100) | Flagged | flag assert |
| `test_name_flag_all_lowercase` | Entirely lowercase name | Flagged | flag assert |
| `test_name_flag_double_space` | Consecutive double space | Flagged | flag assert |
| `test_name_flag_leading_trailing_whitespace` | Leading/trailing whitespace | Flagged | flag assert |
| `test_name_clean_title_not_flagged` | Well-formed mixed-case title | Not flagged | negative assert |
| `test_name_llm_nominated_rename_beyond_flagged_allowed` | Clean name the LLM still nominates to rename | Rename permitted (filter is not a gate) | positive assert |

### 7. Quarantine lifecycle (§3, §4, §5.3)

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_merge_moves_loser_to_quarantine_not_archive` | Executed merge | Loser moved to `Agent: Merged/Removed` with a comment linking survivor; **not** archived | move-call + comment-call assert |
| `test_auto_updated_label_expires_after_quarantine_days` | Label untouched > 7 days | Label stripped | remove-label call assert |
| `test_auto_updated_label_within_window_kept` | Label 6 days old | Label retained | negative assert |
| `test_quarantined_card_auto_archives_after_7_days` | Quarantined, untouched ≥ 7 days | Card archived (only path to archive is via quarantine) | archive-call assert |
| `test_quarantined_card_within_window_not_archived` | Quarantined 6 days | Not archived | negative assert |
| `test_quarantine_pullback_cancels_archive` | Card pulled back before 7 days | Not archived; rejection recorded (ties to §1) | negative archive assert + DB assert |
| `test_nothing_hard_deleted_ever` | Any lifecycle path | No delete call is ever issued | global negative assert on delete |

### 8. Recovery scope resolution (§5.2)

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_recovery_includes_scratch_lists` | `^Scratch` include | `Scratch 6-24`, `Scratch 6-3`, `Scratch 7-1` in source set | set assert |
| `test_recovery_excludes_archive_renamed_list` | `ARCHIVE Scratch 5-12` | Excluded by `^ARCHIVE` despite matching `^Scratch` | set-membership negative assert |
| `test_recovery_excludes_non_scratch_april_archive` | `Archive - April 12` | Excluded (no `^Scratch` match) | negative assert |
| `test_recovery_new_scratch_list_auto_included` | New `Scratch 7-1` | Auto-included without config change | assert |
| `test_recovery_orders_newest_list_first` | Multiple scratch lists | Batch pulled newest list first | ordering assert |
| `test_recovery_ledger_prevents_retriage` | Card id already in `recovery_ledger` | Not re-selected into batch | negative selection assert |

### 9. Recovery disposition routing (§3 Phase 3, §5.2)

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_recovery_route_to_today_when_spine_supports` | Spine clearly supports Today | Card moved to Today (Tier 1) with origin comment | move + comment assert |
| `test_recovery_today_cap_demotes_overflow_to_nfd` | 4 spine-supported-to-Today cards, `recovery_today_max`=3 | 3 routed to Today; the 4th routed to `Next Few Days`; Grooming Report notes it was demoted by the cap | Today-move count == 3; NFD-move assert; report content assert |
| `test_recovery_route_to_inbox_when_ambiguous` | Ambiguous context | Card routed to `Inbox / Triage` (default) | move assert |
| `test_recovery_merge_into_active_card` | Clusters with an active card | Merge disposition (flows through merge path/invariant) | merge-call assert |
| `test_recovery_propose_archive_default_tier2` | Clearly obsolete (Done workstream), `tier1_recovery_archive`=false | `propose-archive`, Tier 2 | tier assert |
| `test_recovery_routed_card_gets_origin_comment` | Any routed card | Comment noting origin list added | comment-text assert |
| `test_recovery_respects_max_recoveries_cap` | > cap dispositioned to route | Executed routings ≤ `max_recoveries_per_run` | count assert |

### 10. Dry-run mode (§6)

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_dry_run_zero_board_mutations` | `--dry-run` (or `dry_run: true`), full pipeline | Every Trello **write** method (create/update/move/label/comment/archive/delete) called 0 times | call-count == 0 across all mutation mocks |
| `test_dry_run_writes_report_file` | Dry run | Report written to local file; report card optional | file-exists / content assert |
| `test_dry_run_still_reads_and_computes` | Dry run | Snapshot, diff, candidate gen, and (mocked) LLM judgment all still run | positive read/compute asserts |
| `test_live_run_applies_mutations` | `dry_run: false` | Approved Tier 1 actions issue Trello writes | positive call assert (contrast case) |

### 11. LLM output schema validation (§3, CLAUDE.md LLM standards)

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_llm_valid_json_parsed` | Well-formed structured response | Parsed into typed objects; executed | object-shape assert |
| `test_llm_markdown_fenced_json_recovered` | JSON wrapped in ```json fences | Fences stripped, parsed successfully | parse assert |
| `test_llm_trailing_prose_json_recovered` | JSON with extra text before/after | JSON extracted and parsed | parse assert |
| `test_llm_invalid_json_dropped_and_logged` | Unparseable response | Item dropped, raw response logged, nothing partially applied | negative execution assert + log assert |
| `test_llm_unknown_card_id_item_dropped` | Item references a card id not in the board | That item dropped and logged; others kept | selective-drop assert |
| `test_llm_partial_batch_valid_items_kept` | Mixed valid/invalid items in one batch | Valid items executed, invalid dropped | mixed assert |
| `test_llm_name_not_in_source_rejected` | Merged/renamed name introduces an entity absent from sources/spine | Name rejected (ties to §5 name validation) | name-source assert |
| `test_llm_timeout_falls_back_to_ollama` | Primary raises builtin `TimeoutError` and library exception | Fallback (`qwen3:8b`) invoked with the **same unfiltered** inputs | fallback-call assert + input-equality assert |

### 12. Time / timezone helper

| Test | Scenario | Expected result | Validation |
|---|---|---|---|
| `test_window_math_uses_local_tz_offsets_standard` | "now" in standard-time part of year | UTC-offset helper applies `local_tz_offsets.standard` (-5) | computed-offset assert |
| `test_window_math_uses_local_tz_offsets_daylight` | "now" in daylight-time part of year | Helper applies `local_tz_offsets.daylight` (-4) | computed-offset assert |
| `test_no_zoneinfo_dependency` | Import/refactor guard | Timezone handling uses the stdlib offset helper, not `zoneinfo` | import assert |

### 13. End-to-end happy paths (one per major workflow)

| Test | Workflow | Expected result |
|---|---|---|
| `test_e2e_dedup_merge_pipeline` | Two exact duplicates in edit scope → merge | Survivor updated (name+consolidated desc), loser quarantined w/ link, `Agent: Auto-Updated` applied, invariant satisfied, report lists the action |
| `test_e2e_hygiene_pipeline` | Overdue-14d + pipe-in-name card | Due cleared (original in comment), name cleaned with `Original title:` preserved, Tier 1 label applied |
| `test_e2e_recovery_pipeline` | Newest scratch batch triaged | Cards routed per disposition, origin comments added, ledger updated, Today cap respected with NFD demotion noted |
| `test_e2e_run_report_generated` | Full run | Single "Grooming Report" card at top of `report_list` summarizing auto-applied actions, open proposals, quarantine + days remaining, rejections, health stats, Tier 2 approval rates (input for flipping the tiering toggles), and cap-demoted recoveries |
| `test_e2e_three_failures_autopause` | Three consecutive failed runs | Agent auto-pauses (`auto_pause_after_failures`); alert sent via mocked Gmail SMTP |

---

## Coverage guarantees

- **Every forced-tier rule** in design §4 (plus the §5.3 attachment/checklist
  rule) has a dedicated test in §4 above, including both states of the
  `tier1_stale_label_removal` and `tier1_recovery_archive` toggles.
- **Every guardrail** in design §6 has a dedicated test in §5 above, including the
  merge string-containment invariant (positive and violation cases) and the
  heuristic-flagged rename priority under `max_renames_per_run`.
- **Every branching disposition** in recovery triage (§9) and every diff-detected
  rejection source (§1) has positive and negative cases, including the
  `recovery_today_max` overflow → Next Few Days demotion.
- **Blocking** covers both the Jaccard threshold and the entity+person path
  (positive and negative), plus spine-person-name appending.
- **Dry-run** is asserted at the mutation boundary: zero writes across all Trello
  write mocks.
- **LLM robustness** covers fences, trailing prose, invalid JSON, unknown ids,
  invented names, and the builtin-`TimeoutError` fallback path.
- **Timezone** handling is exercised across the DST boundary using
  `local_tz_offsets`, with a guard against reintroducing `zoneinfo`.
