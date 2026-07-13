# work-todo-trello-grooming-agent

A daily grooming agent for Vijay's work (Fira) Trello board. It makes the active
card set trustworthy and manageable — one card per task, clean names/dates/labels,
and a steady drain of the buried Scratch backlog — using an **undo-by-default,
human-in-the-loop** model: high-confidence actions execute immediately but nothing
is permanent for 7 days, and reviewing is a glance while undoing is a drag.

Authoritative design: [`docs/design.md`](docs/design.md). Config schema:
[`docs/config.md`](docs/config.md). Test plan: [`docs/testing.md`](docs/testing.md).

## What it does (P0)

One scheduled run per day, five phases (design §3):

1. **Snapshot & diff** — pull the board, diff against the last post-run snapshot to
   detect implicit rejections (cards Vijay edited/relabeled/pulled back), approvals
   on `Agent: Proposed` cards, and new Scratch sweep lists.
2. **Candidate generation** (deterministic, no LLM) — two-track duplicate blocking
   (normalized-token Jaccard + entity-keyword/person-label paths), the recovery
   batch (newest Scratch list first), and hygiene candidates.
3. **LLM judgment** (`claude-sonnet-4-6`, Ollama `qwen3:8b` fallback) — cluster
   adjudication, hygiene cleanup, and recovery triage. All output is schema-validated
   JSON; malformed or unknown-id items are dropped; LLM-generated names are grounded
   against source + spine before any write.
4. **Validated execution** — Python enforces every guardrail (caps, merge
   string-containment invariant, no-touch window, never-touch list, rejection
   ledger) **before** any Trello write. Tier 1 executes; Tier 2 becomes an
   `Agent: Proposed` card (automatic mode is the shipped default: the `tier1_*`
   flags auto-execute confident actions with an explanatory comment; borderline
   ones become proposals). Nothing is hard-deleted; nothing reaches Trello's
   built-in (restorable) archive without first passing through the single
   `Agent Archive` list (auto-created; cards older than `archive_list_days` are
   Trello-archived).
5. **Run report** — a single "Grooming Report" card at the top of `Today`, replaced
   each run (sections: Still overdue and possibly urgent / Awaiting your decision /
   Recently archived / Done automatically / Health stats), plus a local report file.

## Layout

```
main.py                 entry point (--dry-run, --run-once)
settings.py             agent_config.json + .env.json loading and validation
models.py               Card / BoardView domain models
guardrails.py           pure functions: blocking math, name heuristics, tiering,
                        window predicates, merge invariant, name grounding
storage.py              SQLite (design §7.1) + pause flag + ledgers
spine.py                Notion spine read
phases/                 snapshot_diff, candidates, judgment, execute, report
prompts/                LLM prompt templates
agent_config.json       behavioral parameters (see docs/config.md)
tests/                  pytest suite + anonymized board fixture
```

The agent consumes **agent-shared-library** for Trello, LLM (Anthropic→Ollama),
config loading, alerting (Gmail SMTP), SQLite helpers, the Notion client, and the
stdlib UTC-offset/DST time helper (`agent_shared.infra.timeutil`, added in shared
library v0.2.0 as part of this build).

## Setup

Use the explicit **Python 3.13** executable everywhere — never the PATH default:

```
C:\Users\VJ\AppData\Local\Programs\Python\Python313\python.exe
```

1. Install the shared library (editable) and pytest:
   ```
   C:\Users\VJ\AppData\Local\Programs\Python\Python313\python.exe -m pip install -e "C:\Users\VJ\VS Code Projects\agent-shared-library"
   C:\Users\VJ\AppData\Local\Programs\Python\Python313\python.exe -m pip install pytest
   ```
2. Confirm credentials exist in the global `C:\Users\VJ\VS Code Projects\config\.env.json`
   (see docs/config.md for the exact keys): `trello.api_key`, `trello.token`,
   `notion.integration_token`, `gmail_sender`, `gmail_password`, `ollama_endpoint`,
   and `anthropic_api_keys.work-todo-trello-grooming-agent`.
