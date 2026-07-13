"""Sections 5 & 6 (execution) — caps, merge invariant, never-touch, rejection ledger."""

from unittest.mock import MagicMock

import guardrails
import storage
from phases import execute as ex

NOW_ISO = "2026-07-11T13:00:00+00:00"


def _mut(dry=False):
    return ex.BoardMutator(MagicMock(), dry_run=dry)


def _ops(mutator, op):
    return [e for e in mutator.log if e["op"] == op]


def _dup_verdict(a, b, survivor):
    return {"relation": "duplicate", "cluster_ids": [a, b], "survivor_id": survivor,
            "new_name": None, "exact_or_near_name_match": True, "llm_tier": 1}


# ── Merges ─────────────────────────────────────────────────────────────────

def test_merge_moves_loser_to_archive_list_not_trello_archive(board, settings, db_path, now_utc):
    mut = _mut()
    result = ex.ExecutionResult()
    ex.execute_merges(db_path, mut, board, [_dup_verdict("dup_exact_a", "dup_exact_b", "dup_exact_b")],
                      settings, now_utc, NOW_ISO, result)
    archive = board.list_by_name(settings.archive_list_name).id
    moves = _ops(mut, "move_card")
    # Loser moved to the TOP of the Agent Archive list, never Trello-archived directly.
    assert any(m["card_id"] == "dup_exact_a" and m["target_list_id"] == archive
               and m.get("position") == "top" for m in moves)
    assert _ops(mut, "add_comment")
    assert _ops(mut, "archive_card") == []
    # Entry timestamp tracked in SQLite.
    assert storage.archive_entry_ts(db_path, "dup_exact_a") == NOW_ISO


def test_cap_max_merges_per_run_enforced(board, make_settings, db_path, now_utc):
    settings = make_settings(max_merges_per_run=1)
    mut = _mut()
    result = ex.ExecutionResult()
    verdicts = [_dup_verdict("dup_exact_a", "dup_exact_b", "dup_exact_b"),
                _dup_verdict("dup_near_a", "dup_near_b", "dup_near_a")]
    ex.execute_merges(db_path, mut, board, verdicts, settings, now_utc, NOW_ISO, result)
    assert result.counters["merges"] == 1


def test_merge_invariant_violation_blocks_move(board, settings, db_path, now_utc, monkeypatch):
    monkeypatch.setattr(guardrails, "merge_contains_all_sources", lambda *a, **k: False)
    mut = _mut()
    result = ex.ExecutionResult()
    ex.execute_merge(db_path, mut, board, _dup_verdict("dup_exact_a", "dup_exact_b", "dup_exact_b"),
                     settings, NOW_ISO, result)
    assert _ops(mut, "move_card") == []  # nothing archived when invariant fails


# ── Never-touch / no-touch (renames skipped) ───────────────────────────────

def _rename(card_id, new_name):
    return {"card_id": card_id, "new_name": new_name}


def _run_hygiene(board, settings, db_path, now_utc, verdicts, flagged=None):
    mut = _mut()
    result = ex.ExecutionResult()
    ex.execute_hygiene(db_path, mut, board, verdicts, set(flagged or []), settings, now_utc, NOW_ISO, result)
    return mut, result


def test_no_touch_window_blocks_edit(board, make_settings, db_path, now_utc):
    # With a positive no_touch window a recently edited card is skipped.
    settings = make_settings(no_touch_hours=12)
    mut, _ = _run_hygiene(board, settings, db_path, now_utc, [_rename("recent_edit", "New Name")])
    assert all(e["card_id"] != "recent_edit" for e in _ops(mut, "rename"))


def test_no_touch_zero_allows_recent_edit(board, settings, db_path, now_utc):
    # Default no_touch_hours=0 disables the window; the recent-edit card is touchable.
    assert settings.no_touch_hours == 0
    mut, _ = _run_hygiene(board, settings, db_path, now_utc, [_rename("recent_edit", "New Name")])
    assert any(e["card_id"] == "recent_edit" for e in _ops(mut, "rename"))


