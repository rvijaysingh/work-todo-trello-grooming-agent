# config.md — work-todo-trello-grooming-agent

Configuration schema for `agent_config.json` (behavioral parameters) and the
structure read from the global `C:\Users\VJ\VS Code Projects\config\.env.json`
(credentials). This document is locked before implementation per the PDLC in
`CLAUDE.md`. Any structural change requires explicit approval.

Authoritative source: `docs/design.md` §7.3, extended with the parameters the
design implies elsewhere and the resolved design decisions recorded below.

---

## File locations and precedence

| File | Location | Committed? | Holds |
|---|---|---|---|
| `agent_config.json` | repo root | yes | All behavioral parameters (this schema) — the on-disk source of truth |
| `.env.json` | `C:\Users\VJ\VS Code Projects\config\.env.json` | **no** (gitignored, machine-local) | All credentials/secrets |
| `agent_config.example.json` | repo root | yes | Full example with defaults (mirrors the example below) |
| Notion spine — **Rules and thresholds** section | Notion page `spine_page_id` | n/a | **Live overrides** for behavioral params, read each run (see precedence below) |
| Notion spine — other sections | Notion page `spine_page_id` | n/a | World state (workstreams/people/notes/naming standard), not settings |

Rules (from CLAUDE.md): secrets live **only** in `.env.json`; behavioral params
live **only** in `agent_config.json` (the authoritative on-disk file) with the
spine **Rules and thresholds** section as a live per-run override; no thresholds,
list names, label names, or caps are hardcoded anywhere in code. All configuration
is validated at startup — fail fast with a clear message naming the
missing/invalid field. The shared library receives every value as a parameter and
owns no config files.

### Config precedence

For every run the effective value of a behavioral parameter is resolved as:

1. `agent_config.json` on disk (authoritative default).
2. Overridden by a matching key in the spine **Rules and thresholds** section
   **iff** the value is valid and passes full settings validation.

`--dry-run` on the CLI and a `dry_run: true` in the spine Rules section both force
dry-run on top of the file value (any of the three being true → dry-run). When the
Notion section sets `dry_run`, the Grooming Report states the value came from
Notion. Invalid values and unknown keys in the Rules section are ignored with a
note in the report; a missing section, an empty section, or an unreachable spine
falls back silently to the file. Because the Rules section is live config, any
change to a behavioral default must update **both** `agent_config.json` and that
Notion section (see CLAUDE.md). Recognized keys and coercion live in
`spine.py::_OVERRIDE_TYPES` and cover every behavioral parameter below (the three
`tier1_*` flags, `dry_run`, `recovery_batch_size`, `archive_list_days`,
`proposal_timeout_days`, `spine_review_day`, caps, thresholds, and windows).

---

## `agent_config.json` schema

Types: `string`, `int`, `float`, `bool`, `array<T>`, `object`. "Validation" is
enforced at startup unless noted.

### Identity and scope

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `board_shortlink` | string | `"RwdXsia3"` | non-empty; must resolve to an accessible Trello board | Shortlink of the Fira board the agent operates on. |
| `edit_scope_lists` | array\<string> | `["Today", "Inbox / Triage", "Next Few Days"]` | non-empty; each name must resolve to exactly one list on the board | Lists where hygiene edits and dedup merges are **applied**. |
| `comparison_extra_lists` | array\<string> | `["This Week"]` | may be empty; each name must resolve to a list | Extra lists added to comparison scope (dedup detection) on top of edit scope and all scratch/archive lists. |
| `recovery_include_pattern` | string (regex) | `"^Scratch"` | must compile as a valid Python regex | List names matching this are recovery **sources**. |
| `recovery_exclude_pattern` | string (regex) | `"^ARCHIVE"` | must compile as a valid Python regex | List names matching this are excluded from recovery even if they match the include pattern (the `ARCHIVE ` rename convention). |

### Lists and labels

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `archive_list_name` | string | `"Agent Archive"` | non-empty; **auto-created** at startup (positioned last on the board) if absent — never required to pre-exist | The single list where merged-away duplicates, approved archive proposals, and recovery-archived cards move (to the TOP). Excluded from edit, dedup-comparison, and recovery scopes. Cards here longer than `archive_list_days` are archived via Trello's built-in (restorable) archive. Nothing is ever hard-deleted; nothing reaches Trello's archive without passing through here. |
| `report_list` | string | `"Today"` | must resolve to an existing list; should be in edit scope | List where the pinned "Grooming Report" card and the weekly spine-review reminder card are placed at the top. |
| `labels.auto_updated` | string | `"Agent: Auto-Updated"` | must resolve to an existing board label | Label applied to Tier 1 auto-executed actions. |
| `labels.proposed` | string | `"Agent: Proposed"` | must resolve to an existing board label | Label applied to Tier 2 flag-only proposals. |

