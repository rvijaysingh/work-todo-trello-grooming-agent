"""
Phase 5 — Run report.

Builds the "Grooming Report" with the five canonical sections (Still overdue and
possibly urgent, Awaiting your decision, Recently archived, Done automatically,
Health stats) and, in dry-run or first-run, prepends the pre-first-run reminder
to ARCHIVE-prefix any Scratch list that should be out of recovery scope.

The report is always written to a local file; the report card is replaced at the
top of report_list via the Mutator (a no-op in dry-run — zero board writes).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

REPORT_CARD_PREFIX = "Grooming Report"

_ARCHIVE_REMINDER = (
    "PRE-FIRST-RUN REMINDER: Decide which Scratch lists stay in recovery scope. "
    "Rename any list you want EXCLUDED with an 'ARCHIVE ' prefix (e.g. "
    "'ARCHIVE Scratch 5-12'). Current Scratch lists in scope are listed under "
    "Health below. Note 'Scratch 5-12' sits near the ~8-week boundary."
)


def build_report(result, board, settings, now_iso, dry_run, first_run,
                 stats=None, approval_rates=None) -> str:
    """Assemble the Grooming Report text with the five canonical sections."""
    stats = stats or {}
    approval_rates = approval_rates or {}
    lines: list[str] = []
    lines.append(f"{REPORT_CARD_PREFIX} — {now_iso}")
    lines.append(f"Mode: {'DRY-RUN (no board changes)' if dry_run else 'LIVE'}")
    notion_notes = getattr(result, "notion_notes", []) or []
    if any("dry_run" in n for n in notion_notes):
        lines.append("Note: dry_run was set by the Notion 'Rules and thresholds' section.")
    lines.append("")

    if dry_run or first_run:
        lines.append(_ARCHIVE_REMINDER)
        lines.append("")

    # 1. Still overdue and possibly urgent -----------------------------------
    lines.append("== Still overdue and possibly urgent ==")
    if result.still_overdue:
        for s in result.still_overdue:
            reason = f" — {s['reason']}" if s.get("reason") else ""
            lines.append(f"  - {_ref(s.get('name'), s.get('url'))} (due {s['due']}){reason}")
    else:
        lines.append("  (none)")
    lines.append("")

    # 2. Awaiting your decision ----------------------------------------------
    lines.append("== Awaiting your decision ==")
    if result.proposals_opened:
        for p in result.proposals_opened:
            lines.append(f"  - {_ref(p.get('title'), p.get('url'))}")
            lines.append(f"      proposed: {p.get('action_desc', p.get('type'))}")
            if p.get("reason"):
                lines.append(f"      reason: {p['reason']}")
            conf = p.get("confidence")
            lines.append(f"      Confidence: {int(conf)}%" if conf is not None
                         else "      Confidence: n/a")
            lines.append("      → reply 'yes'/'approve' on the card, or remove the label to reject")
    else:
        lines.append("  (none)")
    if result.expired_proposals:
        for p in result.expired_proposals:
            lines.append(f"  - expired (timed out, dropped): proposal #{p['proposal_id']} ({p.get('fingerprint','')})")
    lines.append("")

    # 3. Recently archived ----------------------------------------------------
    lines.append("== Recently archived ==")
    lines.append(f"  (cards moved to the Agent Archive list this run — visible "
                 f"{settings.archive_list_days} days — and cards nearing their move to "
                 f"Trello's archive (restorable))")
    if result.recently_archived:
        for a in result.recently_archived:
            lines.append(f"  - {_ref(a.get('name'), a.get('url'))} — {a.get('note', '')}")
    else:
        lines.append("  (none)")
    lines.append("")

    # 4. Done automatically ---------------------------------------------------
    lines.append("== Done automatically ==")
    verb = _verbs(dry_run)
    if result.reminder_created:
        lines.append(f"  - {verb['create']} the weekly spine-review reminder card")
    if result.applied:
        for a in result.applied:
            lines.append(f"  - {_describe(a, board, verb)}")
    if not result.applied and not result.reminder_created:
        lines.append("  (none)")
    if result.demoted_recoveries:
        for d in result.demoted_recoveries:
            lines.append(f"  - recovery demoted Today→Next Few Days by cap: {d['name']}")
    lines.append("")

    # 5. Health stats ---------------------------------------------------------
    lines.append("== Health stats ==")
    lines.append(f"  Scratch backlog count: {stats.get('scratch_backlog', 'n/a')}")
    lines.append(f"  Hygiene coverage (in-scope): {stats.get('hygiene_coverage_pct', 'n/a')}%")
    if stats.get("scratch_lists"):
        lines.append(f"  Scratch lists in scope: {', '.join(stats['scratch_lists'])}")
    lines.append(f"  Tier-2 approvals: {approval_rates.get('approved', 0)}  "
                 f"rejections: {approval_rates.get('rejected', 0)}  "
                 f"rate: {approval_rates.get('rate', 'n/a')}")
    lines.append(f"  Auto-mode: tier1_stale_label_removal={settings.tier1_stale_label_removal}  "
                 f"tier1_recovery_archive={settings.tier1_recovery_archive}  "
                 f"tier1_due_date_clear={settings.tier1_due_date_clear}")
    if result.rejections_recorded:
        lines.append(f"  Rejections detected this run: {len(result.rejections_recorded)}")
    for note in notion_notes:
        lines.append(f"  {note}")

    return "\n".join(lines) + "\n"


def _truncate(text: str, limit: int = 60) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _ref(title, url) -> str:
    """Render a card as 'truncated title (url)' — never a bare id."""
    label = _truncate(title or "(untitled)")
    return f"{label} ({url})" if url else label


def _card_ref(board, card_id: str) -> str:
    card = board.card_by_id(card_id) if card_id else None
    if card is None:
        return _truncate(card_id)
    return _ref(card.name, card.url)


def _verbs(dry_run: bool) -> dict:
    """Verb forms so dry-run reads 'would create/move/remove/clear', live reads past tense."""
    if dry_run:
        return {"create": "would create", "move": "would move", "remove": "would remove",
                "rename": "would rename", "clear": "would clear/re-date", "merge": "would merge",
                "archive": "would move to Trello's archive", "swap": "would swap"}
    return {"create": "created", "move": "moved", "remove": "removed",
            "rename": "renamed", "clear": "cleared/re-dated", "merge": "merged",
            "archive": "moved to Trello's archive", "swap": "swapped"}


def _describe(action: dict, board, verb: dict) -> str:
    t = action.get("type")
    cid = action.get("card_id") or action.get("survivor_id", "")
    ref = _card_ref(board, cid)
    if t == "merge":
        losers = action.get("loser_ids", [])
        head = f"{verb['merge']} {len(losers)} duplicate(s) into survivor {_card_ref(board, action.get('survivor_id'))}"
        subs = [f"      merged away: {_card_ref(board, lid)}" for lid in losers]
        return "\n".join([head] + subs)
    if t == "rename":
        return f"{verb['rename']} {ref} → '{action.get('new_name')}'"
    if t == "stale_label_removal":
        lbl = action.get("label", "a stale label")
        return f"{verb['remove']} label '{lbl}' from {ref}"
    if t == "label_swap":
        return f"{verb['swap']} label '{action.get('label')}' → '{action.get('target_label')}' on {ref}"
    if t == "inscope_archive":
        return f"{verb['move']} {ref} to the Agent Archive list (no longer needed)"
    if t == "dead_due_clear":
        return f"{verb['clear']} the long-overdue due date on {ref}"
    if t == "due_redate":
        return f"{verb['clear']} the overdue due date on {ref} (new due {action.get('new_due')})"
    if t == "recovery_route":
        return f"{verb['move']} {ref} from {action.get('origin','?')} → {action.get('dest','?')} (recovered)"
    if t == "recovery_archive":
        return f"{verb['move']} {ref} to the Agent Archive list (recovered, no longer needed)"
    if t == "recovery_merge":
        return f"{verb['merge']} recovered {ref} into an active card"
    if t == "trello_archive":
        return f"{verb['archive']} {ref} (60+ days in the Agent Archive list)"
    if t == "label_expiry":
        return f"{verb['remove']} the aged Agent: Auto-Updated label from {ref}"
    if t and t.startswith("approved_"):
        return f"executed approved proposal ({t[len('approved_'):]}) on {ref}"
    return f"{t} {ref}"


def write_report_file(text: str, path: str) -> None:
    """Write the report to a local file (always, including dry-run)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    logger.info("Report written to %s", path)


def publish_report_card(mutator, board, settings, text: str) -> None:
    """Replace the Grooming Report card at the top of report_list (dry-run: no-op writes)."""
    report_list = board.list_by_name(settings.report_list)
    if report_list is None:
        logger.warning("report_list %r not found; skipping report card", settings.report_list)
        return
    existing = None
    for c in board.cards_in_list(report_list.id):
        if c.name.strip().lower().startswith(REPORT_CARD_PREFIX.lower()):
            existing = c
            break
    title = f"{REPORT_CARD_PREFIX}"
    if existing is not None:
        mutator.set_description(existing.id, text)
    else:
        mutator.create_card(report_list.id, title, text)
