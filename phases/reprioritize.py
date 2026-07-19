"""
Reprioritization pass (design §5.4 / spine "Problem 5").

Runs AFTER merges, archives, and hygiene each morning, so the list-size targets
apply to the already-cleaned board. Two directions:

  - Mark More Time-sensitive (upward): scan Inbox / Triage, This Week, and Next
    Few Days for cards that should move up — a "P0. High" / "P1" label, a due date
    inside the target list's window, or a High-priority + High-time-sensitivity
    workstream match on the spine.
  - Mark Less Time-sensitive (downward): only when Today exceeds today_list_target
    or Next Few Days exceeds next_few_days_target, move the weakest cards down.

The AUTOMATIC GATE is enforced here in code, never left to the LLM:
  (a) every signal the LLM claims must be verified against real card/spine data
      (a claimed-but-unverified signal invalidates the automatic path), and at
      least one signal must be verified (for downward moves the weakness criteria
      collectively count as one signal); AND
  (b) confidence >= time_reprioritization_confidence.
Anything failing either test becomes an Agent: Proposed proposal. reprioritization
mode "proposed" forces every move to a proposal.

HARD EXEMPTIONS (code-enforced): a card the user placed or edited within
demotion_exempt_hours (48h) is never moved down — automatically or by proposal; a
card carrying "1. Today (must do)" is never moved down unless the action
explicitly downgrades that label with a stated reason.

Scope for this pass is edit scope PLUS This Week (both source and destination);
the per-run cap is max_reprioritization_moves_per_run; the rejection ledger
applies to every reprioritization action.
"""

from __future__ import annotations

import logging

import guardrails as g
import storage
from guardrails import fingerprint, is_time_based_label, normalize_tokens
from phases import execute as ex
from vocab import ACTION_MARK_LESS, ACTION_MARK_MORE

logger = logging.getLogger(__name__)

# Time-sensitivity rank of a list role (higher == more time-sensitive).
_RANK = {"today": 3, "next_few_days": 2, "this_week": 1, "inbox": 0}


# ---------------------------------------------------------------------------
# List-role helpers
# ---------------------------------------------------------------------------

def list_role(list_name: str, settings=None) -> str | None:
    """Map a list name to a role: today / next_few_days / this_week / inbox."""
    low = (list_name or "").lower()
    if "inbox" in low or "triage" in low:
        return "inbox"
    if "next few" in low:
        return "next_few_days"
    if "this week" in low:
        return "this_week"
    if "today" in low:
        return "today"
    return None


def _repri_scope_ids(board, settings) -> dict[str, str]:
    """{role: list_id} for the reprioritization scope (edit scope + This Week)."""
    out: dict[str, str] = {}
    for role in ("today", "next_few_days", "this_week", "inbox"):
        lid = ex._role_list_id(board, settings, role)
        if lid:
            out[role] = lid
    return out


# ---------------------------------------------------------------------------
# Effective board state after the earlier phases' mutations
# ---------------------------------------------------------------------------

def effective_list_of(card, mutator) -> str | None:
    """The card's list id after applying this run's moves/archives from the log.

    Merges/archives/recoveries already ran; the live board dataclass isn't
    mutated, so we replay the mutator log to know where each card now sits.
    Returns None for a card archived (closed) this run.
    """
    lst = card.list_id
    for e in mutator.log:
        if e.get("card_id") != card.id:
            continue
        op = e.get("op")
        if op == "move_card":
            lst = e.get("target_list_id")
        elif op == "archive_card":
            lst = None
    return lst


def _moved_this_run(card_id: str, mutator) -> bool:
    return any(e.get("card_id") == card_id and e.get("op") in ("move_card", "archive_card")
               for e in mutator.log)


# ---------------------------------------------------------------------------
# Signal verification (deterministic, against real card/spine data)
# ---------------------------------------------------------------------------

def priority_label_role(card, settings) -> str | None:
    """Destination role implied by a P0/P1 label on the card (or None)."""
    for name in card.label_names:
        if name in settings.priority_labels:
            return settings.priority_labels[name]
    return None


def _days_until_due(card, now_utc, settings) -> float | None:
    d = g.days_since(card.due, now_utc, settings.tz_standard_offset, settings.tz_daylight_offset)
    return None if d is None else -d  # days_since is positive when overdue


