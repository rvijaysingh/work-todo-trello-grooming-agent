# work-todo-trello-grooming-agent — Design Specification

**Version:** 1.1 (P0 design, all open questions resolved; ready for docs lock)
**Date:** 2026-07-06
**Board:** Fira (work), board shortlink `RwdXsia3`
**Repo:** `rvijaysingh/work-todo-trello-grooming-agent`

---

## 1. Purpose and Problem Statement

The Fira work board has become a capture surface rather than a working system. Analysis of the 2026-07-06 export (772 open cards, 18 open lists) shows three compounding problems:

1. **Duplicates fragment context.** The same task exists as multiple cards (Granola-agent re-pushes, manual re-captures, earlier versions swept into Scratch lists). Each copy holds a different slice of context (a description here, a person label there), so no single card is trustworthy.
2. **A large buried backlog.** The recurring "sweep Today into a dated Scratch list" reset has left ~480 cards across Scratch and Archive lists, untouched for 30–53 days, including cards still labeled must-do. Relevant work is hidden in there.
3. **Per-card signal decay.** 14 of 15 due dates are overdue. "1. Today (must do)" sits on 104 open cards. Names are often cryptic or typo'd ("Set yo time Colin", "Hvac aC"). Signals no longer distinguish anything.

The agent's job in P0 is to make the active card set trustworthy and manageable: one card per task, clean names and metadata, and a steady drain of the buried backlog — with every action reviewable and reversible inside Trello.

## 2. P0 Scope

| Dimension | Scope |
|---|---|
| Board | Fira only (single-board, purpose-built) |
| **Edit scope** (hygiene + dedup merges applied here) | `Today`, `Inbox / Triage`, `Next Few Days` |
| **Comparison scope** (dedup candidates detected across) | Edit scope + `This Week` + all `Scratch*` / `Archive*` lists |
| **Recovery scope** (read-only source for recovery) | Lists whose name starts with `Scratch`, excluding any list renamed with an `ARCHIVE` prefix. Vijay controls scope per list by renaming (e.g., `ARCHIVE Scratch 5-12` excludes it). The two April `Archive` lists (>2 months old) are out of scope for now. Both patterns configurable. |
| P0 capabilities | (a) Hygiene: due dates, labels, card names, descriptions. (b) Scratch recovery. (c) Duplicate detection and merge. |
| Explicitly out of P0 | Routing/prioritization of non-duplicate cards, Today right-sizing, staleness demotion, semantic embeddings (ChromaDB), calendar/email context, card splitting, cross-board anything, rule learning |

When a duplicate cluster spans edit scope and an out-of-scope list, detection still happens; execution rules in §5.3 govern what is auto-applied vs proposed.

## 3. Core Loop

One scheduled run per day (Windows Task Scheduler, 6:30 AM, Python 3.13). Five phases:

### Phase 1 — Snapshot & Diff
Pull the full board via Trello API. Persist a snapshot to SQLite. Diff against the last post-run snapshot to detect:

- **Implicit rejections:** any card the agent touched last run that Vijay has since edited, moved, relabeled, or pulled out of quarantine. Recorded in the rejection ledger (proposal fingerprint + card ids) so the same action is never re-proposed. The `Agent: Auto-Updated` label is removed from any card Vijay edited — his version is final.
- **Approvals on proposals:** comments on `Agent: Proposed` cards (a reply of "yes" / "approve" executes the proposed action this run; see §4).
- **New Scratch sweeps:** new lists matching the scratch pattern are added to recovery scope automatically.
- **Label expiry / archive lifecycle:** `Agent: Auto-Updated` labels older than `optimistic_label_days` are stripped; cards in the `Agent Archive` list older than `archive_list_days` (60, from the SQLite-tracked entry time) move to Trello's built-in (restorable) archive.
- **Notion live config:** the spine's "Rules and thresholds" section is parsed and (for valid values) overrides `agent_config.json` for this run; invalid/unknown keys are noted in the report; a missing section or unreachable spine falls back to the file.
- **Weekly spine reminder:** on the first run on/after `spine_review_day`, a "Review agent spine…" card is created at the top of Today (unless already open).

### Phase 2 — Candidate Generation (Python, deterministic)
No LLM calls in this phase. Facts are pre-computed and passed to the LLM as constraints, per the standing LLM-integration standard.

