"""
Phase 5 — Run report.

Builds the "Grooming Report" with the five canonical sections (Still overdue and
possibly urgent, Awaiting your decision, Recently archived, Done automatically,
Health stats).

Rendering conventions (see the report spec):
  - Every entry in every section is numbered #1, #2, … (restarting per section).
    Related cards under one decision share a number with sub-letters (#3a, #3b)
    and render consecutively; a blank line separates unrelated entries/groups.
  - Entry layout: line 1 "#N Card Name: <title>" + " (due M/D h:mmam)" in local
    time; line 2 the action line; line 3 (indented) "Card Labels: …"; line 4
    (indented) "Card Description: …" (first ~12 words). No card URLs anywhere.
  - Archive moves appear ONLY under "Recently archived"; "Done automatically"
    renders labeled subsections (Date fixes / Label changes / Renames / Recovered
    from Scratch / Other).
  - The PRE-FIRST-RUN reminder renders only in dry-run.

The report is always written to a local file; the report card is replaced at the
top of report_list via the Mutator (a no-op in dry-run — zero board writes).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta

import guardrails as g

logger = logging.getLogger(__name__)

REPORT_CARD_PREFIX = "Grooming Report"
_INDENT = "   "

# Description excerpts must carry NO URLs (report rule). Markdown links
# "[text](url)" collapse to their text; any remaining bare URL is dropped.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")

_ARCHIVE_REMINDER = (
    "PRE-FIRST-RUN REMINDER: Decide which Scratch lists stay in recovery scope. "
    "Rename any list you want EXCLUDED with an 'ARCHIVE ' prefix (e.g. "
    "'ARCHIVE Scratch 5-12'). Current Scratch lists in scope are listed under "
    "Health below. Note 'Scratch 5-12' sits near the ~8-week boundary."
)

_DECISION_INSTRUCTION = (
    "To answer any proposal, do exactly one: comment 'yes' on the card (find it by "
    "the Agent: Proposed label or search the name in Trello) = approve; comment "
    "'no' or remove the label = reject; do it yourself = yes; ignore until the "
    "expiry date = no."
)

# "Done automatically" subsections, in render order. Archive MOVES are excluded
# entirely (they belong to "Recently archived"); everything else falls through to
# "Other".
_ARCHIVE_MOVE_TYPES = {"inscope_archive", "recovery_archive", "trello_archive"}
_SUBSECTIONS = [
    ("Date fixes", {"dead_due_clear", "due_redate"}),
    ("Label changes", {"stale_label_removal", "label_swap", "label_expiry"}),
    ("Renames", {"rename"}),
    ("Recovered from Scratch", {"recovery_route", "recovery_merge"}),
    ("Other", None),  # catch-all: merges, reminder creation, approved proposals
]


# ---------------------------------------------------------------------------
# Local-time / value formatting
# ---------------------------------------------------------------------------

def _fmt_local_dt(local) -> str:
    """Format a naive local datetime as 'M/D h:mmam' (e.g. '6/27 11:00am')."""
    hour = local.hour % 12 or 12
    ampm = "am" if local.hour < 12 else "pm"
    return f"{local.month}/{local.day} {hour}:{local.minute:02d}{ampm}"


def _fmt_due(due_iso, settings) -> str:
    dt = g.parse_utc(due_iso)
    if dt is None:
        return ""
    local = g._local(dt, settings.tz_standard_offset, settings.tz_daylight_offset)
    return _fmt_local_dt(local)


def _expiry_date(now_iso, settings) -> str:
    """Local M/D date proposal_timeout_days after now (a proposal's expiry)."""
    dt = g.parse_utc(now_iso)
    if dt is None:
        return ""
    local = g._local(dt + timedelta(days=settings.proposal_timeout_days),
                     settings.tz_standard_offset, settings.tz_daylight_offset)
    return f"{local.month}/{local.day}"


def _delta_str(delta) -> str:
    return f"+{delta}" if delta >= 0 else f"{delta}"


# ---------------------------------------------------------------------------
# Entry building blocks
# ---------------------------------------------------------------------------

def _card(board, card_id):
    return board.card_by_id(card_id) if card_id else None


def _entry_header(num_label: str, title: str, due_iso, settings) -> str:
    title = (title or "(untitled)").strip()
    due = _fmt_due(due_iso, settings)
    suffix = f" (due {due})" if due else ""
    return f"{num_label} Card Name: {title}{suffix}"


def _labels_line(card):
    names = [n for n in (card.label_names if card else []) if n]
    return f"{_INDENT}Card Labels: " + "; ".join(names) if names else None


def _desc_line(card, max_words: int = 12):
    if not card or not card.desc:
        return None
    text = _MD_LINK_RE.sub(r"\1", card.desc)   # keep link text, drop the URL
    text = _URL_RE.sub("", text)               # drop any bare URL
    words = [w for w in text.split() if w]
    if not words:
        return None
    shown = " ".join(words[:max_words])
    if len(words) > max_words:
        shown += "…"
    return f"{_INDENT}Card Description: {shown}"


def _labels_desc(card) -> list[str]:
    out = []
    ll = _labels_line(card)
    if ll:
        out.append(ll)
    dl = _desc_line(card)
    if dl:
        out.append(dl)
    return out


def _flush(lines: list[str], blocks: list[list[str]]) -> None:
    """Emit entry blocks separated by a single blank line, then one trailing blank."""
    for i, block in enumerate(blocks):
        lines.extend(block)
        if i < len(blocks) - 1:
            lines.append("")
    lines.append("")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(result, board, settings, now_iso, dry_run, first_run,
                 stats=None, approval_rates=None, prev_stats=None) -> str:
    """Assemble the Grooming Report text with the five canonical sections."""
    stats = stats or {}
    approval_rates = approval_rates or {}
    prev_stats = prev_stats or {}
    lines: list[str] = []

    auto_entries = [a for a in result.applied if a.get("type") not in _ARCHIVE_MOVE_TYPES]
    n_auto = len(auto_entries) + (1 if result.reminder_created else 0)

    # Header + at-a-glance --------------------------------------------------
    lines.append(f"{REPORT_CARD_PREFIX} — {now_iso}")
    lines.append(f"Mode: {'DRY-RUN (no board changes)' if dry_run else 'LIVE'}")
    notion_notes = getattr(result, "notion_notes", []) or []
    if any("dry_run" in n for n in notion_notes):
        lines.append("Note: dry_run was set by the Notion 'Rules and thresholds' section.")
    lines.append(
        f"At a glance: {len(result.still_overdue)} need your attention | "
        f"{len(result.proposals_opened)} proposals awaiting answer | "
        f"{len(result.recently_archived)} archived | {n_auto} automatic changes")
    lines.append("")

    if dry_run:
        lines.append(_ARCHIVE_REMINDER)
        lines.append("")

    _section_still_overdue(lines, result, board, settings)
    _section_awaiting(lines, result, board, settings, now_iso)
    _section_recently_archived(lines, result, board, settings, dry_run)
    _section_done_automatically(lines, result, board, settings, dry_run, auto_entries, n_auto)
    _section_health(lines, result, settings, stats, approval_rates, prev_stats, notion_notes)

    return "\n".join(lines).rstrip("\n") + "\n"


def _section_still_overdue(lines, result, board, settings):
    lines.append(f"== Still overdue and possibly urgent ({len(result.still_overdue)}) ==")
    if not result.still_overdue:
        lines.append("(none)")
        lines.append("")
        return
    blocks = []
    for i, s in enumerate(result.still_overdue, 1):
        card = _card(board, s.get("card_id"))
        title = s.get("name") or (card.name if card else s.get("card_id"))
        block = [_entry_header(f"#{i}", title, s.get("due"), settings),
                 f"Action needed: {(s.get('reason') or 'Review.').rstrip('.')}."]
        block.extend(_labels_desc(card))
        blocks.append(block)
    _flush(lines, blocks)


def _section_awaiting(lines, result, board, settings, now_iso):
    lines.append(f"== Awaiting your decision ({len(result.proposals_opened)}) ==")
    if result.proposals_opened:
        lines.append(_DECISION_INSTRUCTION)
        lines.append("")
        ordered = sorted(
            result.proposals_opened,
            key=lambda p: p.get("confidence") if p.get("confidence") is not None else -1,
            reverse=True)
        blocks = [_proposal_block(i, p, board, settings, now_iso)
                  for i, p in enumerate(ordered, 1)]
        _flush(lines, blocks)
    else:
        lines.append("(none)")
        lines.append("")
    if result.expired_proposals:
        for p in result.expired_proposals:
            lines.append(f"Expired (timed out, dropped): proposal #{p.get('proposal_id')} "
                         f"({p.get('fingerprint', '')})")
        lines.append("")


def _proposal_block(num, p, board, settings, now_iso) -> list[str]:
    """One proposal decision. Multi-card decisions (merges) render as #Na, #Nb."""
    card_ids = list(p.get("card_ids") or [p.get("card_id")])
    ordered: list[str] = []
    for pref in (p.get("survivor_id"), p.get("card_id")):
        if pref and pref in card_ids and pref not in ordered:
            ordered.append(pref)
    for cid in card_ids:
        if cid and cid not in ordered:
            ordered.append(cid)

    action_line = _proposal_action_line(p, settings, now_iso)
    multi = len(ordered) > 1
    block: list[str] = []
    for idx, cid in enumerate(ordered):
        label = f"#{num}{chr(ord('a') + idx)}" if multi else f"#{num}"
        card = _card(board, cid)
        title = (card.name if card else None) or (p.get("title") if cid == p.get("card_id") else cid)
        block.append(_entry_header(label, title, card.due if card else None, settings))
        if idx == 0:
            block.append(action_line)
        block.extend(_labels_desc(card))
    return block


def _proposal_action_line(p, settings, now_iso) -> str:
    action = (p.get("action_desc") or p.get("type") or "review").rstrip(".")
    reason = (p.get("reason") or "").rstrip(".")
    conf = p.get("confidence")
    conf_s = f"{int(conf)}%" if conf is not None else "n/a"
    parts = [f"Proposed: {action}."]
    if reason:
        parts.append(f"Reason: {reason}.")
    parts.append(f"Confidence: {conf_s}.")
    expires = _expiry_date(now_iso, settings)
    if expires:
        parts.append(f"Expires {expires}.")
    return " ".join(parts)


def _section_recently_archived(lines, result, board, settings, dry_run):
    verb_move = "would move" if dry_run else "moved"
    lines.append(f"== Recently archived ({len(result.recently_archived)}) ==")
    lines.append(f"({verb_move} to the Agent Archive list — visible {settings.archive_list_days} "
                 f"days, then Trello's restorable archive)")
    if not result.recently_archived:
        lines.append("(none)")
        lines.append("")
        return
    lines.append("")
    blocks = []
    for i, a in enumerate(result.recently_archived, 1):
        card = _card(board, a.get("card_id"))
        title = a.get("name") or (card.name if card else a.get("card_id"))
        block = [_entry_header(f"#{i}", title, card.due if card else None, settings),
                 f"Reason: {(a.get('reason') or a.get('note') or 'Archived').rstrip('.')}."]
        block.extend(_labels_desc(card))
        blocks.append(block)
    _flush(lines, blocks)


def _section_done_automatically(lines, result, board, settings, dry_run, auto_entries, n_auto):
    lines.append(f"== Done automatically ({n_auto}) ==")
    if n_auto == 0:
        lines.append("(none)")
        lines.append("")
        return

    buckets = {name: [] for name, _ in _SUBSECTIONS}
    for a in auto_entries:
        for name, types in _SUBSECTIONS:
            if types is None or a.get("type") in types:
                buckets[name].append(a)
                break
    if result.reminder_created:
        buckets["Other"].append({"type": "reminder_created"})

    lines.append("")
    counter = 0
    for name, _ in _SUBSECTIONS:
        items = buckets[name]
        if not items:
            continue
        lines.append(f"-- {name} --")
        blocks = []
        for a in items:
            counter += 1
            blocks.append(_done_block(counter, a, board, settings, dry_run))
        _flush(lines, blocks)


def _done_block(num, a, board, settings, dry_run) -> list[str]:
    verb = "Would" if dry_run else "Did"
    if a.get("type") == "reminder_created":
        return [f"#{num} Card Name: Weekly spine-review reminder card",
                f"{verb}: create the weekly spine-review reminder card"]
    cid = a.get("card_id") or a.get("survivor_id")
    card = _card(board, cid)
    title = (card.name if card else None) or cid or "(action)"
    block = [_entry_header(f"#{num}", title, card.due if card else None, settings),
             f"{verb}: {_auto_phrase(a, board, settings)}"]
    block.extend(_labels_desc(card))
    return block


def _auto_phrase(a, board, settings) -> str:
    t = a.get("type")
    if t == "merge":
        survivor = _card(board, a.get("survivor_id"))
        n = len(a.get("loser_ids", []))
        sname = survivor.name if survivor else a.get("survivor_id", "")
        return f"merge {n} duplicate(s) into survivor '{sname}'"
    if t == "rename":
        return f"rename to '{a.get('new_name')}'"
    if t == "stale_label_removal":
        return f"remove stale label '{a.get('label', 'a stale label')}'"
    if t == "label_swap":
        return f"swap label '{a.get('label')}' → '{a.get('target_label')}'"
    if t == "label_expiry":
        return "remove the aged Agent: Auto-Updated label"
    if t == "dead_due_clear":
        return "clear the long-overdue due date"
    if t == "due_redate":
        new_due = _fmt_due(a.get("new_due"), settings) or a.get("new_due")
        return f"re-date the overdue due date (new due {new_due})"
    if t == "recovery_route":
        return f"recover from {a.get('origin', '?')} → {a.get('dest', '?')}"
    if t == "recovery_merge":
        surv = _card(board, a.get("survivor_id"))
        return f"recover from {a.get('origin', '?')} and merge into '{surv.name if surv else ''}'"
    if t and t.startswith("approved_"):
        return f"execute approved proposal ({t[len('approved_'):]})"
    return t or "review"


def _section_health(lines, result, settings, stats, approval_rates, prev_stats, notion_notes):
    lines.append("== Health stats ==")

    sb = stats.get("scratch_backlog", "n/a")
    sb_line = f"Scratch backlog count: {sb}"
    prev_sb = prev_stats.get("scratch_backlog")
    if isinstance(sb, (int, float)) and isinstance(prev_sb, (int, float)):
        delta = sb - prev_sb
        new_lists = [l for l in stats.get("scratch_lists", [])
                     if l not in set(prev_stats.get("scratch_lists", []))]
        extra = ""
        if new_lists:
            extra = "; new list " + ", ".join(f"'{l}'" for l in new_lists) + " entered scope"
        sb_line += f" ({_delta_str(delta)}{extra})"
    lines.append(sb_line)

    hc = stats.get("hygiene_coverage_pct", "n/a")
    hc_line = f"Hygiene coverage (in-scope): {hc}%"
    prev_hc = prev_stats.get("hygiene_coverage_pct")
    if isinstance(hc, (int, float)) and isinstance(prev_hc, (int, float)):
        hc_line += f" ({_delta_str(round(hc - prev_hc, 1))})"
    lines.append(hc_line)

    if stats.get("scratch_lists"):
        lines.append(f"Scratch lists in scope: {', '.join(stats['scratch_lists'])}")
    lines.append(f"Tier-2 approvals: {approval_rates.get('approved', 0)}  "
                 f"rejections: {approval_rates.get('rejected', 0)}  "
                 f"rate: {approval_rates.get('rate', 'n/a')}")
    lines.append(f"Auto-mode: tier1_stale_label_removal={settings.tier1_stale_label_removal}  "
                 f"tier1_recovery_archive={settings.tier1_recovery_archive}  "
                 f"tier1_due_date_clear={settings.tier1_due_date_clear}")
    if result.rejections_recorded:
        lines.append(f"Rejections detected this run: {len(result.rejections_recorded)}")
    for note in notion_notes:
        lines.append(note)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

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
    if existing is not None:
        mutator.set_description(existing.id, text)
    else:
        mutator.create_card(report_list.id, REPORT_CARD_PREFIX, text)