def due_in_window(card, settings, now_utc, target_role: str) -> bool:
    """True if the card's due date falls within the target list's window.

    Overdue dates (negative days-until) satisfy every window — an overdue card is
    at least as urgent as the tightest band.
    """
    if not card.due:
        return False
    days_until = _days_until_due(card, now_utc, settings)
    if days_until is None:
        return False
    band = settings.reprioritization_due_days.get(target_role)
    if band is None:
        return False
    return days_until <= band


def matched_high_workstream(card, spine, settings):
    """Return an Active, High-priority, High-time-sensitivity workstream the card
    matches (shared significant name token), or None."""
    if spine is None:
        return None
    ctoks = normalize_tokens(card.name)
    for w in spine.workstreams:
        if w.status.lower() != "active":
            continue
        if str(getattr(w, "priority", "")).lower() != "high":
            continue
        if not getattr(w, "time_sensitive", False):
            continue
        if normalize_tokens(w.name) & ctoks:
            return w
    return None


def matched_workstream_any(card, spine):
    """Any workstream the card matches by shared name token (for conflict notes)."""
    if spine is None:
        return None
    ctoks = normalize_tokens(card.name)
    for w in spine.workstreams:
        if normalize_tokens(w.name) & ctoks:
            return w
    return None


def is_weak(card, spine, settings, now_utc) -> bool:
    """Collective demotion-weakness signal: no must-do time label, no near due
    date, and no High-priority workstream match. Counts as ONE verified signal."""
    if any(is_time_based_label(n) for n in card.label_names):
        return False
    days_until = _days_until_due(card, now_utc, settings)
    if days_until is not None and days_until <= settings.reprioritization_due_days.get("this_week", 7):
        return False
    if matched_high_workstream(card, spine, settings) is not None:
        return False
    return True


def verify_signal(sig: str, card, spine, settings, now_utc, target_role: str) -> bool:
    """Verify one claimed signal against real card/spine data."""
    s = (sig or "").lower()
    if s in ("priority_label", "p0_label", "p1_label", "p0", "p1"):
        return priority_label_role(card, settings) is not None
    if s in ("due_in_window", "due", "due_date"):
        return due_in_window(card, settings, now_utc, target_role)
    if s in ("workstream_high", "workstream", "workstream_match"):
        return matched_high_workstream(card, spine, settings) is not None
    if s in ("weak", "weakness"):
        return is_weak(card, spine, settings, now_utc)
    return False


# ---------------------------------------------------------------------------
# Today-must-do downgrade detection
# ---------------------------------------------------------------------------

def _is_today_mustdo(label_name: str) -> bool:
    return is_time_based_label(label_name) and "today" in label_name.lower()


def _downgrades_today_label(verdict: dict) -> bool:
    lc = verdict.get("label_change") or {}
    return lc.get("action") == "downgrade" and _is_today_mustdo(lc.get("from", ""))


def _apply_label_change_ids(label_ids: list[str], lc: dict, board) -> list[str]:
    """Apply an add/upgrade/downgrade label change, returning the new id list."""
    if not lc:
        return label_ids
    action = lc.get("action")
    ids = list(label_ids)
    time_ids = {l["id"] for l in board.labels if is_time_based_label(l.get("name", ""))}
    if action in ("add", "upgrade"):
        if action == "upgrade":
            ids = [x for x in ids if x not in time_ids]
        lid = board.label_id(lc.get("label"))
        if lid and lid not in ids:
            ids.append(lid)
    elif action == "downgrade":
        fid = board.label_id(lc.get("from"))
        tid = board.label_id(lc.get("to"))
        ids = [x for x in ids if x != fid]
        if tid and tid not in ids:
            ids.append(tid)
    return ids


# ---------------------------------------------------------------------------
# Deterministic candidate pre-ranking (Python; the LLM never bulk-generates)
# ---------------------------------------------------------------------------

def _verified_up_signals(card, spine, settings, now_utc) -> list[str]:
    """Code-verified promotion signals present on a card (empty if none)."""
    sigs = []
    if priority_label_role(card, settings) is not None:
        sigs.append("priority_label")
    if any(due_in_window(card, settings, now_utc, r) for r in ("today", "next_few_days", "this_week")):
        sigs.append("due_in_window")
    if matched_high_workstream(card, spine, settings) is not None:
        sigs.append("workstream_high")
    return sigs


def _suggested_up_target(board, card, spine, settings, now_utc) -> str:
    """Destination list name a promotion should aim for, from the strongest signal."""
    role = priority_label_role(card, settings)
    if role is None:
        for r in ("today", "next_few_days", "this_week"):
            if due_in_window(card, settings, now_utc, r):
                role = r
                break
    if role is None and matched_high_workstream(card, spine, settings) is not None:
        role = "today"
    lid = _repri_scope_ids(board, settings).get(role or "today")
    lst = board.list_by_id(lid) if lid else None
    return lst.name if lst else "Today"


