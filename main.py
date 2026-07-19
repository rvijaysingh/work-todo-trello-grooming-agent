"""
work-todo-trello-grooming-agent — entry point.

One scheduled run per day (Windows Task Scheduler, 6:30 AM, Python 3.13). Five
phases: snapshot & diff, candidate generation, LLM judgment, validated
execution, run report. See docs/design.md.

Flags:
  --dry-run    Force dry-run (zero board mutations) regardless of config.
  --run-once   Single run (the agent has no internal loop; explicit for clarity).

Safety: three consecutive failed runs set a paused flag in SQLite that blocks
further runs until manually cleared (see README, "Clearing the pause flag").
Unhandled exceptions send a crash alert and re-raise (non-zero exit).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from datetime import datetime, timezone

import guardrails as g
import storage
from agent_shared.alerts import send_alert, send_crash_alert
from agent_shared.llm.client import LLMClient
from agent_shared.notion_client import NotionClient
from agent_shared.trello.client import TrelloClient
from agent_shared.llm.prompt_loader import PromptLoader
from models import BoardView
from phases import candidates as cand
from phases import execute as ex
from phases import judgment as judge
from phases import report as rep
from phases import snapshot_diff as sd
from settings import AGENT_NAME, load_credentials, load_settings
from spine import read_spine

logger = logging.getLogger(__name__)

ENV_CONFIG_PATH = r"C:\Users\VJ\VS Code Projects\config\.env.json"
AGENT_CONFIG_PATH = "agent_config.json"
PROMPTS_DIR = "prompts"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fira board grooming agent")
    p.add_argument("--dry-run", action="store_true", help="Force dry-run (no board writes)")
    p.add_argument("--run-once", action="store_true", help="Single run (no loop)")
    p.add_argument("--reset-state", action="store_true",
                   help="Wipe all run/diff state (snapshots, actions, proposals, rejections, "
                        "ledgers, kv) and exit. Deletes only agent state, never board data.")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Startup: clients and id resolution
# ---------------------------------------------------------------------------

def build_clients(creds, settings):
    """Construct the Trello, Notion, LLM clients and the prompt loader."""
    trello = TrelloClient(creds.trello_api_key, creds.trello_token, creds.trello_board_id)
    notion = NotionClient(creds.notion_token)
    llm = LLMClient(
        anthropic_api_key=creds.anthropic_api_key,
        ollama_host=creds.ollama_endpoint,
        ollama_model=settings.ollama_model,
        anthropic_model=settings.model,
    )
    prompts = PromptLoader(PROMPTS_DIR)
    return trello, notion, llm, prompts


def fetch_board(trello, settings) -> BoardView:
    """Read the board (lists, labels, cards, and comments on proposed cards)."""
    lists = trello.get_board_lists()
    labels = trello.get_board_labels()
    board = BoardView(
        lists=[sd.ListInfo(id=l.id, name=l.name, closed=l.closed, pos=l.position) for l in lists],
        labels=[{"id": lb.id, "name": lb.name, "color": lb.color} for lb in labels],
    )
    list_name = {l.id: l.name for l in lists}
    for l in lists:
        if l.closed:
            continue
        for tc in trello.get_list_cards(l.id):
            board.cards.append(sd.card_from_trello(tc, list_name.get(tc.list_id, "")))
    # Comments only where we need them: Agent: Proposed cards.
    for c in board.cards:
        if c.has_label(settings.label_proposed):
            for act in trello.get_card_actions(c.id, action_filter="commentCard"):
                data = act.get("data", {}).get("text", "")
                board.comments.append(
                    sd.Comment(id=act.get("id", ""), card_id=c.id, text=data,
                               date=act.get("date", ""), member="")
                )
    return board


def resolve_ids_or_fail(board: BoardView, settings) -> None:
    """Verify every configured list/label name resolves; raise if any is missing."""
    missing = []
    for name in settings.comparison_scope_lists + [settings.report_list]:
        if board.list_by_name(name) is None:
            missing.append(f"list '{name}'")
    for label in (settings.label_auto_updated, settings.label_proposed):
        if board.label_id(label) is None:
            missing.append(f"label '{label}'")
    if missing:
        raise RuntimeError("Unresolved configured names: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# Pipeline (testable with injected board / judgments / mut/clients)
# ---------------------------------------------------------------------------

def run_pipeline(board, settings, db_path, now_utc, dry_run, first_run,
                 llm=None, prompts=None, spine=None, trello=None, judgments=None,
                 notion_notes=None):
    """Run phases 1–5 over an already-fetched board. Returns (result, report_text).

    judgments (optional): {'clusters': [...], 'hygiene': [...], 'recovery': [...]}
    lets tests inject LLM verdicts deterministically, bypassing the LLM calls.
    notion_notes (optional): report lines from applying spine "Rules" overrides.
    """
    now_iso = now_utc.astimezone(timezone.utc).isoformat() if now_utc.tzinfo else now_utc.replace(tzinfo=timezone.utc).isoformat()
    run_id = now_iso

    prev_run_id = storage.latest_prior_run_id(db_path, run_id)
    prev_snapshot = storage.get_snapshot(db_path, prev_run_id) if prev_run_id else {}
    prior_actions = storage.get_actions(db_path, run_id=prev_run_id) if prev_run_id else []

    result = ex.ExecutionResult()
    result.notion_notes = list(notion_notes or [])
    mutator = ex.BoardMutator(trello, dry_run)

    # Ensure the single Agent Archive list exists (auto-created, positioned last).
    archive_list = ex.ensure_archive_list(board, settings, mutator)
    archive_list_id = archive_list.id if archive_list else None

    # Weekly spine-review reminder (created before candidate gen so it's protected).
    ex.maybe_create_spine_reminder(db_path, mutator, board, settings, spine, now_utc, result)

    # Phase 1 — diff
    rejections = sd.detect_implicit_rejections(prev_snapshot, board, prior_actions, settings, archive_list_id)
    open_proposals = storage.get_open_proposals(db_path)
    approvals = sd.parse_approvals(board, open_proposals, settings)

    ex.process_rejections(db_path, mutator, board, rejections, settings, now_iso, result)
    ex.expire_proposals(db_path, board, open_proposals, settings, now_utc, now_iso, result, dry_run=dry_run)
    ex.process_approvals(db_path, mutator, board, approvals, settings, now_utc, now_iso, result)
    ex.expire_labels_and_archive(db_path, mutator, board, settings, now_utc, result)

    # Phase 2 — candidates
    entity_keywords = cand.build_entity_keywords(settings, spine)
    in_scope_ids = ex._in_scope_ids(board, settings)
    in_scope_cards = [c for c in board.cards if c.list_id in in_scope_ids and not c.closed]
    comp_ids = {board.list_by_name(n).id for n in settings.comparison_scope_lists if board.list_by_name(n)}
    scratch_ids = {l.id for l in sd.recovery_source_lists(board, settings)}
    wide_cards = [c for c in board.cards if (c.list_id in scratch_ids or c.list_id in comp_ids)
                  and c.list_id not in in_scope_ids and not c.closed]
    hyg = cand.hygiene_candidates(in_scope_cards, board, settings, now_utc)
    processed = storage.processed_recovery_ids(db_path)
    rec_batch = cand.recovery_batch(board, settings, processed)

    # Phase 3 — judgment
    if judgments is None:
        judgments = _run_judgment(llm, prompts, spine, board, in_scope_cards, wide_cards,
                                  entity_keywords, hyg, rec_batch, settings)
    cluster_verdicts = judgments.get("clusters", [])
    hygiene_verdicts = judgments.get("hygiene", [])
    recovery_verdicts = judgments.get("recovery", [])

    # Phase 4 — execute (precedence: merge > archive > date/label/title fixes)
    flagged_ids = {c.id for c in hyg["flagged_renames"]}
    dead_due_ids = {c.id for c in hyg["dead_dues"]}
    spine_terms = spine.all_terms() if spine else []
    inscope_archive_verdicts = judgments.get("inscope_archive", [])
    label_verdicts = judgments.get("labels", [])

    # Cards in any duplicate cluster are "being merged" — they get no other fix.
    merge_claimed = set()
    for v in cluster_verdicts:
        if v.get("relation") == "duplicate":
            merge_claimed.update(v.get("cluster_ids", []))

    tier2_merges = ex.execute_merges(db_path, mutator, board, cluster_verdicts, settings, now_utc, now_iso, result)

    # In-scope archiving (item 2). Any card with an archive decision (executed or
    # proposed) is claimed → no date/label/title fix this run.
    tier2_inscope, archived_ids = ex.execute_inscope_archive(
        db_path, mutator, board, inscope_archive_verdicts, settings, now_utc, now_iso, result,
        skip_ids=merge_claimed)
    # A duplicate-reasoned archive verdict is dropped by the archive pass (it
    # belongs to merge); don't let it claim its card away from hygiene either.
    archive_claimed = {v["card_id"] for v in inscope_archive_verdicts
                       if not ex.is_duplicate_archive_reason(v.get("reason"))} | archived_ids
    claimed = merge_claimed | archive_claimed

    tier2_due = ex.execute_hygiene(db_path, mutator, board, hygiene_verdicts, flagged_ids, settings,
                                   now_utc, now_iso, result, dead_due_ids=dead_due_ids,
                                   spine_terms=spine_terms, skip_ids=claimed)
    tier2_labels = ex.execute_label_dispositions(db_path, mutator, board, hyg["stale_labels"],
                                                 label_verdicts, settings, now_utc, now_iso, result,
                                                 skip_ids=claimed)
    tier2_archives = ex.execute_recovery(db_path, mutator, board, recovery_verdicts, settings, now_iso, result)

    # Reprioritization runs AFTER merges/archives/hygiene/recovery, so the Today /
    # Next Few Days targets apply to the already-cleaned board (design §5.4).
    from phases import reprioritize as repri
    repri_verdicts = judgments.get("reprioritization")
    repri_unverdicted = 0
    if repri_verdicts is None:
        repri_verdicts, repri_unverdicted = _run_reprioritization_judgment(
            llm, prompts, spine, board, mutator, settings, now_utc)
    tier2_repri = repri.run_reprioritization(db_path, mutator, board, repri_verdicts, settings,
                                             spine, now_utc, now_iso, result,
                                             unverdicted=repri_unverdicted)

    tier2_actions = _collect_tier2(board, settings, tier2_merges, tier2_archives, tier2_due,
                                   tier2_labels, tier2_inscope)
    tier2_actions.extend(tier2_repri)
    ex.generate_proposals(db_path, mutator, board, tier2_actions, settings, now_iso, result)

    # The reprioritization "proposed" count must reflect proposals that ACTUALLY
    # opened (max_proposals_open can cap them), not the pre-cap intended count —
    # otherwise the Today plan header disagrees with "Awaiting your decision".
    if result.today_plan:
        result.today_plan["proposed"] = sum(
            1 for p in result.proposals_opened
            if p.get("type") in ("reprioritize_up", "reprioritize_down"))

    # Phase 5 — report
    stats = _health_stats(board, settings, in_scope_cards, wide_cards, scratch_ids, processed)
    approval_rates = _approval_rates(db_path)
    prev_stats = _load_prev_stats(db_path)
    report_text = rep.build_report(result, board, settings, now_iso, dry_run, first_run,
                                   stats=stats, approval_rates=approval_rates,
                                   prev_stats=prev_stats)
    rep.write_report_file(report_text, settings.report_file)
    rep.publish_report_card(mutator, board, settings, report_text)
    # Persist this run's health stats for next run's day-over-day deltas (a real
    # run only — a dry-run must never become the baseline the next run diffs).
    if not dry_run:
        storage.kv_set(db_path, "last_health_stats", json.dumps(stats))

    # Persist post-run snapshot (agent's intended end-state, so its own changes
    # are not mis-read as user edits next run). Skipped in dry-run — a simulated
    # end-state must never become the baseline a later real run diffs against.
    if not dry_run:
        storage.save_snapshot(db_path, run_id, now_iso, _post_snapshot(board, mutator))
    result.counters["board_writes"] = 0 if dry_run else len(mutator.log)
    return result, report_text, mutator


def _run_judgment(llm, prompts, spine, board, in_scope_cards, wide_cards, entity_keywords, hyg, rec_batch, settings):
    """Call the three LLM passes. Safe no-op structure if llm is None."""
    if llm is None or prompts is None:
        return {"clusters": [], "hygiene": [], "inscope_archive": [], "labels": [], "recovery": []}
    known_ids = board.all_card_ids()
    src_text = {c.id: f"{c.name}\n{c.desc}" for c in board.cards}
    spine_terms = spine.all_terms() if spine else []
    board_summary = f"{len(board.cards)} cards across {len(board.lists)} lists"
    prefix = judge.build_system_prefix(prompts, spine, board_summary, _RULES)

    narrow = cand.narrow_track(in_scope_cards, entity_keywords, settings)
    wide = cand.wide_track(in_scope_cards, wide_cards, entity_keywords, settings)
    clusters_payload = {"names": narrow["names"], "hints": narrow["hints"],
                        "wide_pairs": [{"a": p["a"].id, "b": p["b"].id} for p in wide]}
    hyg_payload = {"flagged": [{"id": c.id, "name": c.name} for c in hyg["flagged_renames"]],
                   "in_scope": [{"id": c.id, "name": c.name} for c in hyg["all_in_scope"]]}
    dead_dues = hyg["dead_dues"]
    due_payload = {"cards": [{"id": c.id, "name": c.name, "due": c.due,
                              "desc": (c.desc or "")[:300]} for c in dead_dues]}
    rec_payload = {"cards": [{"id": c.id, "name": c.name, "origin": c.list_name} for c in rec_batch]}

    recovery = judge.recovery_triage(llm, prompts, prefix, rec_payload, known_ids)
    recovery = _default_recovery(recovery, rec_batch)

    # Renames come from the hygiene pass; dead-due classification is its OWN call
    # with a per-card-scaled token budget so a large dead-due set is never
    # truncated. Both feed execute_hygiene as one list (rename entries and due
    # entries are disjoint per card).
    hygiene = judge.hygiene_pass(llm, prompts, prefix, hyg_payload, known_ids, src_text, spine_terms)
    if dead_dues:
        due_budget = min(8000, max(3000, 120 * len(dead_dues)))
        hygiene = hygiene + judge.classify_due(
            llm, prompts, prefix, due_payload, known_ids, src_text, spine_terms,
            max_tokens=due_budget)

    # In-scope "no longer needed" archive candidates: deterministic [Owner: X]
    # titles (confidence 100) plus the LLM's Done-workstream / passed-deadline
    # judgments. Deterministic entries win on dedup.
    inscope_cards = hyg["all_in_scope"]
    inscope_payload = {"cards": [{"id": c.id, "name": c.name, "list": c.list_name}
                                 for c in inscope_cards]}
    ia_budget = min(8000, max(3000, 60 * len(inscope_cards)))
    llm_archives = judge.classify_inscope_archive(llm, prompts, prefix, inscope_payload, known_ids,
                                                  max_tokens=ia_budget)
    inscope_archive = _merge_owner_archives(inscope_cards, llm_archives)

    # Stale-label disposition (swap vs remove); archive handled above.
    stale_cards = hyg["stale_labels"]
    labels_payload = {"cards": [{"id": c.id, "name": c.name, "stale_label": label,
                                 "list": c.list_name} for c, label in stale_cards]}
    label_verdicts = judge.classify_labels(llm, prompts, prefix, labels_payload, known_ids) \
        if stale_cards else []

    return {
        "clusters": judge.adjudicate_clusters(llm, prompts, prefix, clusters_payload, known_ids, src_text, spine_terms),
        "hygiene": hygiene,
        "inscope_archive": inscope_archive,
        "labels": label_verdicts,
        "recovery": recovery,
    }


def _run_reprioritization_judgment(llm, prompts, spine, board, mutator, settings, now_utc):
    """Reprioritization LLM pass — run AFTER execution so candidates reflect the
    cleaned board (post merge/archive/hygiene/recovery).

    Facts are pre-computed and the shortlist pre-ranked in Python (never a bulk
    generator); the LLM is a per-candidate validator that MUST return a verdict for
    every candidate. Returns (move_verdicts, unverdicted_count). Returns ([], 0)
    when the LLM is unavailable (tests inject move verdicts directly instead)."""
    if llm is None or prompts is None:
        return [], 0
    from phases import reprioritize as repri

    cands = repri.build_candidates(board, mutator, spine, settings, now_utc)
    shortlist = cands["promote"] + cands["demote"]
    if not shortlist:
        return [], 0
    known_ids = board.all_card_ids()
    board_summary = f"{len(board.cards)} cards across {len(board.lists)} lists"
    prefix = judge.build_system_prefix(prompts, spine, board_summary, _RULES)
    # Per-candidate token budget so a large shortlist is never truncated (the same
    # per-item-scaled budget that fixed the dead-due bulk-classification bug).
    budget = min(8000, max(3000, 120 * len(shortlist)))
    verdicts = judge.reprioritize_judge(llm, prompts, prefix, repri.judge_payload(cands),
                                        known_ids, max_tokens=budget)
    verdict_ids = {v.get("card_id") for v in verdicts}
    unverdicted = [c for c in shortlist if c["id"] not in verdict_ids]
    if unverdicted:
        logger.warning("Reprioritization: %d/%d candidate(s) unverdicted by the LLM",
                       len(unverdicted), len(shortlist))
    moves = [v for v in verdicts
             if v.get("verdict") == "move" and v.get("direction") and v.get("target_list")]
    logger.info("Reprioritization judge: %d move, %d keep, %d unverdicted",
                len(moves), len(verdicts) - len(moves), len(unverdicted))
    return moves, len(unverdicted)


def _merge_owner_archives(inscope_cards, llm_archives):
    """Combine deterministic [Owner: X] archive candidates with LLM verdicts.

    Every in-scope card whose title is '[Owner: Name] ...' is a certain archive
    candidate (confidence 100). LLM verdicts add Done-workstream / passed-deadline
    cards. Deterministic entries take precedence on dedup by card id."""
    import guardrails as g

    out = {}
    for v in llm_archives:
        out[v["card_id"]] = v
    for c in inscope_cards:
        if g.is_owner_titled(c.name):
            out[c.id] = {"card_id": c.id, "confidence": 100, "borderline": False,
                         "reason": "Titled '[Owner: …]' — a delegated/handed-off item."}
    return list(out.values())


def _default_recovery(recovery_verdicts, rec_batch):
    """Ensure every card in the recovery batch drains: any card the LLM did not
    dispose defaults to Inbox / Triage (the design's ambiguous-context default),
    so a non-empty batch never silently produces zero dispositions."""
    disposed = {v.get("card_id") for v in recovery_verdicts}
    out = list(recovery_verdicts)
    for c in rec_batch:
        if c.id not in disposed:
            out.append({"card_id": c.id, "disposition": "inbox",
                        "reason": "No disposition returned; routed to Inbox / Triage by default."})
    return out


def _collect_tier2(board, settings, tier2_merges, tier2_archives, tier2_due, tier2_labels,
                   tier2_inscope):
    """Assemble Tier-2 action dicts to become Agent: Proposed cards.

    tier2_due / tier2_labels / tier2_inscope are already full action dicts (with
    confidence and reason); merges and recovery archives are converted here.
    """
    actions = []
    for v in tier2_merges:
        cids = v.get("cluster_ids", [])
        actions.append({
            "type": "merge", "card_ids": cids, "new_name": v.get("new_name"),
            "survivor_id": v.get("survivor_id"), "anchor_card_id": v.get("survivor_id"),
            "confidence": v.get("confidence"),
            "reason": v.get("reason", "Likely duplicate — recommend merging (review)."),
        })
    actions.extend(tier2_inscope)
    actions.extend(tier2_due)
    actions.extend(tier2_labels)
    for v in tier2_archives:
        actions.append({
            "type": "recovery_archive", "card_ids": [v["card_id"]], "anchor_card_id": v["card_id"],
            "confidence": v.get("confidence"),
            "reason": v.get("reason", "Appears no longer needed — recommend archiving (review)."),
        })
    return actions


def _health_stats(board, settings, in_scope_cards, wide_cards, scratch_ids, processed):
    flagged = sum(1 for c in in_scope_cards
                  if g.is_name_flagged(c.name, settings.name_min_length, settings.name_max_length))
    total = len(in_scope_cards)
    coverage = round(100.0 * (total - flagged) / total, 1) if total else 100.0
    scratch_backlog = sum(len(board.cards_in_list(lid)) for lid in scratch_ids) - len(processed)
    scratch_lists = [l.name for l in sd.recovery_source_lists(board, settings)]
    return {
        "open_clusters": "n/a",
        "scratch_backlog": max(scratch_backlog, 0),
        "hygiene_coverage_pct": coverage,
        "scratch_lists": scratch_lists,
    }


def _load_prev_stats(db_path) -> dict:
    """Prior run's health stats (for day-over-day deltas), or {} if none/unreadable."""
    raw = storage.kv_get(db_path, "last_health_stats")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Could not parse stored health stats; skipping deltas")
        return {}


def _approval_rates(db_path):
    props = []
    for status in ("approved", "rejected"):
        with storage.db_connection(db_path) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM proposals WHERE status = ?", (status,)).fetchone()
        props.append(int(row["n"]))
    approved, rejected = props
    total = approved + rejected
    rate = f"{round(100.0 * approved / total, 1)}%" if total else "n/a"
    return {"approved": approved, "rejected": rejected, "rate": rate}


def _post_snapshot(board, mutator):
    """Apply the mutator log to in-memory cards so the snapshot reflects the true
    post-run board state.

    Must cover EVERY field the diff compares (name, desc_hash, label_names, due,
    list_id) — otherwise a live action leaves the saved snapshot out of sync with
    the board and the next run misreads it as a user reversal (a phantom
    rejection that permanently suppresses a legitimate action).
    """
    import copy

    id_to_name = {lb.get("id"): lb.get("name", "") for lb in board.labels}
    cards = {c.id: copy.copy(c) for c in board.cards}
    for entry in mutator.log:
        cid = entry.get("card_id")
        if cid not in cards:
            continue
        op = entry["op"]
        if op == "rename":
            cards[cid].name = entry["new_name"]
        elif op == "set_description":
            if "desc" in entry:
                cards[cid].desc = entry["desc"]
        elif op == "set_labels":
            cards[cid].label_ids = list(entry["label_ids"])
            cards[cid].label_names = [id_to_name.get(i, "") for i in entry["label_ids"]]
        elif op == "move_card":
            cards[cid].list_id = entry["target_list_id"]
        elif op == "clear_due":
            cards[cid].due = None
        elif op == "set_due":
            cards[cid].due = entry.get("due")
    return list(cards.values())


_RULES = (
    "You groom a Trello board. All card ids, list names, and dates are FACTS given "
    "to you; never invent people or entities not present in the source cards or spine. "
    "Return only schema-valid JSON. Judge duplicates, compose merged names/descriptions, "
    "clean names, and triage recovered cards per the constraints provided."
)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(argv=None) -> int:
    args = parse_args(argv)
    settings = load_settings(AGENT_CONFIG_PATH)
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    creds = load_credentials(ENV_CONFIG_PATH)

    storage.init_storage(settings.db_path)
    if args.reset_state:
        deleted = storage.reset_state(settings.db_path)
        logger.info("State reset complete: %s", deleted)
        print("Reset agent state (no board data touched):")
        for table, n in deleted.items():
            print(f"  {table}: {n} row(s) cleared")
        return 0
    if storage.is_paused(settings.db_path):
        logger.error("Agent is PAUSED (three consecutive failures). See README to clear.")
        send_alert("[Agent Alert] work-todo-trello-grooming-agent: PAUSED",
                   "The agent is paused after repeated failures and will not run until cleared.",
                   creds.gmail_sender, creds.gmail_password, creds.gmail_recipient or None)
        return 2

    dry_run = settings.dry_run or args.dry_run
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    try:
        trello, notion, llm, prompts = build_clients(creds, settings)
        board = fetch_board(trello, settings)
        try:
            resolve_ids_or_fail(board, settings)
        except RuntimeError as exc:
            logger.error("Startup name resolution failed: %s", exc)
            send_alert(f"[Agent Alert] {AGENT_NAME}: STARTUP FAILURE", str(exc),
                       creds.gmail_sender, creds.gmail_password, creds.gmail_recipient or None)
            return 3

        first_run = storage.latest_prior_run_id(settings.db_path, now_iso) is None
        try:
            spine = read_spine(notion, settings.spine_page_id)
        except Exception as exc:  # spine read failure is non-fatal; degrade to no spine
            logger.warning("Spine read failed (%s); continuing without spine", exc)
            spine = None

        # Live config: spine "Rules and thresholds" overrides this run's settings.
        from spine import apply_notion_overrides
        notion_notes, dry_run_from_notion = apply_notion_overrides(settings, spine)
        if dry_run_from_notion:
            logger.info("dry_run set to true by the Notion Rules section")
        dry_run = settings.dry_run or args.dry_run or dry_run_from_notion

        run_pipeline(board, settings, settings.db_path, now_utc, dry_run, first_run,
                     llm=llm, prompts=prompts, spine=spine, trello=trello,
                     notion_notes=notion_notes)
        storage.record_success(settings.db_path, now_iso)
        logger.info("Run complete (dry_run=%s)", dry_run)
        return 0
    except Exception as exc:
        failures, paused = storage.record_failure(settings.db_path, now_iso, settings.auto_pause_after_failures)
        logger.error("Run failed (%d consecutive; paused=%s)", failures, paused)
        send_crash_alert(AGENT_NAME, exc, traceback.format_exc(),
                         creds.gmail_sender, creds.gmail_password, creds.gmail_recipient or None)
        raise


if __name__ == "__main__":
    sys.exit(run())
