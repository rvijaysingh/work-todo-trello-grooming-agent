"""
Phase 4 — Validated execution.

Python enforces every guardrail (design.md §6) AFTER LLM judgment and BEFORE any
Trello write. All mutations go through BoardMutator, which performs zero board
writes in dry-run mode (the report is still produced). Tier 1 actions execute;
Tier 2 actions become Agent: Proposed cards; nothing is ever hard-deleted and
nothing reaches Trello's archive without passing through the quarantine list.
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

    def set_labels(self, card_id: str, label_ids: list[str]) -> None:
        self._record("set_labels", card_id=card_id, label_ids=list(label_ids))
        if not self.dry_run:
            self.trello.update_card(card_id, label_ids=list(label_ids))

    def add_comment(self, card_id: str, text: str) -> None:
        self._record("add_comment", card_id=card_id, text=text)
        if not self.dry_run:
            self.trello.add_comment(card_id, text)

    def move_card(self, card_id: str, target_list_id: str, position="top") -> None:
        self._record("move_card", card_id=card_id, target_list_id=target_list_id)
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


@dataclass
class ExecutionResult:
    applied: list = field(default_factory=list)
    proposals_opened: list = field(default_factory=list)
    rejections_recorded: list = field(default_factory=list)
    quarantine_items: list = field(default_factory=list)
    recoveries: list = field(default_factory=list)
    demoted_recoveries: list = field(default_factory=list)
    expired_proposals: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    counters: dict = field(default_factory=dict)


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
        storage.add_rejection(db_path, rej["fingerprint"], rej["source"], now_iso)
        result.rejections_recorded.append(rej)
        if rej.get("remove_label"):
            card = board.card_by_id(rej["card_id"])
            auto_id = board.label_id(settings.label_auto_updated)
            if card and auto_id:
                remaining = [lid for lid in card.label_ids if lid != auto_id]
                mutator.set_labels(card.id, remaining)


def expire_proposals(db_path, board, open_proposals, settings, now_utc, now_iso, result):
    """Expire open proposals older than proposal_timeout_days; fingerprint them."""
    for prop in open_proposals:
        if g.proposal_expired(prop.get("opened_ts"), now_utc, settings):
            storage.set_proposal_status(db_path, prop["proposal_id"], "expired")
            storage.add_rejection(db_path, prop["fingerprint"], "timeout", now_iso)
            result.expired_proposals.append(prop)


# ---------------------------------------------------------------------------
# Quarantine lifecycle
# ---------------------------------------------------------------------------

def expire_labels_and_quarantine(mutator, board, settings, now_utc, result):
    """Strip aged Auto-Updated labels; auto-archive aged quarantined cards.

    Uses each card's last_activity as the age proxy: an untouched agent-labeled
    or quarantined card's last_activity reflects when the agent last touched it.
    """
    quarantine = board.list_by_name(settings.quarantine_list)
    auto_id = board.label_id(settings.label_auto_updated)

    for card in board.cards:
        if card.closed:
            continue
        # Label expiry
        if auto_id and card.has_label(settings.label_auto_updated):
            if g.label_expired(card.last_activity, now_utc, settings):
                remaining = [lid for lid in card.label_ids if lid != auto_id]
                mutator.set_labels(card.id, remaining)
                result.applied.append({"type": "label_expiry", "card_id": card.id})
        # Quarantine lifecycle
        if quarantine and card.list_id == quarantine.id:
            if g.quarantine_expired(card.last_activity, now_utc, settings):
                mutator.archive_card(card.id)
                result.applied.append({"type": "quarantine_archive", "card_id": card.id})
            else:
                days_left = settings.quarantine_days - (
                    g.days_since(card.last_activity, now_utc,
                                 settings.tz_standard_offset, settings.tz_daylight_offset) or 0
                )
                result.quarantine_items.append({"card_id": card.id, "name": card.name,
                                                "days_remaining": round(days_left, 1)})


# ---------------------------------------------------------------------------
# Merges
# ---------------------------------------------------------------------------

def compose_survivor_desc(survivor, losers) -> str:
    """Build the consolidated survivor description (guarantees the merge invariant).

    Contains the survivor's original title + description and, per source card, its
    original name and full description text.
    """
    parts = [f"Original title: {survivor.name}", ""]
    if survivor.desc:
        parts += [survivor.desc, ""]
    for loser in losers:
        parts.append(f"--- Merged from: {loser.name} ({loser.url}) ---")
        if loser.desc:
            parts.append(loser.desc)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def execute_merge(db_path, mutator, board, verdict, settings, now_iso, result) -> bool:
    """Execute one Tier-1 merge. Returns True if applied, False if blocked.

    Enforces the merge string-containment invariant BEFORE moving any loser to
    quarantine. Survivor keeps its own time-based labels; other labels are unioned
    across sources. Losers move to quarantine (never archived directly).
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

    quarantine = board.list_by_name(settings.quarantine_list)
    if quarantine is None:
        result.notes.append("Quarantine list missing; merge aborted")
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

    # Apply survivor mutations.
    if verdict.get("new_name"):
        mutator.rename(survivor.id, verdict["new_name"])
    mutator.set_description(survivor.id, final_desc)
    mutator.set_labels(survivor.id, final_labels)

    # Move losers to quarantine with a link back to the survivor.
    for loser in losers:
        mutator.add_comment(loser.id, f"Merged into {survivor.url or survivor.id} by grooming agent.")
        mutator.move_card(loser.id, quarantine.id)

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
    report_id = _report_card_id(board, settings)

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
        if g.is_never_touch(survivor, now_utc, settings, in_scope, open_proposed, report_id):
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
                    now_utc, now_iso, result):
    """Execute Tier-1 hygiene (renames, dead-due clears). Returns Tier-2 stale-label actions.

    Renames respect max_renames_per_run with heuristic-flagged names prioritized.
    """
    in_scope = _in_scope_ids(board, settings)
    open_proposed = {c.id for c in board.cards if c.has_label(settings.label_proposed)}
    report_id = _report_card_id(board, settings)
    auto_id = board.label_id(settings.label_auto_updated)

    renames = [v for v in hygiene_verdicts if v.get("new_name")]

    def touchable(card_id):
        card = board.card_by_id(card_id)
        return card is not None and not g.is_never_touch(
            card, now_utc, settings, in_scope, open_proposed, report_id)

    renames = [v for v in renames if touchable(v["card_id"])]
    allowed, deferred = g.apply_cap(
        renames, settings.max_renames_per_run,
        priority=lambda v: v["card_id"] in flagged_ids,
    )
    for v in deferred:
        result.notes.append(f"Rename deferred by cap: {v['card_id']}")
    for v in allowed:
        card = board.card_by_id(v["card_id"])
        _apply_rename(mutator, board, card, v["new_name"], v.get("new_desc"), auto_id, settings)
        storage.record_action(db_path, now_iso, now_iso, g.TIER1, "rename", [card.id],
                              {"new_name": v["new_name"], "original_name": card.name}, "success")
        result.applied.append({"type": "rename", "card_id": card.id, "new_name": v["new_name"]})

    # Dead-due clears (Tier 1).
    for v in hygiene_verdicts:
        if not v.get("clear_due"):
            continue
        card = board.card_by_id(v["card_id"])
        if not touchable(card.id) or not card.due:
            continue
        mutator.add_comment(card.id, f"Cleared dead due date (was {card.due}) — grooming agent.")
        mutator.clear_due(card.id)
        _add_label(mutator, card, auto_id)
        storage.record_action(db_path, now_iso, now_iso, g.TIER1, "dead_due_clear", [card.id],
                              {"original_due": card.due}, "success")
        result.applied.append({"type": "dead_due_clear", "card_id": card.id})

    result.counters["renames"] = sum(1 for a in result.applied if a["type"] == "rename")


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
    quarantine = board.list_by_name(settings.quarantine_list)

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
            action = {"type": "recovery_archive"}
            if assign_tier(action, settings) == g.TIER2:
                tier2_archives.append(v)
                continue
            # Tier 1 archive still routes through quarantine (never hard-archive).
            if quarantine:
                mutator.add_comment(card.id, f"Recovered from {origin}; archiving (obsolete).")
                mutator.move_card(card.id, quarantine.id)
                storage.add_recovery(db_path, card.id, origin, "archive", now_iso)
                result.recoveries.append({"card_id": card.id, "disposition": "archive"})
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
                storage.add_recovery(db_path, card.id, origin, "merge", now_iso)
                result.recoveries.append({"card_id": card.id, "disposition": "merge"})
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
        storage.add_recovery(db_path, card.id, origin, disp_final, now_iso)
        result.recoveries.append({"card_id": card.id, "disposition": disp_final})
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
            mutator.add_comment(anchor, action.get("reason", "Agent proposal — review."))
        pid = storage.add_proposal(db_path, now_iso, fp, card_ids, action,
                                   action.get("reason", ""), now_iso)
        result.proposals_opened.append({"proposal_id": pid, "fingerprint": fp, "type": action["type"]})
        open_count += 1


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
            storage.set_proposal_status(db_path, prop["proposal_id"], "approved")
            result.applied.append({"type": "approved_" + action.get("type", "action"),
                                   "proposal_id": prop["proposal_id"]})
        elif decision == "reject":
            storage.set_proposal_status(db_path, prop["proposal_id"], "rejected")
            storage.add_rejection(db_path, prop["fingerprint"], "comment", now_iso)
            result.rejections_recorded.append({"fingerprint": prop["fingerprint"], "source": "comment"})


def _execute_approved(db_path, mutator, board, action, settings, now_utc, now_iso, result):
    atype = action.get("type")
    if atype == "merge":
        verdict = {"survivor_id": action.get("survivor_id"),
                   "cluster_ids": action.get("card_ids", []),
                   "new_name": action.get("new_name")}
        execute_merge(db_path, mutator, board, verdict, settings, now_iso, result)
    elif atype == "stale_label_removal":
        card = board.card_by_id(action["card_ids"][0])
        label_id = action.get("label_id")
        if card and label_id:
            remaining = [lid for lid in card.label_ids if lid != label_id]
            mutator.set_labels(card.id, remaining)
    elif atype == "recovery_archive":
        quarantine = board.list_by_name(settings.quarantine_list)
        card = board.card_by_id(action["card_ids"][0])
        if card and quarantine:
            mutator.move_card(card.id, quarantine.id)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def _report_card_id(board, settings) -> str | None:
    report_list = board.list_by_name(settings.report_list)
    if not report_list:
        return None
    for c in board.cards_in_list(report_list.id):
        if c.name.strip().lower().startswith("grooming report"):
            return c.id
    return None
