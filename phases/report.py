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
            lines.append(f"  - {s['name']} (due {s['due']}){reason}")
    else:
        lines.append("  (none)")
    lines.append("")

    # 2. Awaiting your decision ----------------------------------------------
    lines.append("== Awaiting your decision ==")
    if result.proposals_opened:
        for p in result.proposals_opened:
            lines.append(f"  - [{p['type']}] proposal #{p['proposal_id']} — reply 'yes'/'approve' or remove the label")
    else:
        lines.append("  (none)")
    if result.expired_proposals:
        for p in result.expired_proposals:
            lines.append(f"  - expired (timed out, dropped): proposal #{p['proposal_id']} ({p.get('fingerprint','')})")
    lines.append("")

    # 3. Recently archived ----------------------------------------------------
    lines.append("== Recently archived ==")
    lines.append("  (cards moved to the Agent Archive list this run, and cards nearing "
                 "their 60-day move to Trello's archive (restorable))")
    if result.recently_archived:
        for a in result.recently_archived:
            lines.append(f"  - {a['name']} — {a.get('note', '')}")
    else:
        lines.append("  (none)")
    lines.append("")

    # 4. Done automatically ---------------------------------------------------
    lines.append("== Done automatically ==")
    if result.reminder_created:
        lines.append("  - created the weekly spine-review reminder card")
    if result.applied:
        for a in result.applied:
            lines.append(f"  - {_describe(a, board)}")
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


def _describe(action: dict, board) -> str:
    t = action.get("type")
    cid = action.get("card_id") or action.get("survivor_id", "")
    card = board.card_by_id(cid) if cid else None
    link = f" ({card.url})" if card and card.url else ""
    if t == "merge":
        return f"merge → survivor {action.get('survivor_id')} ({len(action.get('loser_ids', []))} loser(s)){link}"
    if t == "rename":
        return f"rename {cid} → {action.get('new_name')}{link}"
    return f"{t} {cid}{link}"


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