def _weakness(card, spine, settings, now_utc) -> tuple[float, dict]:
    """Demotion-weakness score (higher == weaker) and its components.

    Components mirror the design's weakness criteria: no "must do" time label, no
    near due date, no High-priority workstream match, plus days since last activity
    as a graded weight/tiebreaker.
    """
    no_mustdo = not any(is_time_based_label(n) for n in card.label_names)
    days_until = _days_until_due(card, now_utc, settings)
    no_near_due = not (card.due and days_until is not None
                       and days_until <= settings.reprioritization_due_days.get("this_week", 7))
    no_high_ws = matched_high_workstream(card, spine, settings) is None
    days_inactive = g.days_since(card.last_activity, now_utc,
                                 settings.tz_standard_offset, settings.tz_daylight_offset) or 0.0
    score = (1.0 if no_mustdo else 0.0) + (1.0 if no_near_due else 0.0) + (1.0 if no_high_ws else 0.0)
    score += min(max(days_inactive, 0.0) / 30.0, 2.0)  # up to +2 for very stale cards
    return round(score, 3), {
        "no_mustdo_label": no_mustdo, "no_near_due": no_near_due,
        "no_high_workstream": no_high_ws, "days_inactive": round(max(days_inactive, 0.0), 1),
    }


def build_candidates(board, mutator, spine, settings, now_utc) -> dict:
    """Pre-rank reprioritization candidates in Python (no LLM).

    Promotions are pre-filtered to cards carrying at least one code-verified signal.
    Demotions are limited, per over-target list, to the WEAKEST N cards where
    N = min(2 * max_reprioritization_moves_per_run, overflow). Each candidate
    carries its pre-computed facts so the LLM only validates per card.
    """
    roles = _repri_scope_ids(board, settings)
    today_id, nfd_id = roles.get("today"), roles.get("next_few_days")
    eff = {c.id: effective_list_of(c, mutator) for c in board.cards if not c.closed}
    today_count = sum(1 for l in eff.values() if l == today_id)
    nfd_count = sum(1 for l in eff.values() if l == nfd_id)
    overflow_today = max(0, today_count - settings.today_list_target)
    overflow_nfd = max(0, nfd_count - settings.next_few_days_target)

    promote, demote_by_role = [], {"today": [], "next_few_days": []}
    for c in board.cards:
        if c.closed:
            continue
        cur = eff.get(c.id)
        role = next((r for r, lid in roles.items() if lid == cur), None)
        if role is None or _moved_this_run(c.id, mutator):
            continue
        if role in ("inbox", "this_week", "next_few_days"):
            sigs = _verified_up_signals(c, spine, settings, now_utc)
            if sigs:
                promote.append({"id": c.id, "name": c.name, "list": c.list_name,
                                "verified_signals": sigs,
                                "suggested_target": _suggested_up_target(board, c, spine, settings, now_utc)})
        if (role == "today" and overflow_today) or (role == "next_few_days" and overflow_nfd):
            score, comp = _weakness(c, spine, settings, now_utc)
            demote_by_role[role].append({"id": c.id, "name": c.name, "list": c.list_name,
                                         "role": role, "weakness_score": score, "weakness": comp,
                                         "has_today_mustdo": any(_is_today_mustdo(n) for n in c.label_names)})

    cap = settings.max_reprioritization_moves_per_run
    demote = []
    for role, overflow in (("today", overflow_today), ("next_few_days", overflow_nfd)):
        ranked = sorted(demote_by_role[role], key=lambda d: d["weakness_score"], reverse=True)
        demote.extend(ranked[: min(2 * cap, overflow)])

    logger.info(
        "Reprioritization candidates: %d promote, %d demote shortlisted "
        "(Today %d/%d overflow=%d, NFD %d/%d overflow=%d)",
        len(promote), len(demote), today_count, settings.today_list_target, overflow_today,
        nfd_count, settings.next_few_days_target, overflow_nfd,
    )
    return {
        "today_count": today_count, "today_target": settings.today_list_target,
        "nfd_count": nfd_count, "nfd_target": settings.next_few_days_target,
        "overflow_today": overflow_today, "overflow_nfd": overflow_nfd,
        "promote": promote, "demote": demote,
    }


