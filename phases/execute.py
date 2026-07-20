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
import re
from contextlib import contextmanager
from dataclasses import dataclass, field

import guardrails as g
import storage
from guardrails import assign_tier, fingerprint
from vocab import (
    ACTION_ARCHIVE,
    ACTION_BACKLOG,
    ACTION_FIX_DUE,
    ACTION_MERGE,
    ACTION_RECOVER,
    ACTION_RENAME,
    ACTION_TIME_LABEL,
    action_phrase,
    three_bullet,
)

logger = logging.getLogger(__name__)

# Words that mark an archive rationale as "this card duplicates another". Such
# cards belong to the MERGE pipeline (precedence merge > archive): merge
# consolidates every source card's text into a survivor, verified by string
# containment, before a loser is archived. Archiving a "redundant copy" here
# would drop it WITHOUT that consolidation — silent data loss. The in-scope
# archive pass must never act on a duplicate reason (enforced in code, not left
# to the LLM prompt alone).
_DUP_ARCHIVE_REASON_RE = re.compile(
    r"\b(duplicate|duplicates|duplicated|duplication|redundant|redundancy|"
    r"redundant copy|copy of|copies of|dupe|dupes)\b",
    re.IGNORECASE,
)


def is_duplicate_archive_reason(reason: str | None) -> bool:
    """True if an in-scope archive reason claims the card is a duplicate/redundant
    copy of another card. Those are owned by the merge pass, never archived here."""
    return bool(_DUP_ARCHIVE_REASON_RE.search(reason or ""))


# ---------------------------------------------------------------------------
# Mutation boundary
# ---------------------------------------------------------------------------

