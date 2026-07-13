"""Sections 8 & 9 — recovery scope resolution and disposition routing."""

from unittest.mock import MagicMock

import storage
from phases import candidates as cand
from phases import execute as ex
from phases import snapshot_diff as sd

NOW_ISO = "2026-07-11T13:00:00+00:00"


def _mut():
    return ex.BoardMutator(MagicMock(), dry_run=True)


def _ops(mut, op):
    return [e for e in mut.log if e["op"] == op]


# ── Section 8: scope resolution ────────────────────────────────────────────

def test_recovery_includes_scratch_lists(board, settings):
    names = {l.name for l in sd.recovery_source_lists(board, settings)}
    assert {"Scratch 7-1", "Scratch 6-24", "Scratch 6-3"} <= names


def test_recovery_excludes_archive_renamed_list(board, settings):
    names = {l.name for l in sd.recovery_source_lists(board, settings)}
    assert "ARCHIVE Scratch 5-12" not in names


def test_recovery_excludes_non_scratch_april_archive(board, settings):
    names = {l.name for l in sd.recovery_source_lists(board, settings)}
    assert "Archive - April 12" not in names


def test_recovery_new_scratch_list_auto_included(board, settings):
    names = {l.name for l in sd.recovery_source_lists(board, settings)}
    assert "Scratch 7-1" in names


def test_recovery_orders_newest_list_first(board, settings):
    batch = cand.recovery_batch(board, settings, set())
    # First cards come from the newest list (Scratch 7-1).
    assert batch[0].list_name == "Scratch 7-1"


def test_recovery_ledger_prevents_retriage(board, settings):
    batch = cand.recovery_batch(board, settings, {"rec_s71_1"})
    assert all(c.id != "rec_s71_1" for c in batch)


# ── Section 9: disposition routing ─────────────────────────────────────────

def _run_recovery(board, settings, db_path, verdicts):
    mut = _mut()
    result = ex.ExecutionResult()
    tier2 = ex.execute_recovery(db_path, mut, board, verdicts, settings, NOW_ISO, result)
    return mut, result, tier2


def test_recovery_route_to_today_when_spine_supports(board, settings, db_path):
    mut, result, _ = _run_recovery(board, settings, db_path,
                                   [{"card_id": "rec_s71_1", "disposition": "today"}])
    today = board.list_by_name("Today").id
    assert any(e["card_id"] == "rec_s71_1" and e["target_list_id"] == today for e in _ops(mut, "move_card"))
    assert any("Recovered from" in e["text"] for e in _ops(mut, "add_comment"))


def test_recovery_today_cap_demotes_overflow_to_nfd(board, settings, db_path):
    verdicts = [{"card_id": f"rec_s71_{i}", "disposition": "today"} for i in (1, 2, 3, 4)]
    mut, result, _ = _run_recovery(board, settings, db_path, verdicts)
    today = board.list_by_name("Today").id
    nfd = board.list_by_name("Next Few Days").id
    today_moves = [e for e in _ops(mut, "move_card") if e["target_list_id"] == today]
    nfd_moves = [e for e in _ops(mut, "move_card") if e["target_list_id"] == nfd]
    assert len(today_moves) == settings.recovery_today_max == 3
    assert len(nfd_moves) == 1
    assert len(result.demoted_recoveries) == 1


def test_recovery_route_to_inbox_when_ambiguous(board, settings, db_path):
    mut, _, _ = _run_recovery(board, settings, db_path,
                              [{"card_id": "rec_s71_1", "disposition": "inbox"}])
    inbox = board.list_by_name("Inbox / Triage").id
    assert any(e["card_id"] == "rec_s71_1" and e["target_list_id"] == inbox for e in _ops(mut, "move_card"))


def test_recovery_merge_into_active_card(board, settings, db_path):
    mut, _, _ = _run_recovery(board, settings, db_path,
                              [{"card_id": "rec_s71_2", "disposition": "merge",
                                "merge_into": "dup_exact_a", "llm_tier": 1}])
    archive = board.list_by_name(settings.archive_list_name).id
    assert any(e["card_id"] == "rec_s71_2" and e["target_list_id"] == archive for e in _ops(mut, "move_card"))


def test_recovery_archive_auto_when_confident(board, settings, db_path):
    # Automatic mode default: a confident "no longer needed" card is archived now.
    mut, result, tier2 = _run_recovery(board, settings, db_path,
                                       [{"card_id": "rec_s71_1", "disposition": "archive", "confidence": 90}])
    assert tier2 == []
    archive = board.list_by_name(settings.archive_list_name).id
    assert any(e["card_id"] == "rec_s71_1" and e["target_list_id"] == archive
               and e.get("position") == "top" for e in _ops(mut, "move_card"))
    assert storage.archive_entry_ts(db_path, "rec_s71_1") == NOW_ISO
    assert any(a["card_id"] == "rec_s71_1" for a in result.recently_archived)
    # Wording is archive-not-deletion.
    assert any(ex.ARCHIVE_MOVED_WORDING in e["text"] for e in _ops(mut, "add_comment"))


def test_recovery_archive_borderline_proposed(board, settings, db_path):
    mut, result, tier2 = _run_recovery(board, settings, db_path,
                                       [{"card_id": "rec_s71_1", "disposition": "archive", "confidence": 40}])
    assert any(v["card_id"] == "rec_s71_1" for v in tier2)
    assert all(e["card_id"] != "rec_s71_1" for e in _ops(mut, "move_card"))


def test_recovery_archive_proposed_when_flag_off(board, make_settings, db_path):
    settings = make_settings(tier1_recovery_archive=False)
    mut, result, tier2 = _run_recovery(board, settings, db_path,
                                       [{"card_id": "rec_s71_1", "disposition": "archive", "confidence": 95}])
    assert any(v["card_id"] == "rec_s71_1" for v in tier2)


def test_recovery_routed_card_gets_origin_comment(board, settings, db_path):
    mut, _, _ = _run_recovery(board, settings, db_path,
                              [{"card_id": "rec_s71_1", "disposition": "next_few_days"}])
    assert any("Recovered from Scratch 7-1" in e["text"] for e in _ops(mut, "add_comment"))


def test_recovery_respects_max_recoveries_cap(board, make_settings, db_path):
    settings = make_settings(max_recoveries_per_run=1)
    verdicts = [{"card_id": "rec_s71_1", "disposition": "inbox"},
                {"card_id": "rec_s71_2", "disposition": "inbox"}]
    mut, result, _ = _run_recovery(board, settings, db_path, verdicts)
    assert result.counters["recoveries"] == 1