3. One-time board/workspace prep (design §11, Step 0): create the labels
   `Agent: Auto-Updated` and `Agent: Proposed` (the single `Agent Archive` list is
   auto-created at startup); merge the two duplicate `Logan` labels; review/share
   the Notion spine page — including its optional "Rules and thresholds" (live
   config overrides) and "Card naming standard" sections.
4. `agent_config.json` ships with `dry_run: true`. Behavioral parameters live only
   here; secrets live only in `.env.json`.

## Running

```
:: dry-run once against the live board (writes a report file, zero board changes)
C:\Users\VJ\AppData\Local\Programs\Python\Python313\python.exe main.py --run-once --dry-run

:: live run (only after the dry-run week and setting dry_run:false)
C:\Users\VJ\AppData\Local\Programs\Python\Python313\python.exe main.py --run-once
```

`--dry-run` forces zero board mutations regardless of config. The report is written
to `logs/grooming_report.txt` (and, in live mode, to the Grooming Report card).

### Tests

```
C:\Users\VJ\AppData\Local\Programs\Python\Python313\python.exe -m pytest tests\ -x
```

The suite mocks all external dependencies (Trello, LLM, Notion, SMTP), makes no
network calls, and runs in well under 5 seconds.

## Task Scheduler (daily 6:30 AM)

Create a Basic Task:
- **Trigger:** Daily, 6:30 AM.
- **Action:** Start a program.
  - **Program/script:** `C:\Users\VJ\AppData\Local\Programs\Python\Python313\python.exe`
  - **Add arguments:** `main.py --run-once`
  - **Start in:** `C:\Users\VJ\VS Code Projects\work-todo-trello-grooming-agent`
- Always use the explicit 3.13 path above, not `python` (PATH may resolve to another
  version). Task Scheduler can fire before the network is up; the agent's startup
  checks tolerate transient failures and alert on hard ones.

## Dry-run week

1. Run `main.py --run-once --dry-run` manually first; read `logs/grooming_report.txt`.
   The first dry-run report restates the **ARCHIVE reminder**: decide which Scratch
   lists stay in recovery scope and rename any exclusions with an `ARCHIVE ` prefix
   (current candidates: `Scratch 6-24`, `Scratch 6-3`, `Scratch 5-12` — note 5-12 is
   near the ~8-week boundary).
2. Schedule the dry-run in Task Scheduler and review reports for 3–5 days. Check:
   would you have accepted the merges? Are survivors chosen sensibly? Any spine gaps
   causing bad recovery triage? Tune the spine and `agent_config.json`.

## Go-live checklist (design §11, Steps 5–6)

1. Set `dry_run: false` in `agent_config.json`.
2. First live week: skim the Grooming Report daily (~60s). Fix anything wrong
   in-place — your edit **is** the rejection signal (the next run detects it, treats
   your version as final, records a rejection fingerprint so it is never re-proposed,
   and removes the `Agent: Auto-Updated` label itself). Approve/reject `Agent: Proposed`
   cards by replying `yes`/`approve` on the comment or removing the label.
3. At two weeks, check the design §9 metrics. If the Tier 1 rejection rate is < 10%,
   consider flipping `tier1_stale_label_removal` / `tier1_recovery_archive` to `true`
   (the report surfaces Tier 2 approval rates to inform this) and expanding edit scope
   to `This Week`, then proceed down the P1 roadmap.

## Clearing the pause flag

Three consecutive failed runs set a `paused` flag in SQLite (`state.db`) that blocks
all further runs and sends an alert. After you have fixed the underlying cause, clear
it with the storage helper:

```
C:\Users\VJ\AppData\Local\Programs\Python\Python313\python.exe -c "import storage; storage.clear_pause('state.db', '')"
```

This resets both the paused flag and the consecutive-failure counter. The next
scheduled run proceeds normally.