### Recovery

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `recovery_batch_size` | int | `2` | `> 0` | Number of unprocessed scratch cards triaged per run, newest source list first. Deliberately small — a steady, low-risk drain. |
| `recovery_today_max` | int | `3` | `>= 0` | Max cards routed **directly to Today** per run. **Not** bounded by `recovery_batch_size` (it may exceed the batch — harmless; a small batch simply never reaches the cap in one run). Cards judged Today-worthy beyond this cap route to `Next Few Days`, noted in the report. |

### Timing windows

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `archive_list_days` | int | `60` | `> 0` | Age (from the SQLite-tracked entry timestamp) at which a card sitting in the Agent Archive list is archived via Trello's built-in (restorable) archive. |
| `proposal_timeout_days` | int | `14` | `> 0` | Age at which open Tier 2 (`Agent: Proposed`) proposals are summarized in the report, dropped, and fingerprinted as rejected (§4). Independent of `archive_list_days`. |
| `no_touch_hours` | int | `0` | `>= 0` | A card edited by Vijay within this window is never touched. **Default 0** disables the window (automatic mode acts immediately); the rejection ledger and the open-proposal lock still protect their cards regardless. |
| `dead_due_days` | int | `14` | `> 0` | Due dates more than this many days overdue are classified against the spine (still-matters vs no-longer-matters); within the window they are left alone (§5.1). |
| `optimistic_label_days` | int | `7` | `> 0` | Stale time-based labels are removable only if applied more than this many days ago. Also the window after which an untouched `Agent: Auto-Updated` label (a cosmetic marker) is stripped (§5.1). |

### Per-run caps (§6)

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `max_merges_per_run` | int | `10` | `>= 0` | Hard cap on merges executed per run. |
| `max_renames_per_run` | int | `15` | `>= 0` | Hard cap on renames executed per run. When it binds, heuristic-flagged names take priority over LLM-nominated non-flagged renames (see `name_min_length`). |
| `max_recoveries_per_run` | int | `15` | `>= 0` | Hard cap on recovery routings executed per run. |
| `max_proposals_open` | int | `20` | `>= 0` | Stop generating new Tier 2 proposals once this many are already open. |

### Automatic-mode toggles (shipped default is automatic)

When a toggle is `true`, actions in that category execute automatically (Tier 1)
with an explanatory card comment (reason + `Confidence: NN%`), **unless** the LLM
marks the item borderline or reports a confidence below `auto_min_confidence` — in
which case it becomes an `Agent: Proposed` card instead. When a toggle is `false`,
the whole category is `Agent: Proposed`.

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `tier1_stale_label_removal` | bool | `true` | `true` / `false` | Auto-remove stale time-based labels (deterministic → confidence 100, never borderline). |
| `tier1_recovery_archive` | bool | `true` | `true` / `false` | Auto-archive recovery cards judged no longer needed (workstream Done on the spine, an event/deadline passed with nothing left to do, or a card titled `[Owner: Name]`). |
| `tier1_due_date_clear` | bool | `true` | `true` / `false` | Auto-apply the dead-due decision (re-date from a written source, else clear) for cards classified no-longer-matters. |
| `auto_min_confidence` | int | `70` | `0 <= x <= 100` | An auto-eligible action with LLM confidence below this (or flagged `borderline`) is downgraded to `Agent: Proposed` instead of executing. |

### Blocking / similarity (Phase 2, deterministic, no LLM)

Jaccard similarity is computed on **normalized name tokens**: lowercased,
punctuation stripped, stopwords removed.

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `wide_block_jaccard` | float | `0.5` | `0.0 < x <= 1.0` | Wide-track (vs. Scratch/Archive) pair is blocked and sent to the LLM if token Jaccard ≥ this value. A pair is **also** blocked if it shares at least one entity keyword AND at least one person label, regardless of Jaccard. |
| `narrow_hint_jaccard` | float | `0.4` | `0.0 < x <= 1.0` | Used only to build **hint** clusters for the narrow track (all in-scope names are sent to the LLM regardless; the LLM may add clusters it sees). |