class BoardMutator:
    """The single seam for board writes, aware of the run mode.

    - dry_run: performs NO Trello calls and persists nothing.
    - live: every action is real.
    - limited_test: only the first `limited_per_type` actions of each action TYPE
      execute for real (the rest are simulated); proposals and infra writes are
      always real.

    `self.dry_run` is the PER-ACTION simulate flag — set for the duration of an
    `action(atype)` block, otherwise the base flag — so the existing
    `if not mutator.dry_run:` persistence guards read the correct per-action value
    with no change. Every call is appended to `self.log` regardless of mode.
    """

    def __init__(self, trello_client, dry_run: bool | None = None,
                 run_mode: str | None = None, limited_per_type: int = 2):
        self.trello = trello_client
        self.run_mode = run_mode or ("dry_run" if dry_run else "live")
        self.limited_per_type = limited_per_type
        self._budget: dict[str, int] = {}
        self._base_dry = self.run_mode == "dry_run"
        self.dry_run = self._base_dry   # per-action flag; starts at the base
        self.log: list[dict] = []

    def _decide_real(self, atype: str, limited: bool) -> bool:
        if self.run_mode == "dry_run":
            return False
        if self.run_mode == "live" or not limited:
            return True
        used = self._budget.get(atype, 0)   # limited_test: top N per type
        if used < self.limited_per_type:
            self._budget[atype] = used + 1
            return True
        return False

    @contextmanager
    def action(self, atype: str, limited: bool = True):
        """Scope a single action's writes; yields whether it executes for real."""
        real = self._decide_real(atype, limited)
        self.dry_run = not real
        try:
            yield real
        finally:
            self.dry_run = self._base_dry

    def _record(self, op: str, **kwargs) -> None:
        self.log.append({"op": op, **kwargs})
        logger.info("%s %s %s", "[dry-run]" if self.dry_run else "[live]", op, kwargs)

    def rename(self, card_id: str, new_name: str) -> None:
        self._record("rename", card_id=card_id, new_name=new_name)
        if not self.dry_run:
            self.trello.update_card(card_id, name=new_name)

    def set_description(self, card_id: str, desc: str) -> None:
        self._record("set_description", card_id=card_id, desc=desc)
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
    reprioritizations: list = field(default_factory=list)   # executed More/Less moves
    today_plan: dict = field(default_factory=dict)           # counts vs targets (report)
    expired_proposals: list = field(default_factory=list)
    reminder_created: bool = False
    spine_unreadable: bool = False                           # spine read failed → degraded run
    run_mode: str = "dry_run"                                # dry_run | limited_test | live
    limited_per_type: int = 2                                # real actions per type in limited_test
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
                                                 "url": card.url, "note": TRELLO_ARCHIVE_WORDING,
                                                 "reason": "Aged out of the Agent Archive list (60+ days)"})
            else:
                days_in = g.days_since(entered, now_utc,
                                       settings.tz_standard_offset, settings.tz_daylight_offset) or 0
                days_left = settings.archive_list_days - days_in
                if days_left <= 10:
                    result.recently_archived.append(
                        {"card_id": card.id, "name": card.name, "url": card.url,
                         "note": f"{round(days_left, 1)} day(s) until Trello archive",
                         "reason": f"{round(days_left, 1)} day(s) until it moves to Trello's archive"})


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
    reason = verdict.get("reason") or "Same task captured as multiple cards."
    return three_bullet(
        [("duplicate_of", src)], ACTION_MERGE,
        f"→ survivor holds all text; sources {archive_list_wording(settings)}",
        f"Merged in {len(losers)} duplicate(s). {reason}", verdict.get("confidence"))


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
        mutator.add_comment(loser.id, _action_comment(
            ACTION_MERGE, [("survivor", survivor.name)],
            f"Merged into {survivor.url or survivor.id}; {archive_list_wording(settings)}.",
            from_to=f"to '{settings.archive_list_name}'"))
        mutator.move_card(loser.id, archive_list.id, position="top")
        if not mutator.dry_run:
            storage.add_archive_entry(db_path, loser.id, now_iso)
        result.recently_archived.append({"card_id": loser.id, "name": loser.name,
                                         "url": loser.url, "note": archive_list_wording(settings),
                                         "reason": f"Merged into '{survivor.name}'"})

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

    # Highest-confidence first so limited_test executes the strongest N merges real.
    tier1.sort(key=lambda v: v.get("confidence") or 0, reverse=True)
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
        with mutator.action("merge") as real:
            if execute_merge(db_path, mutator, board, v, settings, now_iso, result):
                if result.applied and result.applied[-1].get("type") == "merge":
                    result.applied[-1]["real"] = real
                for entry in result.recently_archived:
                    entry.setdefault("real", real)
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
                    now_utc, now_iso, result, dead_due_ids=None, spine_terms=None, skip_ids=None):
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
    dead_due_ids = set(dead_due_ids or set())
    spine_terms = spine_terms or []
    skip_ids = skip_ids or set()  # cards claimed by a higher-precedence action (merge/archive)

    def touchable(card_id):
        card = board.card_by_id(card_id)
        return card is not None and card_id not in skip_ids and not g.is_never_touch(
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
        with mutator.action("rename") as real:
            _apply_rename(mutator, board, card, v["new_name"], v.get("new_desc"), auto_id, settings)
            mutator.add_comment(card.id, _action_comment(
                ACTION_RENAME, [("name_quality", f"unclear title '{old_name}'")],
                v.get("reason") or "Rewritten per the card naming standard.",
                confidence=v.get("confidence"),
                from_to=f"from '{old_name}' to '{v['new_name']}'"))
            if real:
                storage.record_action(db_path, now_iso, now_iso, g.TIER1, "rename", [card.id],
                                      {"new_name": v["new_name"], "original_name": old_name}, "success")
            result.applied.append({"type": "rename", "card_id": card.id,
                                   "new_name": v["new_name"], "real": real})

    # -- Dead-due handling --------------------------------------------------
    # Every dead-due in-scope card must resolve to an escalation OR a fix — never
    # silently dropped. Cards the LLM classifies drive the decision; any dead-due
    # card the LLM did not classify defaults to a "still overdue" escalation
    # (safe: no mutation, but visible).
    tier2_due = []
    handled_due: set[str] = set()
    for v in hygiene_verdicts:
        cid = v["card_id"]
        # Only entries carrying an explicit due decision act here — a rename
        # verdict for a dead-due card must NOT trigger a clear.
        if not v.get("clear_due") and not v.get("due_status"):
            continue
        if cid in skip_ids:  # merged/archived → no date fix (precedence)
            handled_due.add(cid)
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
        with mutator.action("due") as real:
            if redate:
                mutator.add_comment(cid, _action_comment(
                    ACTION_FIX_DUE,
                    [("due_status", f"dead — old due date was {card.due}"),
                     ("written_date", f"{new_due} (from card/spine text)")],
                    v.get("reason") or "Re-dated from a written source.",
                    confidence=v.get("confidence"),
                    from_to=f"from {card.due} to {new_due}"))
                mutator.set_due(cid, new_due)
                atype = "due_redate"
            else:
                mutator.add_comment(cid, _action_comment(
                    ACTION_FIX_DUE,
                    [("due_status", f"dead — old due date was {card.due}"),
                     ("written_date", "none found")],
                    v.get("reason") or "Cleared a long-past due date (nothing left to do).",
                    confidence=v.get("confidence")))
                mutator.clear_due(cid)
                atype = "dead_due_clear"
            # Date fixes are label-neutral by design: they never add/remove labels.
            if real:
                storage.record_action(db_path, now_iso, now_iso, g.TIER1, atype, [cid],
                                      {"original_due": card.due, "new_due": new_due if redate else None},
                                      "success")
            result.applied.append({"type": atype, "card_id": cid,
                                   "new_due": new_due if redate else None, "real": real})

    # Safety net: any overdue in-scope card the LLM did not classify is escalated,
    # not silently dropped (but a card claimed by merge/archive is not escalated).
    for cid in dead_due_ids:
        if cid in handled_due or cid in skip_ids:
            continue
        card = board.card_by_id(cid)
        if card is None or not card.due:
            continue
        result.still_overdue.append({"card_id": cid, "name": card.name, "url": card.url,
                                     "due": card.due,
                                     "reason": "Not classified this run — left untouched, review."})

    result.counters["renames"] = sum(1 for a in result.applied if a["type"] == "rename")
    return tier2_due


def _action_comment(action_label: str, signals, rationale: str,
                    confidence=None, from_to: str | None = None) -> str:
    """Format an auto-action comment in the canonical 3-bullet layout (vocab.three_bullet).

    signals: ordered [(name, value)] facts that drove the action (bullet 1).
    action_label: a phrase from vocab.py, rendered "[<label>]" (bullet 2).
    from_to: optional from/to clause on bullet 2.
    rationale + confidence: bullet 3.
    """
    return three_bullet(signals, action_label, from_to, rationale, confidence)


def _archive_signals(v: dict) -> list[tuple]:
    """Signal pairs for an archive comment: the verdict's own signals if present,
    else a single assessment fact derived from its reason."""
    s = v.get("signals")
    if s:
        return [tuple(x) if isinstance(x, (list, tuple)) else ("signal", x) for x in s]
    return [("assessment", (v.get("reason") or "no longer needed").rstrip("."))]


def execute_label_dispositions(db_path, mutator, board, stale_pairs, label_verdicts, settings,
                               now_utc, now_iso, result, skip_ids=None):
    """Three-way disposition for stale time-based labels (design §5.1):

      (a) card is no longer needed → archived by the in-scope archive pass (it is
          in skip_ids here, so we do nothing);
      (b) workstream Active AND Time-sensitive → SWAP the label to the matching
          tier (2. Next Few Days / 3. This Week) instead of removing;
      (c) otherwise REMOVE the label.

    The old label is always noted in a comment. Governed by
    tier1_stale_label_removal + auto_min_confidence (borderline → proposal).
    """
    in_scope = _in_scope_ids(board, settings)
    open_proposed = {c.id for c in board.cards if c.has_label(settings.label_proposed)}
    protected = _protected_card_ids(board, settings)
    auto_id = board.label_id(settings.label_auto_updated)
    skip_ids = skip_ids or set()
    verdict_by_id = {v["card_id"]: v for v in (label_verdicts or [])}
    tier2 = []

    for card, label in stale_pairs:
        if card.id in skip_ids:  # merged/archived → precedence, no label change
            continue
        old_lid = board.label_id(label)
        v = verdict_by_id.get(card.id, {})
        disp = v.get("disposition", "remove")
        target = v.get("target_label")
        target_lid = board.label_id(target) if target else None

        if disp == "swap" and target_lid:
            action = {"type": "label_swap", "card_ids": [card.id], "anchor_card_id": card.id,
                      "label_id": old_lid, "target_label": target,
                      "confidence": v.get("confidence"), "borderline": v.get("borderline"),
                      "reason": v.get("reason",
                                      f"Workstream active & time-sensitive; move '{label}' to '{target}'.")}
            if assign_tier(action, settings) == g.TIER2:
                tier2.append(action)
                continue
            if g.is_never_touch(card, now_utc, settings, in_scope, open_proposed, protected):
                continue
            with mutator.action("label") as real:
                new_labels = [x for x in card.label_ids if x != old_lid]
                if target_lid not in new_labels:
                    new_labels.append(target_lid)
                if auto_id and auto_id not in new_labels:
                    new_labels.append(auto_id)
                mutator.set_labels(card.id, new_labels)
                mutator.add_comment(card.id, _action_comment(
                    ACTION_TIME_LABEL,
                    [("time_label", f"'{label}' on a card not in the matching list")],
                    action.get("reason") or "Workstream active & time-sensitive; swap to the matching tier.",
                    confidence=action.get("confidence"),
                    from_to=f"from '{label}' to '{target}'"))
                if real:
                    storage.record_action(db_path, now_iso, now_iso, g.TIER1, "label_swap",
                                          [card.id], {"old": label, "new": target}, "success")
                result.applied.append({"type": "label_swap", "card_id": card.id,
                                       "label": label, "target_label": target, "real": real})
            continue

        # (c) Remove.
        action = {"type": "stale_label_removal", "card_ids": [card.id], "label_id": old_lid,
                  "anchor_card_id": card.id, "confidence": v.get("confidence", 100),
                  "borderline": v.get("borderline"),
                  "reason": v.get("reason",
                                  f"Time-based label '{label}' is stale (card not in the matching "
                                  f"list, applied long ago).")}
        if assign_tier(action, settings) == g.TIER2:
            tier2.append(action)
            continue
        if g.is_never_touch(card, now_utc, settings, in_scope, open_proposed, protected) or not old_lid:
            continue
        with mutator.action("label") as real:
            remaining = [x for x in card.label_ids if x != old_lid]
            if auto_id and auto_id not in remaining:
                remaining.append(auto_id)
            mutator.set_labels(card.id, remaining)
            mutator.add_comment(card.id, _action_comment(
                ACTION_TIME_LABEL, [("time_label", f"'{label}' stale (wrong list, applied long ago)")],
                action.get("reason") or "Stale time label removed.",
                confidence=action.get("confidence"), from_to=f"remove '{label}'"))
            if real:
                storage.record_action(db_path, now_iso, now_iso, g.TIER1, "stale_label_removal",
                                      [card.id], {"label": label}, "success")
            result.applied.append({"type": "stale_label_removal", "card_id": card.id,
                                   "label": label, "real": real})
    return tier2


# ---------------------------------------------------------------------------
# Archive helpers (shared by merge losers, recovery archive, in-scope archive)
# ---------------------------------------------------------------------------

def _archive_card(db_path, mutator, board, card, settings, now_iso, result, comment,
                  reason: str = "") -> bool:
    """Move a card to the TOP of the Agent Archive list with a comment + ledger entry."""
    archive_list = board.list_by_name(settings.archive_list_name)
    if archive_list is None:
        result.notes.append("Agent Archive list missing; archive skipped")
        return False
    mutator.add_comment(card.id, comment)
    mutator.move_card(card.id, archive_list.id, position="top")
    if not mutator.dry_run:
        storage.add_archive_entry(db_path, card.id, now_iso)
    result.recently_archived.append({"card_id": card.id, "name": card.name, "url": card.url,
                                     "note": archive_list_wording(settings),
                                     "reason": reason or "No longer needed"})
    return True


def execute_inscope_archive(db_path, mutator, board, archive_verdicts, settings,
                            now_utc, now_iso, result, skip_ids=None):
    """Archive in-scope cards that meet the 'no longer needed' test (design §4/§5.2).

    Governed by tier1_recovery_archive + auto_min_confidence; capped at
    max_inscope_archives_per_run. Returns (tier2_actions, archived_ids). skip_ids
    are cards already claimed by a merge (precedence merge > archive).
    """
    skip_ids = skip_ids or set()
    in_scope = _in_scope_ids(board, settings)
    open_proposed = {c.id for c in board.cards if c.has_label(settings.label_proposed)}
    protected = _protected_card_ids(board, settings)
    executed = 0
    tier2 = []
    archived_ids: set[str] = set()

    # Highest-confidence first so limited_test executes the strongest N for real.
    archive_verdicts = sorted(archive_verdicts, key=lambda v: v.get("confidence") or 0, reverse=True)
    for v in archive_verdicts:
        cid = v["card_id"]
        if cid in skip_ids:
            continue
        # Precedence merge > archive: a duplicate/redundant card must be merged,
        # never archived as a "redundant copy" (would skip text consolidation).
        if is_duplicate_archive_reason(v.get("reason")):
            result.notes.append(
                f"In-scope archive skipped (duplicate — belongs to the merge pass): {cid}")
            continue
        card = board.card_by_id(cid)
        if card is None or card.list_id not in in_scope:
            continue
        action = {"type": "inscope_archive", "card_ids": [cid], "anchor_card_id": cid,
                  "confidence": v.get("confidence"), "borderline": v.get("borderline"),
                  "reason": v.get("reason", "No longer needed.")}
        if assign_tier(action, settings) == g.TIER2:
            tier2.append(action)
            continue
        if g.is_never_touch(card, now_utc, settings, in_scope, open_proposed, protected):
            continue
        if executed >= settings.max_inscope_archives_per_run:
            result.notes.append(f"In-scope archive deferred by cap: {cid}")
            continue
        with mutator.action("archive") as real:
            ok = _archive_card(db_path, mutator, board, card, settings, now_iso, result,
                               _action_comment(
                                   ACTION_ARCHIVE, _archive_signals(v),
                                   v.get("reason") or "No longer needed.",
                                   confidence=v.get("confidence"),
                                   from_to=f"to '{settings.archive_list_name}'"),
                               reason=v.get("reason") or "No longer needed")
            if not ok:
                continue
            if real:
                storage.record_action(db_path, now_iso, now_iso, g.TIER1, "inscope_archive",
                                      [cid], {"reason": v.get("reason")}, "success")
            result.applied.append({"type": "inscope_archive", "card_id": cid,
                                   "reason": v.get("reason"), "real": real})
            if result.recently_archived:
                result.recently_archived[-1]["real"] = real
            archived_ids.add(cid)
            executed += 1

    result.counters["inscope_archives"] = executed
    return tier2, archived_ids


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
                mutator.add_comment(card.id, _action_comment(
                    ACTION_ARCHIVE, [("origin", f"Scratch list {origin}")] + _archive_signals(v),
                    v.get("reason") or f"Recovered from {origin}; no longer needed.",
                    confidence=v.get("confidence"),
                    from_to=f"to '{settings.archive_list_name}'"))
                mutator.move_card(card.id, archive_list.id, position="top")
                if not mutator.dry_run:
                    storage.add_archive_entry(db_path, card.id, now_iso)
                    storage.add_recovery(db_path, card.id, origin, "archive", now_iso)
                result.recoveries.append({"card_id": card.id, "disposition": "archive"})
                result.applied.append({"type": "recovery_archive", "card_id": card.id,
                                       "origin": origin})
                result.recently_archived.append({"card_id": card.id, "name": card.name,
                                                 "url": card.url, "note": archive_list_wording(settings),
                                                 "reason": f"Recovered from {origin}; no longer needed"})
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
                mutator.add_comment(card.id, _action_comment(
                    ACTION_RECOVER, [("origin", f"Scratch list {origin}")],
                    "Recovered and merged into an active card.", from_to=f"from {origin}"))
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

        with mutator.action("recover") as real:
            mutator.add_comment(card.id, _action_comment(
                ACTION_RECOVER, [("origin", f"Scratch list {origin}")],
                "Pulled a buried card back into circulation.",
                from_to=f"from {origin} to '{disp_final}'"))
            mutator.move_card(card.id, dest_id)
            if real:
                storage.add_recovery(db_path, card.id, origin, disp_final, now_iso)
            result.recoveries.append({"card_id": card.id, "disposition": disp_final})
            dest_name = board.list_by_id(dest_id).name if board.list_by_id(dest_id) else disp_final
            result.applied.append({"type": "recovery_route", "card_id": card.id,
                                   "origin": origin, "dest": dest_name, "real": real})
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
        # Proposals are always real (they create an Agent: Proposed card) except in
        # dry_run — in limited_test they are not subject to the per-type real budget.
        with mutator.action("proposal", limited=False) as real:
            if card and proposed_id:
                _add_label(mutator, card, proposed_id)
                mutator.add_comment(anchor, _proposal_comment(action, board))
            pid = storage.add_proposal(db_path, now_iso, fp, card_ids, action,
                                       action.get("reason", ""), now_iso) if real else 0
        result.proposals_opened.append({
            "proposal_id": pid, "fingerprint": fp, "type": action["type"],
            "card_id": anchor,
            "card_ids": list(card_ids),                 # all cards under this decision
            "survivor_id": action.get("survivor_id"),   # merges: the card kept
            "title": card.name if card else anchor,
            "url": card.url if card else "",
            "action_desc": _proposed_action_desc(action, board),
            "reason": action.get("reason", ""),
            "confidence": action.get("confidence"),
            "dest_name": action.get("dest_name"),   # reprioritization destination (Today plan)
        })
        open_count += 1


def _proposed_action_desc(action: dict, board) -> str:
    """Vocabulary one-liner for a proposed action (leads with the action phrase).

    Used identically in the proposal card comment and the report's "Proposed:"
    line, so both speak the canonical action vocabulary.
    """
    atype = action.get("type")
    if atype == "merge":
        survivor = board.card_by_id(action.get("survivor_id"))
        tgt = f"'{survivor.name}'" if survivor else action.get("survivor_id", "")
        return f"{ACTION_MERGE} into {tgt}"
    if atype == "stale_label_removal":
        return f"{ACTION_TIME_LABEL} (remove the stale must-do label)"
    if atype == "label_swap":
        return f"{ACTION_TIME_LABEL} (swap the stale must-do label to the matching tier)"
    if atype in ("recovery_archive", "inscope_archive"):
        return f"{ACTION_ARCHIVE} (no longer needed)"
    if atype in ("dead_due_clear", "due_redate"):
        return f"{ACTION_FIX_DUE} (clear or re-date the long-overdue date)"
    if atype == "backlog":
        dest = action.get("dest_name")
        if dest:
            return f"{ACTION_BACKLOG} to '{dest}'"
        return f"{ACTION_BACKLOG} (suggest creating 'Backlog - {action.get('topic', '<topic>')}')"
    if atype in ("reprioritize_up", "reprioritize_down"):
        dest = action.get("dest_name") or action.get("target_list") or "a fitting list"
        return f"{action_phrase(atype)}: move to {dest}"
    return action_phrase(atype)


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
    elif atype in ("recovery_archive", "inscope_archive"):
        card = board.card_by_id(action["card_ids"][0])
        if card:
            _archive_card(db_path, mutator, board, card, settings, now_iso, result,
                          f"Approved; {archive_list_wording(settings)}.",
                          reason=action.get("reason") or "Approved for archive")
    elif atype == "label_swap":
        card = board.card_by_id(action["card_ids"][0])
        target_lid = board.label_id(action.get("target_label")) if action.get("target_label") else None
        old_lid = action.get("label_id")
        if card and target_lid:
            new_labels = [x for x in card.label_ids if x != old_lid]
            if target_lid not in new_labels:
                new_labels.append(target_lid)
            mutator.set_labels(card.id, new_labels)
    elif atype in ("reprioritize_up", "reprioritize_down"):
        card = board.card_by_id(action["card_ids"][0])
        dest = board.list_by_name(action.get("dest_name", ""))
        if card and dest:
            mutator.add_comment(card.id, f"Approved; moved to '{dest.name}'.")
            mutator.move_card(card.id, dest.id, position="top")
    elif atype == "backlog":
        card = board.card_by_id(action["card_ids"][0])
        dest = board.list_by_name(action.get("dest_name", "")) if action.get("dest_name") else None
        if card and dest:
            mutator.add_comment(card.id, f"Approved; moved to backlog list '{dest.name}'.")
            mutator.move_card(card.id, dest.id, position="top")
        elif card:
            # suggest-create case: the user must create the list first.
            mutator.add_comment(
                card.id, f"Approved, but no '{settings.backlog_list_prefix} - "
                         f"{action.get('topic', '')}' list exists yet — create it, then re-approve.")


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

REPORT_CARD_PREFIX = "Grooming Report"
REMINDER_CARD_TITLE = "Review agent spine: update Active Workstreams and People"


def _proposal_signals(action: dict, board) -> list[tuple]:
    """(name, value) pairs for a proposal's bullet 1: the action's own signals if
    it carries them, else a single fact derived from its type/reason."""
    s = action.get("signals")
    if s:
        return [tuple(x) if isinstance(x, (list, tuple)) else ("signal", x) for x in s]
    atype = action.get("type")
    if atype == "merge":
        survivor = board.card_by_id(action.get("survivor_id"))
        return [("duplicate_of", survivor.name if survivor else action.get("survivor_id", ""))]
    return [("assessment", (action.get("reason") or "review").rstrip("."))]


def _proposal_comment(action: dict, board) -> str:
    """Proposal comment in the canonical 3-bullet format (same as executed actions).

    Bullet 3 folds in the placement-conflict reject note (when present) and the
    reply instruction, so the comment stays exactly three bullets. Approval parsing
    reads the USER's reply, so it is unaffected by this format.
    """
    label = action_phrase(action.get("type"))
    from_to = action.get("from_to")
    if not from_to and action.get("type") in ("reprioritize_up", "reprioritize_down", "backlog") \
            and action.get("dest_name"):
        from_to = f"to '{action['dest_name']}'"
    conflict = action.get("conflict_note")
    rationale = action.get("reason") or "Agent proposal — review."
    if conflict:
        rationale += " Reject if your placement stands."
    rationale += " Reply 'yes'/'approve' to apply, 'no' or remove the label to reject."
    return three_bullet(_proposal_signals(action, board), label, from_to,
                        rationale, action.get("confidence"), prefix=conflict or "")


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