- **Duplicate candidates, narrow track (in-scope lists):** all in-scope card names + key metadata are compact enough (~140 cards) to hand to the LLM wholesale. Python still pre-clusters via normalized token overlap, shared person labels, and shared entity keywords (seeded from label names and spine workstream names) — these clusters are passed as *hints*, and the LLM may nominate additional clusters it sees in the full list.
- **Duplicate candidates, wide track (vs Scratch/Archive):** for the ~600 out-of-scope cards, Python blocking only (token overlap ≥ threshold, or shared entity + person label). Only blocked candidates are sent to the LLM. A weekly full-board name sweep (Sunday run, LLM reads all open card names, ~15k tokens) catches semantic duplicates lexical blocking misses.
- **Recovery batch:** next `recovery_batch_size` (default 15) unprocessed cards from scratch/archive lists, newest list first (recency predicts relevance). Processed card ids tracked in SQLite so nothing is re-triaged.
- **Hygiene candidates:** in-scope cards with overdue due dates (> `dead_due_days` past), stale time-based labels, or names failing simple quality heuristics (length, all-lowercase fragments, pipe-separated segments, known-typo patterns).

### Phase 3 — LLM Judgment (Sonnet, batched, prompt-cached)
Batched calls sharing a cached prefix (Notion spine + board summary + rules): cluster adjudication, hygiene (names/descriptions), **dead-due classification** (its own call with a per-card-scaled token budget so a large overdue set is never truncated), **in-scope "no longer needed" archiving**, **stale-label disposition** (swap vs remove), and recovery triage. Model: `claude-sonnet-4-6` primary, Ollama `qwen3:8b` fallback receiving the same unfiltered inputs.

1. **Cluster adjudication:** for each candidate cluster: `duplicate` / `related-but-distinct` / `unrelated`; pick the survivor (most context, most recent activity, best list position); compose merged name + consolidated description; assign confidence tier per the bounded rules in §4.
2. **Hygiene pass:** cleaned names (typos fixed, verb-first where natural, person references preserved), description structuring, due-date clear/keep decisions, stale-label removals.
3. **Recovery triage:** per recovery-batch card: `route-to-active-list` (Today / Next Few Days / This Week when the spine clearly supports the destination; Today capped per run) / `route-to-Inbox/Triage` (default when context is ambiguous) / `merge-into-existing-card` (cluster with an active card) / `propose-archive` (clearly obsolete, e.g., Sales Summit logistics post-event), each with a one-line reason. Spine context is what lets this pass distinguish dead workstreams from live ones.

All LLM output is structured JSON, schema-validated in Python. Anything malformed or referencing unknown card ids is dropped and logged.

### Phase 4 — Validated Execution
Python enforces guardrails (§6) then executes per the confidence model (§4). All Trello writes go through `agent-shared-library`'s Trello module with retry/backoff. Order within the phase: merges → in-scope archiving → hygiene (renames, dead-due, label disposition) → recovery routing → **reprioritization** (§5.4). Reprioritization runs last so its Today / Next Few Days list-size targets apply to the already-cleaned board; its own LLM judgment call is made here (post-execution), not in Phase 3.

**Action vocabulary.** Every card comment and report line names its action with one fixed phrase (`vocab.py`): *Merge Duplicates / Move to Archive / Recover From Scratch / Rename Card / Fix Due Date / Update Time Label / Mark More Time-sensitive / Mark Less Time-sensitive*. Comment layout: `<Action>: <detail>. Reason: <one line>. Confidence: NN%.`

### Phase 5 — Run Report
One **"Grooming Report"** card pinned at the top of `Today`, replaced each run (single daily run at 6:30 AM). It opens with a **"Today plan"** section (reprioritization; §5.4) rendered **first** — current Today/Next Few Days counts vs targets ("Today: 23 cards, target 15 — 2 moved automatically, 6 proposed"; dry-run uses "would move"; empty state "Today: N cards (target 15) — no changes proposed."), each executed move with its code-verified signals, then one-line summaries of open reprioritization proposals (full entries live under "Awaiting your decision") — followed by these sections, each with a count in its header: **"Still overdue and possibly urgent"**, **"Awaiting your decision"** (open proposals), **"Recently archived"** (cards moved to `Agent Archive` this run and cards approaching their 60-day Trello-archive date), **"Done automatically"** (reprioritization moves excluded — they render only under Today plan), and **"Health stats"** (scratch backlog, hygiene coverage, Tier-2 approval rates, mode flags, Notion override notes). An **"At a glance"** line under the header totals the action counts and the Today-plan counts.