def judge_payload(cands: dict) -> dict:
    """Shape the pre-ranked candidates into the per-candidate judge payload."""
    return {
        "today_count": cands["today_count"], "today_target": cands["today_target"],
        "overflow_today": cands["overflow_today"],
        "nfd_count": cands["nfd_count"], "nfd_target": cands["nfd_target"],
        "overflow_nfd": cands["overflow_nfd"],
        "promote_candidates": cands["promote"],
        "demote_candidates": cands["demote"],
    }


# ---------------------------------------------------------------------------
# Execution + gate
# ---------------------------------------------------------------------------

def _conflict_note(card, cur_list, direction, verdict, board, spine) -> str | None:
    """A placement-conflict note when the action contradicts the user's placement.

    Downward moves always contradict placement (the user chose to work the card on
    that list); the LLM may also flag conflicts_placement for an upward case.
    """
    if not (direction == "down" or verdict.get("conflicts_placement")):
        return None
    lst = board.list_by_id(cur_list)
    where = lst.name if lst else "its list"
    note = f"You placed this on {where}"
    if direction == "down":
        ws = matched_workstream_any(card, spine)
        if ws and str(getattr(ws, "priority", "")).lower() != "high":
            note += f"; the spine marks this workstream {ws.priority} priority"
    return note + "."