def test_no_touch_zero_still_protects_open_proposed_card(board, settings, db_path, now_utc):
    # Even with no_touch_hours=0, the open-proposal lock protects proposed_card.
    assert settings.no_touch_hours == 0
    mut, _ = _run_hygiene(board, settings, db_path, now_utc, [_rename("proposed_card", "New")])
    assert _ops(mut, "rename") == []


def test_never_touch_grooming_report_card(board, settings, db_path, now_utc):
    mut, _ = _run_hygiene(board, settings, db_path, now_utc, [_rename("report_card", "Renamed Report")])
    assert _ops(mut, "rename") == []


def test_never_touch_open_proposed_card(board, settings, db_path, now_utc):
    mut, _ = _run_hygiene(board, settings, db_path, now_utc, [_rename("proposed_card", "New")])
    assert _ops(mut, "rename") == []


def test_never_touch_out_of_scope_card(board, settings, db_path, now_utc):
    mut, _ = _run_hygiene(board, settings, db_path, now_utc, [_rename("dup_scratch_src", "New")])
    assert _ops(mut, "rename") == []


# ── Rename caps and priority ───────────────────────────────────────────────

def test_cap_max_renames_per_run_enforced(board, make_settings, db_path, now_utc):
    settings = make_settings(max_renames_per_run=1)
    mut, result = _run_hygiene(board, settings, db_path, now_utc,
                               [_rename("name_pipe", "Call Dana and prep agenda"),
                                _rename("name_double", "Send the deck")],
                               flagged=["name_pipe", "name_double"])
    assert result.counters["renames"] == 1


def test_cap_max_renames_prioritizes_flagged_names(board, make_settings, db_path, now_utc):
    settings = make_settings(max_renames_per_run=1)
    mut, _ = _run_hygiene(board, settings, db_path, now_utc,
                          [_rename("name_clean", "Review RSM comp with Colin now"),
                           _rename("name_pipe", "Call Dana and prep agenda")],
                          flagged=["name_pipe"])
    renamed = [e["card_id"] for e in _ops(mut, "rename")]
    assert renamed == ["name_pipe"]  # flagged wins the single slot


def test_name_llm_nominated_rename_beyond_flagged_allowed(board, settings, db_path, now_utc):
    """A clean (non-flagged) card the LLM still nominates may be renamed (filter not a gate)."""
    mut, _ = _run_hygiene(board, settings, db_path, now_utc,
                          [_rename("name_clean", "Review RSM comp with Colin today")], flagged=[])
    assert any(e["card_id"] == "name_clean" for e in _ops(mut, "rename"))


def test_dead_due_clear_is_tier1(board, settings, db_path, now_utc):
    mut = _mut()
    result = ex.ExecutionResult()
    ex.execute_hygiene(db_path, mut, board, [{"card_id": "dead_due", "clear_due": True}], set(),
                       settings, now_utc, NOW_ISO, result, dead_due_ids={"dead_due"})
    assert any(e["card_id"] == "dead_due" for e in _ops(mut, "clear_due"))
    assert any(e["card_id"] == "dead_due" for e in _ops(mut, "add_comment"))


def test_rename_carries_change_comment_with_confidence(board, settings, db_path, now_utc):
    mut, _ = _run_hygiene(board, settings, db_path, now_utc,
                          [{"card_id": "name_pipe", "new_name": "Call Dana and prep agenda",
                            "confidence": 88, "reason": "Split multi-part title"}],
                          flagged=["name_pipe"])
    comment = [e for e in _ops(mut, "add_comment") if e["card_id"] == "name_pipe"][0]["text"]
    assert "Renamed from" in comment and "Confidence: 88%" in comment


# ── Stale time-based label auto-removal (auto-mode default) ─────────────────

def test_stale_label_auto_removed_tier1(board, settings, db_path, now_utc):
    # No LLM label verdict → default disposition is remove (three-way (c)).
    card = board.card_by_id("stale_label")
    mut = _mut()
    result = ex.ExecutionResult()
    tier2 = ex.execute_label_dispositions(db_path, mut, board, [(card, "1. Today (must do)")],
                                          [], settings, now_utc, NOW_ISO, result)
    assert tier2 == []
    stripped = [e for e in _ops(mut, "set_labels") if e["card_id"] == "stale_label"]
    assert stripped and "LB_t" not in stripped[0]["label_ids"]
    assert any("Removed stale label" in e["text"] for e in _ops(mut, "add_comment"))