### Name-quality heuristics (prioritization filter, not a gate)

A name is **heuristic-flagged** if any hold: it contains a pipe (`|`); is shorter
than `name_min_length` or longer than `name_max_length` characters; is entirely
lowercase; contains consecutive double spaces; or has leading/trailing
whitespace. Flagging does not gate renaming — the hygiene LLM pass may nominate
additional renames beyond flagged cards — but when `max_renames_per_run` binds,
heuristic-flagged cards take priority.

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `name_min_length` | int | `4` | `> 0`; `< name_max_length` | Names shorter than this are heuristic-flagged. |
| `name_max_length` | int | `100` | `> name_min_length` | Names longer than this are heuristic-flagged. |

### LLM and scheduling

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `model` | string | `"claude-sonnet-4-6"` | non-empty | Primary LLM (Anthropic), prompt-cached shared prefix. |
| `ollama_model` | string | `"qwen3:8b"` | non-empty | Fallback LLM; receives the same unfiltered inputs as primary. |
| `weekly_sweep_day` | string | `"sunday"` | one of `monday`…`sunday` (lowercase) | Day the full-board LLM name sweep runs (§2 wide track). |
| `spine_review_day` | string | `"monday"` | one of `monday`…`sunday`, or `"off"` | On the first run on/after this weekday each week, create a "Review agent spine…" card at the top of Today (unless one is already open). `"off"` disables the reminder. |

### Runtime and operations

| Parameter | Type | Default | Allowed / validation | Description |
|---|---|---|---|---|
| `dry_run` | bool | `true` | `true` / `false` | Ships `true`. When true, the full pipeline runs but zero board mutations occur (§6); overridable by `--dry-run`. |
| `auto_pause_after_failures` | int | `3` | `> 0` | Consecutive failed runs after which the agent auto-pauses (§3). |
| `spine_page_id` | string | `"3966c55b25638155a69dfdb1421d5d3e"` | non-empty; must be a readable Notion page | Notion context spine read at run start. |
| `entity_keywords_seed` | array\<string> | see example | may be empty; lowercase strings | Seed entity keywords for blocking/clustering. At runtime, person names from the Notion spine **People** section are appended to this set before blocking runs. |
| `local_tz_offsets` | object | `{"standard": -5, "daylight": -4}` | keys `standard` and `daylight`; each `-24 < x < 24` | UTC offsets (hours) for the local timezone (US Eastern by default), consumed by the stdlib-only UTC-offset helper with DST auto-switching in `agent-shared-library` (`agent_shared.infra.timeutil`, added to the shared library as part of this build — the `zoneinfo` workaround for Windows Python 3.13). Drives all wall-clock window math. |
| `log_level` | string | `"INFO"` | one of `DEBUG`, `INFO`, `WARNING`, `ERROR` | Logging verbosity (global CLAUDE.md logging standard). |

### Cross-field validation (startup)

- `recovery_today_max` is **not** bounded by `recovery_batch_size` (may exceed it).
- `no_touch_hours >= 0` (0 disables the no-touch window).
- `name_min_length < name_max_length`.
- `0 <= auto_min_confidence <= 100`.
- `0.0 < narrow_hint_jaccard <= 1.0` and `0.0 < wide_block_jaccard <= 1.0`.
- `local_tz_offsets` has both `standard` and `daylight` numeric keys.
- Every list name in `edit_scope_lists`, `comparison_extra_lists`, and
  `report_list` must resolve to exactly one list on the board named by
  `board_shortlink`. `archive_list_name` need only be non-empty — it is
  **auto-created** (positioned last) at startup if absent.
- Both label names in `labels.*` must resolve to existing board labels.
- `recovery_include_pattern` and `recovery_exclude_pattern` must compile as regex.
- `weekly_sweep_day` must be a valid lowercase weekday; `spine_review_day` must be
  a valid lowercase weekday or `"off"`.
- `spine_page_id` must be readable by the configured Notion integration.
- Any missing or type-mismatched field aborts the run with a message naming the
  field (fail fast).

---

## `.env.json` structure (credentials only)

Read from `C:\Users\VJ\VS Code Projects\config\.env.json`. Gitignored,
machine-local. Field names below are the **actual** keys in that file. Trello and
Notion credentials are **nested objects**; Gmail SMTP fields are top-level. The
file also contains sections for other agents (Granola, Gmail OAuth, other
Anthropic keys) which this agent ignores. **Only** the key names and structure
are documented here — never any secret values.