def run_reprioritization(db_path, mutator, board, verdicts, settings, spine,
                         now_utc, now_iso, result, unverdicted=0):
    """Execute / propose reprioritization MOVE verdicts; return Tier-2 action dicts.

    `verdicts` are move verdicts only (keeps are filtered upstream). `unverdicted`
    is the count of shortlisted candidates the LLM returned no verdict for — surfaced
    in the report's Health stats so a silent zero is visible. Populates
    result.reprioritizations (executed moves, with verified signals) and
    result.today_plan (counts vs targets + overflow for the report). Tier-2 actions
    are returned for the caller to hand to generate_proposals with everything else.
    """
    roles = _repri_scope_ids(board, settings)
    scope_ids = set(roles.values())
    dest_by_role = roles
    role_by_id = {lid: r for r, lid in roles.items()}
    auto_id = board.label_id(settings.label_auto_updated)
    open_proposed = {c.id for c in board.cards if c.has_label(settings.label_proposed)}
    protected = ex._protected_card_ids(board, settings)

    eff = {c.id: effective_list_of(c, mutator) for c in board.cards if not c.closed}
    today_id, nfd_id = roles.get("today"), roles.get("next_few_days")
    today_start = sum(1 for l in eff.values() if l == today_id)
    nfd_start = sum(1 for l in eff.values() if l == nfd_id)
    today_live, nfd_live = today_start, nfd_start

    executed = 0
    tier2: list[dict] = []

    for v in verdicts:
        cid = v.get("card_id")
        card = board.card_by_id(cid)
        if card is None:
            continue
        cur_list = eff.get(cid)
        if cur_list not in scope_ids or _moved_this_run(cid, mutator):
            continue
        if g.is_never_touch(card, now_utc, settings, scope_ids, open_proposed, protected):
            continue

        direction = v.get("direction")
        origin_role = role_by_id.get(cur_list)
        target_role = list_role(v.get("target_list", ""), settings)
        if target_role is None or origin_role is None:
            result.notes.append(f"Reprioritization skipped (unresolved list): {cid}")
            continue
        dest_id = dest_by_role.get(target_role)
        if dest_id is None:
            continue

        # Direction must agree with the time-sensitivity ranking.
        if direction == "up" and not _RANK[target_role] > _RANK[origin_role]:
            result.notes.append(f"Reprioritization skipped (not an upward move): {cid}")
            continue
        if direction == "down" and not _RANK[target_role] < _RANK[origin_role]:
            result.notes.append(f"Reprioritization skipped (not a downward move): {cid}")
            continue

        # Downward moves only from an over-target list (live counts).
        if direction == "down":
            if origin_role == "today" and today_live <= settings.today_list_target:
                result.notes.append(f"Demotion skipped (Today at/under target): {cid}")
                continue
            if origin_role == "next_few_days" and nfd_live <= settings.next_few_days_target:
                result.notes.append(f"Demotion skipped (Next Few Days at/under target): {cid}")
                continue
            if origin_role not in ("today", "next_few_days"):
                continue

            # HARD EXEMPTION 1: recently placed/edited (never demote, even to propose).
            hrs = g.hours_since(card.last_activity, now_utc,
                                settings.tz_standard_offset, settings.tz_daylight_offset)
            if hrs is not None and 0 <= hrs < settings.demotion_exempt_hours:
                result.notes.append(f"Demotion exempt (edited within {settings.demotion_exempt_hours}h): {cid}")
                continue
            # HARD EXEMPTION 2: "1. Today (must do)" unless the action downgrades it.
            if any(_is_today_mustdo(n) for n in card.label_names) and not _downgrades_today_label(v):
                result.notes.append(f"Demotion exempt (Today must-do, no downgrade): {cid}")
                continue

        # Rejection ledger applies to every reprioritization action.
        atype = "reprioritize_up" if direction == "up" else "reprioritize_down"
        fp = fingerprint(atype, [cid])
        if storage.is_rejected(db_path, fp):
            result.notes.append(f"Reprioritization suppressed (rejected before): {cid}")
            continue

        # --- AUTOMATIC GATE ------------------------------------------------
        claimed = list(v.get("signals", []) or [])
        verified = [s for s in claimed if verify_signal(s, card, spine, settings, now_utc, target_role)]
        all_verified = len(claimed) > 0 and len(verified) == len(claimed)
        try:
            conf = int(v.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            conf = 0
        auto = (settings.reprioritization_mode and all_verified
                and conf >= settings.time_reprioritization_confidence)
        if auto and executed >= settings.max_reprioritization_moves_per_run:
            auto = False  # cap binds → downgrade to a proposal
            result.notes.append(f"Reprioritization over cap; proposing instead: {cid}")

        dest_name = board.list_by_id(dest_id).name if board.list_by_id(dest_id) else target_role
        conflict = _conflict_note(card, cur_list, direction, v, board, spine)
        reason = v.get("reason") or ("Weakest card on an over-target list."
                                     if direction == "down" else "Signals indicate higher time-sensitivity.")

        if not auto:
            tier2.append({
                "type": atype, "card_ids": [cid], "anchor_card_id": cid,
                "confidence": conf, "reason": reason,
                "dest_name": dest_name, "target_list": dest_name,
                "conflict_note": conflict,
            })
            continue

        # --- Execute (Tier 1) ----------------------------------------------
        action_phrase = ACTION_MARK_MORE if direction == "up" else ACTION_MARK_LESS
        parts = []
        if conflict:
            parts.append(conflict)
        parts.append(f"{action_phrase}: move to {dest_name}.")
        parts.append(f"Reason: {reason.rstrip('.')}.")
        parts.append(f"Confidence: {conf}%.")
        if conflict:
            parts.append("Reject if your placement stands.")
        mutator.add_comment(cid, " ".join(parts))
        mutator.move_card(cid, dest_id, position="top")

        label_ids = _apply_label_change_ids(list(card.label_ids), v.get("label_change") or {}, board)
        if auto_id and auto_id not in label_ids:
            label_ids.append(auto_id)
        if label_ids != card.label_ids:
            mutator.set_labels(cid, label_ids)

        if not mutator.dry_run:
            storage.record_action(db_path, now_iso, now_iso, g.TIER1, atype, [cid],
                                  {"direction": direction, "dest": dest_name,
                                   "verified_signals": verified}, "success")
        result.applied.append({"type": atype, "card_id": cid, "dest": dest_name,
                               "direction": direction})
        result.reprioritizations.append({
            "card_id": cid, "name": card.name, "direction": direction, "dest": dest_name,
            "verified_signals": verified, "confidence": conf, "reason": reason})
        executed += 1
        eff[cid] = dest_id
        if direction == "down":
            if origin_role == "today":
                today_live -= 1
            elif origin_role == "next_few_days":
                nfd_live -= 1
        elif direction == "up" and target_role == "today":
            today_live += 1

    overflow = (max(0, today_start - settings.today_list_target)
                + max(0, nfd_start - settings.next_few_days_target))
    result.today_plan = {
        "today_count": today_start, "today_target": settings.today_list_target,
        "nfd_count": nfd_start, "nfd_target": settings.next_few_days_target,
        "moved": executed, "proposed": len(tier2),
        "overflow": overflow, "unverdicted": unverdicted,
    }
    result.counters["reprioritizations"] = executed
    if unverdicted:
        logger.warning("Reprioritization: %d candidate(s) left unverdicted by the LLM", unverdicted)
    logger.info("Reprioritization: %d moved, %d proposed against %d overflow",
                executed, len(tier2), overflow)
    return tier2
