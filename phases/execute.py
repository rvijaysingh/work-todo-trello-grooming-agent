"""
Phase 4 — Validated execution.

Python enforces every guardrail (design.md §6) AFTER LLM judgment and BEFORE any
Trello write. All mutations go through BoardMutator, which performs zero board
writes in dry-run mode (the report is still produced). Tier 1 actions execute;
Tier 2 actions become Agent: Proposed cards; nothing is ever hard-deleted and
nothing reaches Trello's built-in archive without first passing through the
single Agent Archive list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import guardrails as g
import storage
from guardrails import assign_tier, fingerprint

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mutation boundary
# ---------------------------------------------------------------------------

class BoardMutator:
    """The single seam for board writes. In dry-run it performs NO Trello calls.

    Every call is appended to `self.log` (op name + kwargs) for the report and
    for test assertions, regardless of mode.
    """

    def __init__(self, trello_client, dry_run: bool):
        self.trello = trello_client
        self.dry_run = dry_run
        self.log: list[dict] = []

    def _record(self, op: str, **kwargs) -> None:
        self.log.append({"op": op, **kwargs})
        logger.info("%s %s %s", "[dry-run]" if self.dry_run else "[live]", op, kwargs)

    def rename(self, card_id: str, new_name: str) -> None:
        self._record("rename", card_id=card_id, new_name=new_name)
        if not self.dry_run:
            self.trello.update_card(card_id, name=new_name)

    def set_description(self, card_id: str, desc: str) -> None:
        self._record("set_description", card_id=card_id)
        if not self.dry_run:
            self.trello.update_card(card_id, description=desc)

    def clear_due(self, card_id: str) -> None:
        self._record("clear_due", card_id=card_id)
        if not self.dry_run:
            # Empty string clears the due date via the Trello API.
            self.trello.update_card(card_id, due_date="")

    def set_due(self, card_id: str, due_iso: str) -> None:
        self._record("set_due", card_id=card_id, due=due_iso)
        if not self.dry_run:
            self.trello.update_card(card_id, due_date=due_iso)

    def set_labels(self, card_id: str, label_ids: list[str]) -> None:
        self._record("set_labels", card_id=card_id, label_ids=list(label_ids))
        if not self.dry_run:
            self.trello.update_card(card_id, label_ids=list(label_ids))

    def add_comment(self, card_id: str, text: str) -> None:
        self._record("add_comment", card_id=card_id, text=text)
        if not self.dry_run:
            self.trello.add_comment(card_id, text)

    def move_card(self, card_id: str, target_list_id: str, position="top") -> None:
        self._record("move_card", card_id=card_id, target_list_id=target_list_id, position=position)
        if not self.dry_run:
            self.trello.move_card(card_id, target_list_id, position)

    def archive_card(self, card_id: str) -> None:
        self._record("archive_card", card_id=card_id)
        if not self.dry_run:
            self.trello.update_card(card_id, closed=True)

    def create_card(self, list_id: str, name: str, description: str) -> dict:
        self._record("create_card", list_id=list_id, name=name)
        if not self.dry_run:
            return self.trello.create_card(list_id, name, description)
        return {"id": "dry-run", "url": ""}

    def create_list(self, name: str, position: str = "bottom") -> dict:
        self._record("create_list", name=name, position=position)
        if not self.dry_run:
            lst = self.trello.create_list(name, position)
            return {"id": lst.id, "name": lst.name, "pos": lst.position}
        return {"id": "dry-run-archive-list", "name": name, "pos": 9999.0}


@dataclass
class ExecutionResult:
    applied: list = field(default_factory=list)
    proposals_opened: list = field(default_factory=list)
    rejections_recorded: list = field(default_factory=list)
    recently_archived: list = field(default_factory=list)   # moved this run + approaching 60d
    still_overdue: list = field(default_factory=list)        # dead-due but still matters
    recoveries: list = field(default_factory=list)
    demoted_recoveries: list = field(default_factory=list)
    expired_proposals: list = field(default_factory=list)
    reminder_created: bool = False
    notion_notes: list = field(default_factory=list)         # Notion Rules override notes
    notes: list = field(default_factory=list)
    counters: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Agent Archive list — single-list lifecycle (replaces the old quarantine list)
# ---------------------------------------------------------------------------

# Two distinct lifecycle stages, two distinct wordings:
#  - moving a card INTO the Agent Archive list this run (merge loser, recovery
#    archive, approved archive) → it stays visible there for archive_list_days;
#  - a card LEAVING the list after archive_list_days → Trello's built-in archive.
TRELLO_ARCHIVE_WORDING = "moved to Trello's archive (restorable)"


def archive_list_wording(settings) -> str:
    return f"moved to the Agent Archive list (visible {settings.archive_list_days} days)"


def ensure_archive_list(board, settings, mutator):
    """Return the Agent Archive list, creating it (positioned last) if absent.

    The list is excluded from edit, dedup-comparison, and recovery scopes by
    virtue of not being named in any of those config fields and not matching the
    recovery include pattern.
    """
    lst = board.list_by_name(settings.archive_list_name)
    if lst is not None:
        return lst
    from models import ListInfo

    created = mutator.create_list(settings.archive_list_name, position="bottom")
    info = ListInfo(id=created["id"], name=created.get("name", settings.archive_list_name),
                    closed=False, pos=float(created.get("pos", 9999.0)))
    board.lists.append(info)
    logger.info("Created Agent Archive list %s", info.id)
    return info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _time_label_ids(board) -> set[str]:
    return {lb["id"] for lb in board.labels if g.is_time_based_label(lb.get("name", ""))}


def _role_list_id(board, settings, role: str) -> str | None:
    """Resolve a recovery destination list id from configured scope by keyword."""
    keywords = {
        "today": ["today"],
        "next_few_days": ["next few"],
        "this_week": ["this week"],
        "inbox": ["inbox", "triage"],
    }[role]
    for name in settings.comparison_scope_lists:
        low = name.lower()
        if any(k in low for k in keywords):
            lst = board.list_by_name(name)
            if lst:
                return lst.id
    return None


def _in_scope_ids(board, settings) -> set[str]:
    ids = set()
    for name in settings.edit_scope_lists:
        lst = board.list_by_name(name)
        if lst:
            ids.add(lst.id)
    return ids


# ---------------------------------------------------------------------------
# Rejections & approvals
# ---------------------------------------------------------------------------

def process_rejections(db_path, mutator, board, rejections, settings, now_iso, result):
    """Record diff-detected rejections; strip Auto-Updated from edited cards."""
    for rej in rejections:
        if not mutator.dry_run:
            storage.add_rejection(db_path, rej["fingerprint"], rej["source"], now_iso)
        result.rejections_recorded.append(rej)
        if rej.get("remove_label"):
            card = board.card_by_id(rej["card_id"])
            auto_id = board.label_id(settings.label_auto_updated)
            if card and auto_id:
                remaining = [lid for lid in card.label_ids if lid != auto_id]
                mutator.set_labels(card.id, remaining)


def expire_proposals(db_path, board, open_proposals, settings, now_utc, now_iso, result, dry_run=False):
    """Expire open proposals older than proposal_timeout_days; fingerprint them."""
    for prop in open_proposals:
        if g.proposal_expired(prop.get("opened_ts"), now_utc, settings):
            if not dry_run:
                storage.set_proposal_status(db_path, prop["proposal_id"], "expired")
                storage.add_rejection(db_path, prop["fingerprint"], "timeout", now_iso)
            result.expired_proposals.append(prop)


# ---------------------------------------------------------------------------
# Quarantine lifecycle
# ---------------------------------------------------------------------------

def expire_labels_and_archive(db_path, mutator, board, settings, now_utc, result):
    """Strip aged Auto-Updated labels; Trello-archive aged Agent Archive cards.

    Cards in the Agent Archive list that have sat there longer than
    archive_list_days are archived via Trello's built-in (restorable) archive.
    Entry timestamps come from the SQLite archive_ledger (falling back to
    last_activity for any card lacking a ledger entry). Cards within ~10 days of
    their archive date are surfaced under "Recently archived" in the report.
    """
    archive_list = board.list_by_name(settings.archive_list_name)
    auto_id = board.label_id(settings.label_auto_updated)

    for card in board.cards:
        if card.closed:
            continue
        # Auto-Updated label expiry (cosmetic marker only).
        if auto_id and card.has_label(settings.label_auto_updated):
            if g.label_expired(card.last_activity, now_utc, settings):
                remaining = [lid for lid in card.label_ids if lid != auto_id]
                mutator.set_labels(card.id, remaining)
                result.applied.append({"type": "label_expiry", "card_id": card.id})
        # Agent Archive list lifecycle.
        if archive_list and card.list_id == archive_list.id:
            entered = storage.archive_entry_ts(db_path, card.id) or card.last_activity
            if g.archive_list_expired(entered, now_utc, settings):
                mutator.archive_card(card.id)
                if not mutator.dry_run:
                    storage.remove_archive_entry(db_path, card.id)
                result.applied.append({"type": "trello_archive", "card_id": card.id})
                result.recently_archived.append({"card_id": card.id, "name": card.name,
                                                 "url": card.url, "note": TRELLO_ARCHIVE_WORDING})
            else:
                days_in = g.days_since(entered, now_utc,
                                       settings.tz_standard_offset, settings.tz_daylight_offset) or 0
                days_left = settings.archive_list_days - days_in
                if days_left <= 10:
                    result.recently_archived.append(
                        {"card_id": card.id, "name": card.name, "url": card.url,
                         "note": f"{round(days_left, 1)} day(s) until Trello archive"})


# ---------------------------------------------------------------------------
# Merges
# ---------------------------------------------------------------------------

def compose_survivor_desc(survivor, losers) -> str:
    """Build the consolidated survivor description (guarantees the merge invariant).

    Holds task content only: the survivor's original title + description and, per
    source card, its original name and full description text. Audit metadata
    (links, "merged from" trail) lives in a card COMMENT, not the description, so
    the string-containment invariant never depends on audit text.
    """
    parts = [f"Original title: {survivor.name}", ""]
    if survivor.desc:
        parts += [survivor.desc, ""]
    for loser in losers:
        parts.append(f"From: {loser.name}")
        if loser.desc:
            parts.append(loser.desc)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _merge_audit_comment(survivor, losers, verdict, settings) -> str:
    """The audit-trail comment (links + confidence) written to the survivor."""
    src = ", ".join(f"{l.name} ({l.url or l.id})" for l in losers)
    line = f"Merged in {len(losers)} duplicate(s): {src}. Sources {archive_list_wording(settings)}."
    reason = verdict.get("reason")
    if reason:
        line += f" {reason}"
    conf = verdict.get("confidence")
    if conf is not None:
        line += f" Confidence: {int(conf)}%."
    return line


def execute_merge(db_path, mutator, board, verdict, settings, now_iso, result) -> bool:
    """Execute one merge. Returns True if applied, False if blocked.

    Enforces the merge string-containment invariant BEFORE moving any loser. The
    survivor keeps its own time-based labels; other labels are unioned across
    sources. Losers move to the TOP of the Agent Archive list (never hard-deleted,
    never Trello-archived directly), and their entry time is tracked in SQLite.
    """
    survivor = board.card_by_id(verdict["survivor_id"])
    if survivor is None:
        return False
    loser_ids = [cid for cid in verdict.get("cluster_ids", []) if cid != survivor.id]
    losers = [board.card_by_id(cid) for cid in loser_ids]
    losers = [l for l in losers if l is not None]
    if not losers:
        return False

    final_desc = compose_survivor_desc(survivor, losers)

    # INVARIANT: verify containment before any mutation.
    if not g.merge_contains_all_sources(final_desc, [survivor] + losers):
        logger.error("Merge invariant failed for survivor %s; aborting merge", survivor.id)
        result.notes.append(f"Merge aborted (invariant) for {survivor.id}")
        return False

    archive_list = board.list_by_name(settings.archive_list_name)
    if archive_list is None:
        result.notes.append("Agent Archive list missing; merge aborted")
        return False

    # Compose labels: survivor keeps its own time-based labels; union the rest.
    time_ids = _time_label_ids(board)
    survivor_time = [lid for lid in survivor.label_ids if lid in time_ids]
    nontime = set()
    for src in [survivor] + losers:
        nontime |= {lid for lid in src.label_ids if lid not in time_ids}
    auto_id = board.label_id(settings.label_auto_updated)
    proposed_id = board.label_id(settings.label_proposed)
    nontime.discard(proposed_id)
    final_labels = survivor_time + sorted(nontime)
    if auto_id and auto_id not in final_labels:
        final_labels.append(auto_id)

    # Apply survivor mutations (description = task content; audit trail = comment).
    if verdict.get("new_name"):
        mutator.rename(survivor.id, verdict["new_name"])
    mutator.set_description(survivor.id, final_desc)
    mutator.set_labels(survivor.id, final_labels)
    mutator.add_comment(survivor.id, _merge_audit_comment(survivor, losers, verdict, settings))

    # Move losers to the TOP of the Agent Archive list with a link back.
    for loser in losers:
        mutator.add_comment(
            loser.id,
            f"Merged into {survivor.url or survivor.id}; {archive_list_wording(settings)}.")
        mutator.move_card(loser.id, archive_list.id, position="top")
        if not mutator.dry_run:
            storage.add_archive_entry(db_path, loser.id, now_iso)
        result.recently_archived.append({"card_id": loser.id, "name": loser.name,
                                         "url": loser.url, "note": archive_list_wording(settings)})

    if not mutator.dry_run:
        storage.record_action(
            db_path, now_iso, now_iso, g.TIER1, "merge",
            [survivor.id] + loser_ids,
            {"survivor_id": survivor.id, "loser_ids": loser_ids, "new_name": verdict.get("new_name")},
            "success",
        )
    result.applied.append({"type": "merge", "survivor_id": survivor.id, "loser_ids": loser_ids})
    return True


def execute_merges(db_path, mutator, board, verdicts, settings, now_utc, now_iso, result):
    """Execute Tier-1 merges up to max_merges_per_run; others become proposals.

    verdicts: adjudication verdicts with relation=='duplicate'. Tier is assigned
    per guardrails; Tier-2 merges are handed to generate_proposals by the caller.
    """
    in_scope = _in_scope_ids(board, settings)
    open_proposed = {c.id for c in board.cards if c.has_label(settings.label_proposed)}
    protected = _protected_card_ids(board, settings)

    tier1, tier2 = [], []
    for v in verdicts:
        if v.get("relation") != "duplicate":
            continue
        action = _merge_action_facts(v, board, settings, in_scope)
        tier = assign_tier(action, settings)
        v["_tier"] = tier
        (tier1 if tier == g.TIER1 else tier2).append(v)

    allowed, deferred = g.apply_cap(tier1, settings.max_merges_per_run)
    for v in deferred:
        result.notes.append(f"Merge deferred by cap: survivor {v.get('survivor_id')}")
    for v in allowed:
        survivor = board.card_by_id(v["survivor_id"])
        if survivor is None:
            continue
        if g.is_never_touch(survivor, now_utc, settings, in_scope, open_proposed, protected):
            result.notes.append(f"Merge skipped (never-touch): {survivor.id}")
            continue
        execute_merge(db_path, mutator, board, v, settings, now_iso, result)
    result.counters["merges"] = sum(1 for a in result.applied if a["type"] == "merge")
    return tier2


def _merge_action_facts(verdict, board, settings, in_scope_ids) -> dict:
    """Derive the forced-tier facts for a merge verdict."""
    survivor = board.card_by_id(verdict.get("survivor_id"))
    loser_ids = [cid for cid in verdict.get("cluster_ids", []) if cid != verdict.get("survivor_id")]
    losers = [board.card_by_id(cid) for cid in loser_ids]
    losers = [l for l in losers if l]
    from phases.candidates import person_labels

    people = set()
    dues = set()
    loser_attach = loser_check = False
    for c in ([survivor] + losers) if survivor else losers:
        if c is None:
            continue
        people.add(frozenset(person_labels(c, settings)))
        if c.due:
            dues.add(c.due)
    for l in losers:
        loser_attach = loser_attach or l.has_attachments
        loser_check = loser_check or l.has_checklist
    distinct_people = {p for p in people if p}
    return {
        "type": "merge",
        "exact_or_near_name_match": bool(verdict.get("exact_or_near_name_match")),
        "cross_person_labels": len(distinct_people) > 1,
        "conflicting_due": len(dues) > 1,
        "survivor_outside_edit_scope": bool(survivor and survivor.list_id not in in_scope_ids),
        "loser_has_attachment": loser_attach,
        "loser_has_checklist": loser_check,
        "llm_tier": verdict.get("llm_tier", 2),
    }


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------

def execute_hygiene(db_path, mutator, board, hygiene_verdicts, flagged_ids, settings,
                    now_utc, now_iso, result, dead_due_ids=None, spine_terms=None):
    """Execute renames (Tier 1) and dead-due handling; return Tier-2 due actions.

    Renames respect max_renames_per_run with heuristic-flagged names prioritized;
    each carries a comment (change explanation + Confidence). Dead-due cards are
    classified against the spine: still-matters cards are never touched and
    surfaced under "Still overdue"; no-longer-matters cards are re-dated ONLY from
    a written source (else cleared), executed per tier1_due_date_clear /
    borderline. The old date is always preserved in a comment.
    """
    in_scope = _in_scope_ids(board, settings)
    open_proposed = {c.id for c in board.cards if c.has_label(settings.label_proposed)}
    protected = _protected_card_ids(board, settings)
    auto_id = board.label_id(settings.label_auto_updated)
    dead_due_ids = dead_due_ids or set()
    spine_terms = spine_terms or []

    def touchable(card_id):
        card = board.card_by_id(card_id)
        return card is not None and not g.is_never_touch(
            card, now_utc, settings, in_scope, open_proposed, protected)

    # -- Renames (Tier 1) ---------------------------------------------------
    renames = [v for v in hygiene_verdicts if v.get("new_name") and touchable(v["card_id"])]
    allowed, deferred = g.apply_cap(
        renames, settings.max_renames_per_run,
        priority=lambda v: v["card_id"] in flagged_ids,
    )
    for v in deferred:
        result.notes.append(f"Rename deferred by cap: {v['card_id']}")
    for v in allowed:
        card = board.card_by_id(v["card_id"])
        old_name = card.name
        _apply_rename(mutator, board, card, v["new_name"], v.get("new_desc"), auto_id, settings)
        mutator.add_comment(card.id, _change_comment(
            f"Renamed from '{old_name}' to '{v['new_name']}'", v))
        if not mutator.dry_run:
            storage.record_action(db_path, now_iso, now_iso, g.TIER1, "rename", [card.id],
                                  {"new_name": v["new_name"], "original_name": old_name}, "success")
        result.applied.append({"type": "rename", "card_id": card.id, "new_name": v["new_name"]})

    # -- Dead-due handling --------------------------------------------------
    # Every dead-due in-scope card must resolve to an escalation OR a fix — never
    # silently dropped. Cards the LLM classifies drive the decision; any dead-due
    # card the LLM did not classify defaults to a "still overdue" escalation
    # (safe: no mutation, but visible).
    tier2_due = []
    handled_due: set[str] = set()
    for v in hygiene_verdicts:
        cid = v["card_id"]
        if cid not in dead_due_ids and not v.get("clear_due") and not v.get("due_status"):
            continue
        card = board.card_by_id(cid)
        if card is None or not card.due:
            continue
        handled_due.add(cid)

        status = v.get("due_status")
        if status == "still_matters":
            # Never touched; surfaced in the report so it stays visible.
            result.still_overdue.append({"card_id": cid, "name": card.name, "url": card.url,
                                         "due": card.due,
                                         "reason": v.get("reason", "Workstream still active.")})
            continue

        # no_longer_matters (or legacy clear_due=True): re-date only from a written
        # source, else clear. Validate the cited source substring is really present.
        new_due = v.get("new_due")
        source = v.get("new_due_source")
        src_texts = [f"{card.name}\n{card.desc}"] + list(spine_terms)
        redate = bool(new_due) and g.value_written_in_sources(source, src_texts)
        action = {"type": "due_redate" if redate else "dead_due_clear",
                  "card_ids": [cid], "anchor_card_id": cid,
                  "confidence": v.get("confidence"), "borderline": v.get("borderline"),
                  "new_due": new_due if redate else None,
                  "reason": v.get("reason", "Due date long past.")}
        tier = assign_tier(action, settings)
        if tier == g.TIER2:
            tier2_due.append(action)
            continue
        if not touchable(cid):
            continue
        if redate:
            mutator.add_comment(cid, _change_comment(
                f"Old due date was {card.due}; set new due {new_due} (from written source)", v))
            mutator.set_due(cid, new_due)
            atype = "due_redate"
        else:
            mutator.add_comment(cid, _change_comment(
                f"Old due date was {card.due}; cleared it (long past, nothing left to do)", v))
            mutator.clear_due(cid)
            atype = "dead_due_clear"
        _add_label(mutator, card, auto_id)
        if not mutator.dry_run:
            storage.record_action(db_path, now_iso, now_iso, g.TIER1, atype, [cid],
                                  {"original_due": card.due, "new_due": new_due if redate else None}, "success")
        result.applied.append({"type": atype, "card_id": cid, "new_due": new_due if redate else None})

    # Safety net: any overdue in-scope card the LLM did not classify is escalated,
    # not silently dropped.
    for cid in dead_due_ids:
        if cid in handled_due:
            continue
        card = board.card_by_id(cid)
        if card is None or not card.due:
            continue
        result.still_overdue.append({"card_id": cid, "name": card.name, "url": card.url,
                                     "due": card.due,
                                     "reason": "Not classified this run — left untouched, review."})

    result.counters["renames"] = sum(1 for a in result.applied if a["type"] == "rename")
    return tier2_due


def _change_comment(summary: str, verdict: dict) -> str:
    """Format an auto-action comment: '<summary>. <reason> Confidence: NN%.'"""
    line = summary.rstrip(".") + "."
    reason = verdict.get("reason")
    if reason and reason not in summary:
        line += f" {reason.rstrip('.')}."
    conf = verdict.get("confidence")
    if conf is not None:
        line += f" Confidence: {int(conf)}%."
    return line


def execute_stale_labels(db_path, mutator, board, stale_pairs, settings, now_utc, now_iso, result):
    """Auto-remove stale time-based labels (Tier 1) or return them as Tier-2 actions.

    stale_pairs: list of (card, label_name). These are detected deterministically,
    so they carry confidence 100 (never borderline). Governed by
    tier1_stale_label_removal.
    """
    in_scope = _in_scope_ids(board, settings)
    open_proposed = {c.id for c in board.cards if c.has_label(settings.label_proposed)}
    protected = _protected_card_ids(board, settings)
    auto_id = board.label_id(settings.label_auto_updated)
    tier2 = []
    for card, label in stale_pairs:
        lid = board.label_id(label)
        action = {"type": "stale_label_removal", "card_ids": [card.id], "label_id": lid,
                  "anchor_card_id": card.id, "confidence": 100,
                  "reason": f"Time-based label '{label}' is stale (card not in the matching "
                            f"list, applied long ago)."}
        if assign_tier(action, settings) == g.TIER2:
            tier2.append(action)
            continue
        if g.is_never_touch(card, now_utc, settings, in_scope, open_proposed, protected) or not lid:
            continue
        remaining = [x for x in card.label_ids if x != lid]
        if auto_id and auto_id not in remaining:
            remaining.append(auto_id)
        mutator.set_labels(card.id, remaining)
        mutator.add_comment(card.id, _change_comment(f"Removed stale label '{label}'", action))
        if not mutator.dry_run:
            storage.record_action(db_path, now_iso, now_iso, g.TIER1, "stale_label_removal",
                                  [card.id], {"label": label}, "success")
        result.applied.append({"type": "stale_label_removal", "card_id": card.id, "label": label})
    return tier2


def _apply_rename(mutator, board, card, new_name, new_desc, auto_id, settings):
    """Rename preserving the original title as the first description line."""
    original_line = f"Original title: {card.name}"
    base = new_desc if new_desc else card.desc
    if original_line not in base:
        desc = f"{original_line}\n\n{base}".rstrip() + "\n"
    else:
        desc = base
    mutator.rename(card.id, new_name)
    mutator.set_description(card.id, desc)
    _add_label(mutator, card, auto_id)


def _add_label(mutator, card, label_id):
    if label_id and label_id not in card.label_ids:
        mutator.set_labels(card.id, card.label_ids + [label_id])


# ---------------------------------------------------------------------------
# Recovery routing
# ---------------------------------------------------------------------------

def execute_recovery(db_path, mutator, board, recovery_verdicts, settings, now_iso, result):
    """Route recovery-batch cards per disposition, honoring caps and the Today cap.

    Cards judged Today-worthy beyond recovery_today_max are demoted to Next Few
    Days (noted in the report). propose-archive is handled as Tier 2 unless
    tier1_recovery_archive is set.
    """
    today_id = _role_list_id(board, settings, "today")
    nfd_id = _role_list_id(board, settings, "next_few_days")
    week_id = _role_list_id(board, settings, "this_week")
    inbox_id = _role_list_id(board, settings, "inbox")
    archive_list = board.list_by_name(settings.archive_list_name)

    today_count = 0
    executed = 0
    tier2_archives = []

    for v in recovery_verdicts:
        if executed >= settings.max_recoveries_per_run:
            result.notes.append(f"Recovery deferred by cap: {v['card_id']}")
            continue
        card = board.card_by_id(v["card_id"])
        if card is None:
            continue
        disp = v.get("disposition")
        origin = card.list_name

        if disp == "archive":
            action = {"type": "recovery_archive", "confidence": v.get("confidence"),
                      "borderline": v.get("borderline")}
            if assign_tier(action, settings) == g.TIER2:
                tier2_archives.append(v)
                continue
            # Tier 1 archive routes through the Agent Archive list (never hard-archive).
            if archive_list:
                mutator.add_comment(card.id, _change_comment(
                    f"Recovered from {origin}; no longer needed, {archive_list_wording(settings)}", v))
                mutator.move_card(card.id, archive_list.id, position="top")
                if not mutator.dry_run:
                    storage.add_archive_entry(db_path, card.id, now_iso)
                    storage.add_recovery(db_path, card.id, origin, "archive", now_iso)
                result.recoveries.append({"card_id": card.id, "disposition": "archive"})
                result.applied.append({"type": "recovery_archive", "card_id": card.id,
                                       "origin": origin})
                result.recently_archived.append({"card_id": card.id, "name": card.name,
                                                 "url": card.url, "note": archive_list_wording(settings)})
                executed += 1
            continue

        if disp == "merge":
            # Recovery merge into an active card (flows through the merge path).
            target = v.get("merge_into")
            verdict = {"survivor_id": target, "cluster_ids": [target, card.id],
                       "new_name": None, "llm_tier": v.get("llm_tier", 1),
                       "exact_or_near_name_match": v.get("exact_or_near_name_match", False)}
            in_scope = _in_scope_ids(board, settings)
            facts = _merge_action_facts(verdict, board, settings, in_scope)
            facts["type"] = "recovery_merge"
            if assign_tier(facts, settings) == g.TIER1 and board.card_by_id(target):
                mutator.add_comment(card.id, f"Recovered from {origin}; merged into active card.")
                execute_merge(db_path, mutator, board, verdict, settings, now_iso, result)
                if not mutator.dry_run:
                    storage.add_recovery(db_path, card.id, origin, "merge", now_iso)
                result.recoveries.append({"card_id": card.id, "disposition": "merge"})
                result.applied.append({"type": "recovery_merge", "card_id": card.id,
                                       "origin": origin, "survivor_id": target})
                executed += 1
            else:
                tier2_archives.append(v)  # cautious: propose instead
            continue

        # Direct routing dispositions.
        dest_id, disp_final = None, disp
        if disp == "today":
            if today_count < settings.recovery_today_max:
                dest_id = today_id
                today_count += 1
            else:
                dest_id = nfd_id
                disp_final = "next_few_days"
                result.demoted_recoveries.append({"card_id": card.id, "name": card.name})
                result.notes.append(f"Recovery {card.id} demoted Today->Next Few Days by cap")
        elif disp == "next_few_days":
            dest_id = nfd_id
        elif disp == "this_week":
            dest_id = week_id
        else:  # inbox / ambiguous default
            dest_id = inbox_id
            disp_final = "inbox"

        if dest_id is None:
            dest_id = inbox_id
            disp_final = "inbox"
        if dest_id is None:
            result.notes.append(f"Recovery {card.id} skipped: no destination list resolved")
            continue

        mutator.add_comment(card.id, f"Recovered from {origin} by grooming agent.")
        mutator.move_card(card.id, dest_id)
        if not mutator.dry_run:
            storage.add_recovery(db_path, card.id, origin, disp_final, now_iso)
        result.recoveries.append({"card_id": card.id, "disposition": disp_final})
        dest_name = board.list_by_id(dest_id).name if board.list_by_id(dest_id) else disp_final
        result.applied.append({"type": "recovery_route", "card_id": card.id,
                               "origin": origin, "dest": dest_name})
        executed += 1

    result.counters["recoveries"] = executed
    return tier2_archives


# ---------------------------------------------------------------------------
# Tier 2 proposals
# ---------------------------------------------------------------------------

def generate_proposals(db_path, mutator, board, tier2_actions, settings, now_iso, result):
    """Create Agent: Proposed cards for Tier-2 actions.

    Consults the rejection ledger before every proposal (never re-propose) and
    stops once max_proposals_open open proposals exist.
    """
    proposed_id = board.label_id(settings.label_proposed)
    open_count = storage.count_open_proposals(db_path)

    for action in tier2_actions:
        if open_count >= settings.max_proposals_open:
            result.notes.append("max_proposals_open reached; not generating more proposals")
            break
        card_ids = action["card_ids"]
        fp = fingerprint(action["type"], card_ids, action.get("new_name"))
        if storage.is_rejected(db_path, fp):
            result.notes.append(f"Proposal suppressed (rejected before): {fp}")
            continue
        anchor = action.get("anchor_card_id", card_ids[0])
        card = board.card_by_id(anchor)
        if card and proposed_id:
            _add_label(mutator, card, proposed_id)
            mutator.add_comment(anchor, _proposal_comment(action))
        if mutator.dry_run:
            pid = 0
        else:
            pid = storage.add_proposal(db_path, now_iso, fp, card_ids, action,
                                       action.get("reason", ""), now_iso)
        result.proposals_opened.append({
            "proposal_id": pid, "fingerprint": fp, "type": action["type"],
            "card_id": anchor,
            "title": card.name if card else anchor,
            "url": card.url if card else "",
            "action_desc": _proposed_action_desc(action, board),
            "reason": action.get("reason", ""),
            "confidence": action.get("confidence"),
        })
        open_count += 1


def _proposed_action_desc(action: dict, board) -> str:
    """Human-readable one-liner for the proposed action (title/url friendly)."""
    atype = action.get("type")
    if atype == "merge":
        survivor = board.card_by_id(action.get("survivor_id"))
        tgt = f"{survivor.name} ({survivor.url})" if survivor else action.get("survivor_id", "")
        return f"merge duplicates into {tgt}"
    if atype == "stale_label_removal":
        return "remove a stale time-based label"
    if atype == "recovery_archive":
        return "move to the Agent Archive list (no longer needed)"
    if atype in ("dead_due_clear", "due_redate"):
        return "clear or re-date a long-overdue due date"
    return atype or "review"


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

def process_approvals(db_path, mutator, board, approvals, settings, now_utc, now_iso, result):
    """Execute approved proposals; record rejections for rejected/label-removed ones."""
    for entry in approvals:
        prop = entry["proposal"]
        decision = entry["decision"]
        if decision == "approve":
            action = prop.get("action", {})
            _execute_approved(db_path, mutator, board, action, settings, now_utc, now_iso, result)
            if not mutator.dry_run:
                storage.set_proposal_status(db_path, prop["proposal_id"], "approved")
            result.applied.append({"type": "approved_" + action.get("type", "action"),
                                   "proposal_id": prop["proposal_id"]})
        elif decision == "reject":
            if not mutator.dry_run:
                storage.set_proposal_status(db_path, prop["proposal_id"], "rejected")
                storage.add_rejection(db_path, prop["fingerprint"], "comment", now_iso)
            result.rejections_recorded.append({"fingerprint": prop["fingerprint"], "source": "comment"})


def _execute_approved(db_path, mutator, board, action, settings, now_utc, now_iso, result):
    atype = action.get("type")
    if atype == "merge":
        verdict = {"survivor_id": action.get("survivor_id"),
                   "cluster_ids": action.get("card_ids", []),
                   "new_name": action.get("new_name"), "reason": action.get("reason"),
                   "confidence": action.get("confidence")}
        execute_merge(db_path, mutator, board, verdict, settings, now_iso, result)
    elif atype == "stale_label_removal":
        card = board.card_by_id(action["card_ids"][0])
        label_id = action.get("label_id")
        if card and label_id:
            remaining = [lid for lid in card.label_ids if lid != label_id]
            mutator.set_labels(card.id, remaining)
    elif atype in ("dead_due_clear", "due_redate"):
        card = board.card_by_id(action["card_ids"][0])
        if card:
            new_due = action.get("new_due")
            if atype == "due_redate" and new_due:
                mutator.add_comment(card.id, f"Old due date was {card.due}; set new due {new_due}.")
                mutator.set_due(card.id, new_due)
            else:
                mutator.add_comment(card.id, f"Old due date was {card.due}; cleared it.")
                mutator.clear_due(card.id)
    elif atype == "recovery_archive":
        archive_list = board.list_by_name(settings.archive_list_name)
        card = board.card_by_id(action["card_ids"][0])
        if card and archive_list:
            mutator.add_comment(card.id, f"Approved; {archive_list_wording(settings)}.")
            mutator.move_card(card.id, archive_list.id, position="top")
            if not mutator.dry_run:
                storage.add_archive_entry(db_path, card.id, now_iso)
            result.recently_archived.append({"card_id": card.id, "name": card.name,
                                             "url": card.url, "note": archive_list_wording(settings)})


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

REPORT_CARD_PREFIX = "Grooming Report"
REMINDER_CARD_TITLE = "Review agent spine: update Active Workstreams and People"


def _proposal_comment(action: dict) -> str:
    """Proposal comment: reason (+ Confidence). Approve with 'yes'/'approve'."""
    line = action.get("reason", "Agent proposal — review.").rstrip(".") + "."
    conf = action.get("confidence")
    if conf is not None:
        line += f" Confidence: {int(conf)}%."
    line += " Reply 'yes'/'approve' to apply, 'no' or remove the label to reject."
    return line


def _report_card_id(board, settings) -> str | None:
    report_list = board.list_by_name(settings.report_list)
    if not report_list:
        return None
    for c in board.cards_in_list(report_list.id):
        if c.name.strip().lower().startswith(REPORT_CARD_PREFIX.lower()):
            return c.id
    return None


def _protected_card_ids(board, settings) -> set[str]:
    """Cards exempt from all hygiene/dedup: the Grooming Report + spine reminder."""
    protected: set[str] = set()
    rid = _report_card_id(board, settings)
    if rid:
        protected.add(rid)
    for c in board.cards:
        if not c.closed and c.name.strip() == REMINDER_CARD_TITLE:
            protected.add(c.id)
    return protected


def maybe_create_spine_reminder(db_path, mutator, board, settings, spine, now_utc, result):
    """Create the weekly spine-review reminder card at the top of Today.

    On the first run on/after spine_review_day each week (tracked in SQLite),
    create the reminder unless a card with that title is already open. "off"
    disables it entirely. Exempt from hygiene/dedup via _protected_card_ids.
    """
    if settings.spine_review_day == "off":
        return
    _WD = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    local = g.local_now(now_utc, settings.tz_standard_offset, settings.tz_daylight_offset)
    review_idx = _WD.index(settings.spine_review_day)
    if local.weekday() < review_idx:
        return  # before the review day this week
    iso = local.isocalendar()
    week_key = f"{iso[0]}-{iso[1]:02d}"
    if storage.kv_get(db_path, "last_reminder_week") == week_key:
        return  # already handled this week
    # In dry-run we do NOT consume the weekly slot, so a later live run still
    # creates the card for real.
    if not mutator.dry_run:
        storage.kv_set(db_path, "last_reminder_week", week_key)

    today_list = board.list_by_name(settings.report_list)
    if today_list is None:
        return
    already_open = any(
        c.name.strip() == REMINDER_CARD_TITLE
        for c in board.cards_in_list(today_list.id)
    )
    if already_open:
        result.notes.append("Spine-review reminder already open; not recreating")
        return
    url = getattr(spine, "page_url", "") if spine else ""
    desc = f"Open the Notion spine and update Active Workstreams and People.\nSpine: {url}".rstrip()
    created = mutator.create_card(today_list.id, REMINDER_CARD_TITLE, desc)
    cid = created.get("id", "") if isinstance(created, dict) else ""
    if isinstance(cid, str) and cid and cid not in ("dry-run", "dry-run-archive-list"):
        from models import Card
        board.cards.append(Card(id=cid, name=REMINDER_CARD_TITLE, desc=desc,
                                list_id=today_list.id, list_name=today_list.name))
    result.reminder_created = True
    logger.info("Created weekly spine-review reminder card")