```json
{
  "gmail_sender": "<...>",
  "gmail_password": "<...>",
  "gmail_recipient": "<...>",
  "ollama_endpoint": "<...>",
  "ollama_model": "<...>",
  "trello": {
    "api_key": "<...>",
    "token": "<...>",
    "fira_todo_board_id": "<...>",
    "fira_today_list_id": "<...>",
    "fira_inbox_triage_list_id": "<...>",
    "fira_p0_high_label_id": "<...>",
    "fira_p1_medium_label_id": "<...>"
  },
  "notion": {
    "integration_token": "<...>"
  },
  "anthropic_api_keys": {
    "work-todo-trello-grooming-agent": "<...>"
  }
}
```

| Path | Type | Used for | Notes |
|---|---|---|---|
| `trello.api_key` | string | Trello REST auth (key) | Nested under `trello`. Owns the Fira board. |
| `trello.token` | string | Trello REST auth (token) | Nested under `trello`. |
| `trello.fira_todo_board_id` | string | Fira board id | Available for direct lookups; the agent still resolves scope by `board_shortlink` from `agent_config.json`. Sibling `fira_*_list_id` / `fira_*_label_id` keys exist for convenience but are not required by this agent. |
| `notion.integration_token` | string | Reading the Notion spine page | Nested under `notion`. The spine page must be shared with this integration. |
| `gmail_sender` | string | Alerting sender (Gmail SMTP `smtp.gmail.com:587`) | Top-level. Per global CLAUDE.md alerting standard. |
| `gmail_password` | string | Alerting SMTP auth (app password) | Top-level. Per global CLAUDE.md alerting standard. |
| `ollama_endpoint` | string | Ollama fallback connection URL | Top-level. Consumed when the primary LLM is unavailable. |
| `anthropic_api_keys` | object | Namespaced Anthropic keys | Top-level. |
| `anthropic_api_keys.work-todo-trello-grooming-agent` | string | This agent's Anthropic key | **Read from this nested path only — never a flat top-level key field, and never another agent's sibling key** (a flat-field read caused a rent-checker production failure). |

Startup must fail with a clear message if any required credential is missing,
naming the exact missing path (e.g., `anthropic_api_keys.work-todo-trello-grooming-agent`).
The Ollama fallback requires no credential beyond `ollama_endpoint`.

---

## Full example `agent_config.json`

All defaults populated.

```json
{
  "board_shortlink": "RwdXsia3",
  "edit_scope_lists": ["Today", "Inbox / Triage", "Next Few Days"],
  "comparison_extra_lists": ["This Week"],
  "recovery_include_pattern": "^Scratch",
  "recovery_exclude_pattern": "^ARCHIVE",

  "archive_list_name": "Agent Archive",
  "report_list": "Today",
  "labels": {
    "auto_updated": "Agent: Auto-Updated",
    "proposed": "Agent: Proposed"
  },

  "recovery_batch_size": 2,
  "recovery_today_max": 3,

  "archive_list_days": 60,
  "proposal_timeout_days": 14,
  "no_touch_hours": 0,
  "dead_due_days": 14,
  "optimistic_label_days": 7,

  "max_merges_per_run": 10,
  "max_renames_per_run": 15,
  "max_recoveries_per_run": 15,
  "max_proposals_open": 20,

  "tier1_stale_label_removal": true,
  "tier1_recovery_archive": true,
  "tier1_due_date_clear": true,
  "auto_min_confidence": 70,

  "wide_block_jaccard": 0.5,
  "narrow_hint_jaccard": 0.4,

  "name_min_length": 4,
  "name_max_length": 100,

  "model": "claude-sonnet-4-6",
  "ollama_model": "qwen3:8b",
  "weekly_sweep_day": "sunday",
  "spine_review_day": "monday",

  "dry_run": true,
  "auto_pause_after_failures": 3,
  "spine_page_id": "3966c55b25638155a69dfdb1421d5d3e",
  "entity_keywords_seed": [
    "sales summit",
    "admithub",
    "adaptive connect",
    "comp plan",
    "comp",
    "intake",
    "escalation",
    "forge",
    "q3",
    "forecast",
    "objectives",
    "onboarding",
    "collateral",
    "sales hub",
    "hiring",
    "weekend referral",
    "scoreboard",
    "hchb",
    "payer",
    "focus group",
    "dashboard"
  ],
  "local_tz_offsets": { "standard": -5, "daylight": -4 },
  "log_level": "INFO"
}
```