Rendering: every entry is numbered `#N` (restarting per section); related cards under one decision (e.g. a merge's cards) share a number with sub-letters `#Na/#Nb` and render consecutively; proposal groups sort by confidence, highest first. Each entry uses a fixed layout — line 1 `#N Card Name: <title>` plus `(due M/D h:mmam)` in local time (never ISO); line 2 the action line (`Action needed:` / `Proposed: … Reason: … Confidence: NN%. Expires M/D.` / `Did:` or dry-run `Would:` / `Reason:`); then indented `Card Labels:` and `Card Description:` (first ~12 words). No card URLs anywhere. **Archive moves appear only under "Recently archived"** (its lifecycle stated once in the section parenthetical, dry-run "would move"), never duplicated in "Done automatically", which renders labeled subsections (Date fixes / Label changes / Renames / Recovered from Scratch / Other). "Awaiting your decision" prints the answer instructions once under its header. Health stats show a day-over-day delta after each number when the prior run's value is known, noting any new Scratch list entering scope. The **pre-first-run reminder** renders only in dry-run. Failures alert via the shared alerting module (Gmail SMTP). Three consecutive failed runs auto-pauses the agent (same pattern as granola-to-notion).

## 4. Human-in-the-Loop Model

Undo-by-default rather than approve-by-default, and **automatic mode is the shipped default**: high-confidence actions execute immediately, but nothing is ever hard-deleted, and reviewing is a glance while undoing is a drag. Two agent labels total.

### Tier 1 — Auto-executed, labeled `Agent: Auto-Updated`
- **Content placement.** Only a rename touches the card body: it preserves the original as the first description line (`Original title: ...`). **Everything else the agent writes goes in card comments** — change explanations, old due dates, and the merge audit trail (links) — each ending with `Confidence: NN%`.
- **Merges.** The survivor's **description** holds task content only: its original title/text plus one `From: <name>` section per source card. The audit trail (links, "merged in N duplicates") is a **comment**, so the string-containment invariant never depends on audit metadata. Labels are **unioned across sources, excluding time-based labels** (`1. Today (must do)`, `2. Next Few Days (must do)`, `3. This Week (must do)`); the survivor keeps only its own time-based labels. Losing cards move to the **TOP of the single `Agent Archive` list** with a comment linking to the survivor.
- **Automatic categories** (each gated by a config flag, all shipped `true`): stale time-based label removal (`tier1_stale_label_removal`), recovery archiving (`tier1_recovery_archive`), and dead-due handling (`tier1_due_date_clear`). When the flag is `true` the action auto-executes with an explanatory comment; a **borderline/low-confidence** call (below `auto_min_confidence`) is downgraded to `Agent: Proposed`. When the flag is `false`, the whole category is proposed.
- **Renames** follow the "Card naming standard" section of the spine (falling back to the same hardcoded rules if that section is absent).

**Review mechanic:** passive. If something looks wrong, *just fix it* — revert the name, drag the archived card back to any list, re-add a label. The next run's diff detects the edit, treats Vijay's version as final, records a rejection fingerprint (never re-proposed), and removes `Agent: Auto-Updated` itself. Manually removing the label works too. Untouched `Agent: Auto-Updated` labels auto-expire after `optimistic_label_days`. Cards that sit in the `Agent Archive` list longer than `archive_list_days` (60) are moved to Trello's built-in **(restorable)** archive; entry timestamps are tracked in SQLite. Nothing is ever hard-deleted, and nothing reaches Trello's archive without passing through the `Agent Archive` list.

### Tier 2 — Flagged only, labeled `Agent: Proposed`
The agent makes no change. It adds the label plus a comment stating the recommended action, reason, and `Confidence: NN%` (from an extended LLM output schema).

- **Approve:** reply "yes"/"approve" on the comment (agent reads comments on `Agent: Proposed` cards at run start and executes), or simply do the action yourself.
- **Reject:** remove the label (or reply "no").
- **Timeout:** proposals older than `proposal_timeout_days` (14) are summarized in the run report, then dropped and fingerprinted as rejected.

### Tier assignment (LLM decides, Python bounds)
Deterministic floors/ceilings applied after the LLM's confidence call:

| Action | Forced tier |
|---|---|
| Exact / near-exact name match merges | Always Tier 1 |
| Renames and description restructuring | Always Tier 1 (loss-free by construction) |
| Merges across different person labels, or with conflicting due dates/owners | Always Tier 2 |
| Merges where survivor lives outside edit scope | Always Tier 2 |
| Stale-label / recovery-archive / dead-due actions | Tier 1 when the category flag is on **and** not borderline; else Tier 2 |
| Any auto-eligible action below `auto_min_confidence` or flagged borderline | Tier 2 (`Agent: Proposed`) |
| Cards Vijay edited within `no_touch_hours` (default 0 = disabled) | Never touched at all |

## 5. P0 Capabilities — Detail

### 5.1 (a) Hygiene: archiving, due dates, labels, names, descriptions

**Per-card action precedence: merge > archive > date/label/title fixes.** A card being merged (any card in a duplicate cluster) or archived receives no other fix that run.

- **In-scope archiving ("no longer needed"):** each run, in-scope cards (Today / Inbox/Triage / Next Few Days) are evaluated against the test — workstream marked Done (or Winding down) on the spine, an event/deadline passed with nothing left to do, or a card titled `[Owner: Name]` (the `[Owner:]` case is detected deterministically in Python; the rest by the LLM against the spine). Matches move to the TOP of the Agent Archive list, governed by `tier1_recovery_archive` + `auto_min_confidence` like all archives, capped at `max_inscope_archives_per_run` (10).
- **Dead due dates:** due dates more than `dead_due_days` (14) overdue are first **classified against the spine** (its own LLM pass). *Still-matters* → **never touched**, listed under "Still overdue and possibly urgent". *No-longer-matters* → a new date is set **only if one is actually written** in the card text or as a spine workstream deadline (the LLM cites the exact substring, verified in Python; never guessed); otherwise the date is cleared. The old date is always preserved in a comment. Date fixes are **label-neutral** — they never add or remove a label. Executed automatically per `tier1_due_date_clear` (borderline → proposal).
- **Stale time-based "must do" labels** (label applied > `optimistic_label_days` ago on a card not in the matching list) get a **three-way disposition**: (a) if the card meets the "no longer needed" test → archived (above); (b) if its workstream is **Active AND Time-sensitive** on the spine → the label is **swapped** to the matching tier (`2. Next Few Days (must do)` or `3. This Week (must do)`); (c) otherwise **removed**. The old label is always noted in a comment. Governed by `tier1_stale_label_removal` + `auto_min_confidence`. Swap decisions use the spine's per-workstream `Priority` / `Time-sensitive` attributes.
- **Names:** typo correction, capitalization, verb-first phrasing where natural, person names preserved and moved to a consistent position (`Colin — inputs on RSM comp` style). Pipe-separated multi-part names keep the primary action as the title; remaining segments become description bullets (no card splitting in P0). Original title always preserved in description.
- **Descriptions:** light structuring only — never delete content. If a merge or rename adds material, it is organized under `Original title`, `Context`, and per-source sections.
- One-time manual setup fix (not agent work): merge the two duplicate `Logan` labels.

### 5.2 (b) Scratch recovery
- Source lists: names matching `recovery_include_pattern` (default `^Scratch`) minus names matching `recovery_exclude_pattern` (default `^ARCHIVE`). Future `Scratch 7-x` lists are picked up automatically; Vijay excludes any list by renaming it with an `ARCHIVE ` prefix. The April `Archive - ...` lists are out of scope for now (>2 months old); revisit as a one-time review later.
- **Pre-first-run checklist item:** before enabling live runs, decide which of the current lists (`Scratch 6-24`, `Scratch 6-3`, `Scratch 5-12`) stay in scope and rename any exclusions with the `ARCHIVE ` prefix. Note `Scratch 5-12` sits right at the ~8-week boundary. The agent's first dry-run report restates this reminder.
- 15 cards per run, newest scratch list first. Daily review load stays trivial and the in-scope backlog drains in a few weeks.
- Each card gets exactly one disposition: route to Today / Next Few Days / This Week when the spine clearly supports it (direct-to-Today capped at `recovery_today_max` per run, default 3), route to `Inbox / Triage` when ambiguous, merge into an existing active card, or propose-archive (Tier 2). Every routed card carries a comment noting its origin list, and all recovered cards flow through normal dedup/hygiene on subsequent runs.

### 5.3 (c) Duplicate detection and merge
- Two-track candidate generation per Phase 2; adjudication and merge composition per Phase 3; tiering per §4.
- Survivor selection priority: card with the richest description → most recent activity → highest list (Today > Inbox > NFD).
- Merged description consolidates *all* source content (context fragmentation is the core problem this solves). Source cards remain readable at the top of the `Agent Archive` list, each linking to the survivor, so comments/attachments on losers stay reachable during review. If a losing card has attachments or checklists, the merge is forced Tier 2.
- `related-but-distinct` clusters are not merged; the agent cross-links them with comments (cheap, reversible, preserves the relationship for future runs).

### 5.4 (d) Reprioritization (spine "Problem 5")

Runs after §5.1–5.3 each morning so targets apply to the cleaned board. Scope is edit scope **plus `This Week`** (both source and destination).

- **Mark More Time-sensitive (upward):** scan `Inbox / Triage`, `This Week`, `Next Few Days` for cards with any of a `P0. High`/`P1` label, a due date inside the target list's window (`reprioritization_due_days` bands; an overdue date satisfies every band), or a **High-priority + High-time-sensitivity** workstream match on the spine. Move the card to the fitting list (P0 → Today, P1 → Next Few Days). Where evidence is a hard deadline, the action may add/upgrade the matching "must do" time label.
- **Mark Less Time-sensitive (downward):** only when `Today > today_list_target` or `Next Few Days > next_few_days_target`. Rank weakest first (no "must do" label, no near due date, low/no workstream priority, longest since last activity) and move just enough down (Today → Next Few Days, Next Few Days → This Week) to approach target.
- **Automatic gate (code, not the LLM):** the LLM enumerates the signals it relies on in a structured field; a move auto-executes only if (a) **every** claimed signal is verified by code against real card/spine data and at least one is verified (for downward moves the weakness criteria collectively count as one signal) **and** (b) confidence ≥ `time_reprioritization_confidence`. Anything else becomes an `Agent: Proposed` proposal. `automatic_action_confidence` continues to govern all non-reprioritization actions. `reprioritization_mode: proposed` forces every move to a proposal.
- **Hard exemptions (code-enforced):** a card placed/edited within `demotion_exempt_hours` (48h) is never demoted — automatically or by proposal; a card with `1. Today (must do)` is never demoted unless the action explicitly downgrades that label with the reason stated.
- **Conflict comments** are mandatory when a move contradicts the user's placement (all demotions) or spine priority conflicts with placement — the comment names the placement and ends "Reject if your placement stands." Capped at `max_reprioritization_moves_per_run`; the rejection ledger applies to every reprioritization action.

## 6. Guardrails (enforced in Python, not the LLM)

- Per-run caps: `max_merges_per_run` (10), `max_renames_per_run` (15), `max_recoveries_per_run` (15), `max_inscope_archives_per_run` (10), `max_reprioritization_moves_per_run` (10 — over-cap moves become proposals), `max_proposals_open` (20 — stop generating Tier 2 proposals when 20 are already open).
- Never touch: the Grooming Report card, any card edited by Vijay in the last 12 hours, any card with an open `Agent: Proposed` decision, anything outside scope definitions. The reprioritization scope check additionally allows `This Week` as a source/destination; the two reprioritization demotion exemptions (48h placement window, `1. Today (must do)`) are enforced in the code gate.
- Merge invariant: survivor description must contain every source card's original name and description text (validated string-containment check before the losing card moves to quarantine).
- LLM-generated names validated against source data (no invented people/entities not present in the source cards or spine).
- Rejection ledger consulted before every proposal; fingerprint = action type + sorted card ids (+ new name for renames).
- `--dry-run` flag: full pipeline runs, report written to local file (and optionally a report card), zero board mutations.

## 7. Data Stores and Config

### 7.1 SQLite (`state.db`, per standard agent pattern)
- `snapshots(run_id, ts, card_id, list_id, name, desc_hash, labels_json, due, date_last_activity)`
- `actions(run_id, ts, tier, action_type, card_ids_json, payload_json, status)`
- `proposals(proposal_id, run_id, fingerprint, card_ids_json, action_json, reason, status[open/approved/rejected/expired], opened_ts)`
- `rejections(fingerprint, source[edit/label-removal/comment/timeout], ts)`
- `recovery_ledger(card_id, source_list, disposition, ts)`
- `archive_ledger(card_id, entered_ts)` — when a card entered the `Agent Archive` list (drives the 60-day Trello-archive clock)
- `kv(key, value)` — small scratch state (e.g. `last_reminder_week` for the weekly spine reminder)

### 7.2 Notion context spine
**Created:** `Trello Grooming Agent Spine` (private workspace page, page ID `3966c55b25638155a69dfdb1421d5d3e`), read at run start, human-edited in P0. Sections:
- **Workstreams:** columns name, status, `Time Sensitivity`, and context. On the live page these are two Notion native tables — one under the **Active Workstreams** toggle, one under the **Complete or Inactive Workstreams** toggle — and both are parsed into one workstream list. `Time Sensitivity` is graded (High / Medium / Low / No; `Yes` tolerated; ranges like `Medium to High` take the higher grade); **High/Medium map to time-sensitive** for the stale-label swap and escalation decisions. Status `Complete`/`Completed` (as well as `Done`/`Winding down`) counts as finished for the "no longer needed" test. Cell text is markup-stripped (color spans) before parsing and empty Context cells are valid. The reader still accepts the older flat heading+bullet format with inline `Priority: High/Normal/Low` and `Time-sensitive: Yes/No` attributes. Seeded from the 2026-07-06 board: Sales Summit (Done, wrap-up only), AdmitHub rollout, Adaptive Connect, comp plans (RSM/AE), Q3 objectives and forecasts, intake escalations, Forge, weekend referral strategy (Ty), sales collateral/Sales Hub, hiring and onboarding, agents and knowledge systems.
- **People:** name, role, relationship to Vijay's work. Roles are inferred from the board and flagged for correction.
- **Notes for the agent:** standing interpretation rules (Done-workstream cards are archive candidates, unknown-context cards default to Inbox / Triage routing).
- **Card naming standard:** free-text rules the rename pass must follow (falls back to hardcoded rules if the section is absent).
- **Rules and thresholds:** live config overrides read each run — `- key: value` lines (trailing prose allowed) for any behavioral parameter. Valid values override `agent_config.json` for that run; the JSON on disk stays authoritative, so a default change must update both (see CLAUDE.md).

The spine is world state; `agent_config.json` is agent settings. Agent-written spine updates are post-P0. One-time task: ensure the page is shared with the Notion internal integration the agent will use.

### 7.3 `agent_config.json` (schema locked in `docs/config.md` before build)
Key parameters and defaults: `board_shortlink: RwdXsia3`; `edit_scope_lists: [Today, Inbox / Triage, Next Few Days]`; `comparison_extra_lists: [This Week]`; `recovery_include_pattern: ^Scratch`; `recovery_exclude_pattern: ^ARCHIVE`; `recovery_today_max: 3`; `archive_list_name: Agent Archive` (single archive list, auto-created last); `report_list: Today`; `labels: {auto_updated: "Agent: Auto-Updated", proposed: "Agent: Proposed"}`; `archive_list_days: 60`; `no_touch_hours: 0`; `dead_due_days: 14`; `optimistic_label_days: 7`; `recovery_batch_size: 2`; `proposal_timeout_days: 14`; `tier1_stale_label_removal: true`; `tier1_recovery_archive: true`; `tier1_due_date_clear: true`; `auto_min_confidence: 70`; per-run caps per §6; `model: claude-sonnet-4-6`; `ollama_model: qwen3:8b`; `weekly_sweep_day: sunday`; `spine_review_day: monday`; `dry_run: true` (ships true); `auto_pause_after_failures: 3`; `spine_page_id: 3966c55b25638155a69dfdb1421d5d3e`; `entity_keywords_seed: [...]`. The spine's "Rules and thresholds" section can override any of these per run (see docs/config.md precedence).

Credentials come from global `C:\Users\VJ\VS Code Projects\config\.env.json` — Trello key/token, Notion token, `anthropic_api_keys.work-todo-trello-grooming-agent`, Gmail SMTP for alerts. Shared library receives all settings as parameters and owns no config files.

All wall-clock window math (no-touch, quarantine/label expiry, dead-due, proposal timeout) runs through the stdlib-only UTC-offset + US-DST helper `agent_shared.infra.timeutil`, driven by the `local_tz_offsets` config field. This helper was **added to `agent-shared-library` as part of this build** (ported from the granola-to-notion agent's local `timezone_helpers`); `zoneinfo` is never imported. See `docs/config.md` for the `local_tz_offsets` schema.

## 8. Model Choice and Cost

Sonnet 4.6 ($3/$15 per MTok) for all three judgment calls — merge composition and spine-grounded triage benefit from Sonnet-level judgment, and volume is small. With the shared prefix cached (~15–20k tokens at 10% read cost) and modest marginal inputs/outputs, daily runs estimate to roughly $0.10–0.25/day, i.e., under ~$8/month, plus a slightly larger Sunday sweep. If costs matter later, the hygiene pass is the natural candidate to route to Haiku 4.5 ($1/$5). Cost tracked via the dedicated per-agent API key.

## 9. Success Metrics (review after 2 weeks live)

- **Duplicate burn-down:** open duplicate clusters in comparison scope trending to ~0; no Granola re-push survives more than one run.
- **Backlog drain:** scratch/archive backlog shrinking ~100 cards/week; empty in ~5 weeks.
- **Hygiene coverage:** >90% of in-scope cards pass name/date/label heuristics.
- **Trust:** Tier 1 rejection rate <10% and Tier 2 approval rate >70%; outside those bands, tune thresholds/spine before expanding scope.
- **Behavioral:** no new bankruptcy sweep needed (the ritual becoming unnecessary is the real win condition).

## 10. Post-P0 Roadmap

**P1 (next, roughly in order):**
1. **Expand edit scope** to `This Week`, then domain lists (Commercial/Sales Motion, Admin, etc.).
2. **Inbox routing:** dispositions for *non-duplicate* Inbox/Triage cards (route to Today/NFD/This Week/domain list with reason) — turns Inbox into a true triage queue and completes the Granola-agent handoff.
3. **Today right-sizing:** cap Today (start ~12); Tier 1 demotions using staleness, label freshness, spine relevance, and person-label batching (cards for the same 1:1 travel together).
4. **Staleness demotion + OBE detection** on NFD/This Week (cards referencing past events → Tier 2 closure proposals).
5. **Promote proven Tier 2 actions to Tier 1** based on observed approval rates (e.g., stale-label removal, recovery archiving).

**P2 (later):**
6. **Semantic dedup** via the planned ChromaDB + Sentence Transformers shared embedding library, slotting in behind the same candidate-generation interface (replaces the weekly LLM name sweep).
7. **Agent-written spine observations:** agent appends inferred workstream-status changes to a review section of the spine; nothing becomes permanent without confirmation.
8. **Rejection-driven tuning / rule learning** from the rejection ledger (data collected from day one).
9. **Granola/meeting context enrichment** (why a card exists, whether the driving meeting decision changed) and **calendar awareness**.
10. **Unified-view integration:** surface the run report and open proposals in the planned FastAPI/React Trello web app.
11. **Card splitting** for genuine multi-task cards (deferred because it increases card count).

## 11. Getting Started — Step by Step

### Step 0 — One-time board and workspace prep (~15 min, manual)
1. On the Fira board, create labels `Agent: Auto-Updated` (pick an unused color) and `Agent: Proposed`. The single `Agent Archive` list is **auto-created** at startup (positioned last) — no manual list creation needed.
2. Merge the two duplicate `Logan` labels (move cards off the lesser-used one, delete it).
3. The Notion spine already exists (`Trello Grooming Agent Spine`, §7.2): review it, correct any inferred roles/statuses, and share the page with the Notion internal integration the agent will use.
4. In the Anthropic Console, create an API key named `work-todo-trello-grooming-agent`. Add it to `C:\Users\VJ\VS Code Projects\config\.env.json` under `anthropic_api_keys.work-todo-trello-grooming-agent`. Confirm the Notion token and Gmail SMTP entries exist, and confirm the Trello key/token belongs to the account that owns the Fira board (single-member board, so likely yes, but verify if Fira lives in a company workspace).
5. Decide recovery scope for launch: rename any Scratch list you want excluded with an `ARCHIVE ` prefix (current candidates: `Scratch 6-24`, `Scratch 6-3`, `Scratch 5-12`; the April Archive lists are already excluded by default). The first dry-run report will remind you of this before anything goes live.

### Step 1 — Create the repo
From a terminal (GitHub CLI), or equivalently via github.com → New repository:

```
cd "C:\Users\VJ\VS Code Projects"
gh repo create rvijaysingh/work-todo-trello-grooming-agent --public --clone --add-readme
cd work-todo-trello-grooming-agent
```

### Step 2 — Seed the repo before any code
1. Save this document as `docs/design.md`.
2. Create `CLAUDE.md` with project conventions: Python 3.13 on Windows; consume `agent-shared-library` (Trello, LLM w/ Ollama fallback, config, alerting, SQLite modules); credentials only from the global `.env.json` (nested `anthropic_api_keys` dict — never a flat field); behavioral parameters only from `agent_config.json`; `findstr` not `grep`; update `LESSONS.md` on every bug root-cause; PDLC — config schema and test plan lock before implementation.
3. Create an empty `LESSONS.md` and a `docs/` folder.
4. Commit and push.

### Step 3 — Claude Code, Phase 1: lock the docs (no implementation)
Open the folder in VS Code, start Claude Code in the integrated terminal (`claude`). Prompt:

> Read docs/design.md and CLAUDE.md. Produce (1) docs/config.md — the complete agent_config.json schema with every parameter from design.md §7.3, types, defaults, and validation rules, plus the exact structure read from the global .env.json; (2) docs/testing.md — a test plan covering the diff/rejection-detection logic, candidate blocking, guardrail enforcement (per-run caps, merge content invariant, no-touch window), tier assignment bounds, quarantine lifecycle, and dry-run behavior, using a fixture built from an anonymized board export. Do not write implementation code. Flag any ambiguity in design.md as an open question at the top of config.md instead of resolving it silently.

Review both docs, resolve open questions, commit. **Schemas are now locked.**

### Step 4 — Claude Code, Phase 2: build
Prompt (adjust as needed):

> Implement the agent per docs/design.md, docs/config.md, and docs/testing.md. Structure: main.py (entry, --dry-run and --run-once flags), phases/ (snapshot_diff, candidates, judgment, execute, report), storage.py (SQLite per design §7.1), spine.py (Notion read), guardrails.py. Consume agent-shared-library for Trello/LLM/config/alerting — do not reimplement those. LLM calls: claude-sonnet-4-6 with prompt caching on the shared prefix; Ollama qwen3:8b fallback receiving identical unfiltered inputs; catch builtin TimeoutError explicitly alongside library exceptions. All LLM outputs schema-validated; invalid items dropped and logged, never partially applied. Write the pytest suite from docs/testing.md. Run the full test suite, fix all failures yourself, and re-run until green before considering this complete — do not report back with failing tests. Update LESSONS.md with the root cause of any bug you fix along the way.

### Step 5 — Dry-run week
1. Run manually first: `python main.py --run-once --dry-run` against the live board; review the report file.
2. Schedule in Task Scheduler: daily 6:30 AM, explicit path to the Python 3.13 executable (not the PATH default), working directory set to the repo.
3. Review dry-run reports for 3–5 days. Check: would you have accepted the merges? Are survivors chosen sensibly? Any spine gaps causing bad recovery triage? Tune spine and config.

### Step 6 — Go live and iterate
1. Set `dry_run: false`. First live week: skim the Grooming Report daily (60 seconds), fix anything wrong in-place (your edit is the rejection signal), approve/reject `Agent: Proposed` cards as they appear.
2. At two weeks, check §9 metrics. If Tier 1 rejections <10%, expand edit scope to `This Week` and promote stale-label removal to Tier 1. Then proceed down the P1 roadmap.

---

*Open items resolved in v1.0: labels renamed to `Agent: Auto-Updated` / `Agent: Proposed`; rejection = just edit the card (label handled by agent); edit scope = Today + Inbox/Triage + Next Few Days; merge labels = union minus time-based; quarantine = 7 days then auto-archive; dedup = two-track (LLM sees all in-scope names; Python blocking + weekly LLM sweep for wide comparison).*

*Open items resolved in v1.1: recovery routes directly to Today (capped) / Next Few Days / This Week when spine-supported, Inbox / Triage when ambiguous; recovery scope controlled per list via `ARCHIVE` rename prefix, April archives out of scope; single daily run with Grooming Report at top of Today; renames confirmed Tier 1 from day one; Notion spine created and seeded (`Trello Grooming Agent Spine`).*
