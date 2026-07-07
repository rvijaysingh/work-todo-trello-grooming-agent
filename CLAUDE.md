# CLAUDE.md — work-todo-trello-grooming-agent

## What this project is
A daily grooming agent for Vijay's work (Fira) Trello board. It deduplicates and merges cards, cleans names/descriptions/labels/due dates, and drains buried Scratch-list backlog, using an undo-by-default human-in-the-loop model. The authoritative design is `docs/design.md`. Do not deviate from it without flagging the deviation explicitly.

## Environment
- Windows 11, Python 3.13. Task Scheduler is the runtime; always reference the explicit Python 3.13 executable path, never rely on PATH defaults.
- Command Prompt has no `grep`; use `findstr` or run `pytest` directly.
- `zoneinfo` crashes on Windows Python 3.13 in this stack; use the stdlib UTC offset helper pattern from agent-shared-library.

## Dependencies and shared code
- Consume `agent-shared-library` for Trello API, LLM calls (Anthropic primary, Ollama fallback), config loading, alerting (Gmail SMTP), and SQLite helpers. Never reimplement these locally.
- The shared library receives all settings as parameters. It owns no config files.

## Configuration rules
- Credentials live ONLY in the global `C:\Users\VJ\VS Code Projects\config\.env.json`.
- The Anthropic key is read from the nested dict: `anthropic_api_keys.work-todo-trello-grooming-agent`. Never read a flat top-level API key field. (This exact bug caused a production failure in the rent-checker agent; see its LESSONS.md.)
- Behavioral parameters live ONLY in `agent_config.json`, schema locked in `docs/config.md`. No hardcoded thresholds, list names, label names, or caps anywhere in code.

## LLM integration standards
- Model: `claude-sonnet-4-6` with prompt caching on the shared prefix. Fallback: Ollama `qwen3:8b`.
- The fallback receives the same unfiltered inputs as the primary. Pre-filtering that defeats the fallback is an antipattern.
- Catch builtin `TimeoutError` explicitly alongside library exceptions; builtin exceptions are not subclasses of library exception hierarchies.
- Factual claims (card ids, dates, counts, list names) are pre-computed in Python and passed to the LLM as constraints. The LLM judges; it does not assert facts.
- All LLM output is schema-validated JSON. Invalid or unknown-id items are dropped and logged, never partially applied. LLM-generated card names are validated against source data before writing.

## Safety invariants (enforce in code, never rely on the LLM)
- Dry-run mode (`--dry-run`) performs zero board mutations.
- Never touch: the Grooming Report card, cards edited by Vijay within `no_touch_hours`, cards with open Agent: Proposed decisions, anything outside configured scope.
- Merge invariant: the survivor description must contain every source card's original name and description text, verified by string containment before any card moves to quarantine.
- Nothing is ever hard-deleted. Nothing reaches Trello's archive without passing through the quarantine list.
- Respect all per-run caps from config.

## Process
- PDLC: `docs/config.md` and `docs/testing.md` are locked before implementation. Config structure changes mid-build require explicit approval.
- Every bug fix gets a root-cause entry in `LESSONS.md` in the same commit.
- Before declaring any task complete: run the full pytest suite, fix all failures yourself, and re-run until green. Do not report back with failing tests or ask Vijay to relay errors.
- Keep docs in `docs/` current with any approved change (design, config, testing).