def test_stale_label_proposed_when_flag_off(board, make_settings, db_path, now_utc):
    settings = make_settings(tier1_stale_label_removal=False)
    card = board.card_by_id("stale_label")
    mut = _mut()
    result = ex.ExecutionResult()
    tier2 = ex.execute_label_dispositions(db_path, mut, board, [(card, "1. Today (must do)")],
                                          [], settings, now_utc, NOW_ISO, result)
    assert any(a["type"] == "stale_label_removal" for a in tier2)
    assert _ops(mut, "set_labels") == []


def test_stale_label_swapped_when_active_time_sensitive(board, settings, db_path, now_utc):
    # LLM disposition "swap" → move to the target tier, not remove.
    card = board.card_by_id("stale_label")
    mut = _mut()
    result = ex.ExecutionResult()
    verdicts = [{"card_id": "stale_label", "disposition": "swap",
                 "target_label": "2. Next Few Days (must do)", "confidence": 85,
                 "reason": "Workstream active & time-sensitive"}]
    tier2 = ex.execute_label_dispositions(db_path, mut, board, [(card, "1. Today (must do)")],
                                          verdicts, settings, now_utc, NOW_ISO, result)
    stripped = [e for e in _ops(mut, "set_labels") if e["card_id"] == "stale_label"]
    assert stripped and "LB_t" not in stripped[0]["label_ids"] and "LB_n" in stripped[0]["label_ids"]
    assert any(a["type"] == "label_swap" for a in result.applied)
    assert any("Swapped stale label" in e["text"] for e in _ops(mut, "add_comment"))


def test_stale_label_archived_card_skipped(board, settings, db_path, now_utc):
    # A stale-label card already claimed by archive gets no label change (precedence).
    card = board.card_by_id("stale_label")
    mut = _mut()
    result = ex.ExecutionResult()
    tier2 = ex.execute_label_dispositions(db_path, mut, board, [(card, "1. Today (must do)")],
                                          [], settings, now_utc, NOW_ISO, result,
                                          skip_ids={"stale_label"})
    assert _ops(mut, "set_labels") == [] and tier2 == []


# ── Proposals: rejection ledger + cap ──────────────────────────────────────

def test_rejection_ledger_consulted_before_proposal(board, settings, db_path):
    storage.add_rejection(db_path, "merge|dup_person_a,dup_person_b", "edit", NOW_ISO)
    mut = _mut()
    result = ex.ExecutionResult()
    action = {"type": "merge", "card_ids": ["dup_person_a", "dup_person_b"],
              "anchor_card_id": "dup_person_a", "reason": "dup"}
    ex.generate_proposals(db_path, mut, board, [action], settings, NOW_ISO, result)
    assert storage.count_open_proposals(db_path) == 0
    assert result.proposals_opened == []


def test_cap_max_proposals_open_stops_generation(board, make_settings, db_path):
    settings = make_settings(max_proposals_open=0)
    mut = _mut()
    result = ex.ExecutionResult()
    action = {"type": "merge", "card_ids": ["dup_person_a", "dup_person_b"],
              "anchor_card_id": "dup_person_a", "reason": "dup"}
    ex.generate_proposals(db_path, mut, board, [action], settings, NOW_ISO, result)
    assert storage.count_open_proposals(db_path) == 0


def test_proposal_created_and_labeled_when_allowed(board, settings, db_path):
    mut = _mut()
    result = ex.ExecutionResult()
    action = {"type": "merge", "card_ids": ["dup_person_a", "dup_person_b"],
              "anchor_card_id": "dup_person_a", "reason": "Owners conflict — recommend merge"}
    ex.generate_proposals(db_path, mut, board, [action], settings, NOW_ISO, result)
    assert storage.count_open_proposals(db_path) == 1
    assert _ops(mut, "set_labels")  # proposed label added to anchor
