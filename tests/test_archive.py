"""Section 7 — single Agent Archive list lifecycle (auto-create, top placement,
60-day Trello-archive, scope exclusion, label expiry, no hard delete)."""

from datetime import timedelta
from unittest.mock import MagicMock

import storage
from phases import candidates as cand
from phases import execute as ex
from phases import snapshot_diff as sd


def _mut(dry=False):
    return ex.BoardMutator(MagicMock(), dry_run=dry)


def _ops(mut, op):
    return [e for e in mut.log if e["op"] == op]


def _days_ago(now_utc, days):
    return (now_utc - timedelta(days=days)).isoformat()


# ── Auto-create + top placement + scope exclusion ──────────────────────────

def test_archive_list_auto_created_when_absent(fresh_board, settings):
    def drop_archive(data):
        data["lists"] = [l for l in data["lists"] if l["name"] != settings.archive_list_name]
    board = fresh_board(drop_archive)
    assert board.list_by_name(settings.archive_list_name) is None
    mut = _mut(dry=False)
    mut.trello.create_list.return_value = type("L", (), {"id": "L_new_arch", "name": settings.archive_list_name, "position": 99.0})()
    lst = ex.ensure_archive_list(board, settings, mut)
    assert lst.id == "L_new_arch"
    assert board.list_by_name(settings.archive_list_name).id == "L_new_arch"
    assert _ops(mut, "create_list") and _ops(mut, "create_list")[0]["position"] == "bottom"


def test_archive_list_not_recreated_when_present(board, settings):
    mut = _mut()
    lst = ex.ensure_archive_list(board, settings, mut)
    assert lst.name == settings.archive_list_name
    assert _ops(mut, "create_list") == []


def test_archive_list_excluded_from_recovery_scope(board, settings):
    names = {l.name for l in sd.recovery_source_lists(board, settings)}
    assert settings.archive_list_name not in names


def test_archive_list_excluded_from_edit_scope(board, settings):
    in_scope = {board.list_by_name(n).id for n in settings.edit_scope_lists}
    assert board.list_by_name(settings.archive_list_name).id not in in_scope


# ── 60-day Trello-archive expiry (entry timestamp from SQLite) ──────────────

def test_archive_card_trello_archived_after_archive_list_days(board, settings, now_utc, db_path):
    storage.add_archive_entry(db_path, "arch_old", _days_ago(now_utc, settings.archive_list_days + 5))
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_archive(db_path, mut, board, settings, now_utc, result)
    assert any(e["card_id"] == "arch_old" for e in _ops(mut, "archive_card"))
    assert storage.archive_entry_ts(db_path, "arch_old") is None  # ledger cleared


def test_archive_card_within_window_not_archived(board, settings, now_utc, db_path):
    storage.add_archive_entry(db_path, "arch_new", _days_ago(now_utc, 3))
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_archive(db_path, mut, board, settings, now_utc, result)
    assert all(e["card_id"] != "arch_new" for e in _ops(mut, "archive_card"))


def test_archive_card_approaching_date_surfaced(board, settings, now_utc, db_path):
    storage.add_archive_entry(db_path, "arch_new", _days_ago(now_utc, settings.archive_list_days - 5))
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_archive(db_path, mut, board, settings, now_utc, result)
    assert any(a["card_id"] == "arch_new" for a in result.recently_archived)


def test_archive_expiry_falls_back_to_last_activity_without_ledger(board, settings, now_utc, db_path):
    # arch_old has last_activity ~82 days ago and no ledger row → archived.
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_archive(db_path, mut, board, settings, now_utc, result)
    assert any(e["card_id"] == "arch_old" for e in _ops(mut, "archive_card"))


def test_archive_pullback_not_archived(board, settings, now_utc, db_path):
    # arch_pullback sits in Today (dragged back out); never Trello-archived.
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_archive(db_path, mut, board, settings, now_utc, result)
    assert all(e["card_id"] != "arch_pullback" for e in _ops(mut, "archive_card"))


# ── Auto-Updated label expiry (optimistic_label_days window) ───────────────

def test_auto_updated_label_expires_after_window(board, settings, now_utc, db_path):
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_archive(db_path, mut, board, settings, now_utc, result)
    stripped = [e for e in _ops(mut, "set_labels") if e["card_id"] == "auto_old"]
    assert stripped and "LB_auto" not in stripped[0]["label_ids"]


def test_auto_updated_label_within_window_kept(board, settings, now_utc, db_path):
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_archive(db_path, mut, board, settings, now_utc, result)
    assert all(e["card_id"] != "auto_fresh" for e in _ops(mut, "set_labels"))


# ── Approved archive proposals route through the Agent Archive list ────────

def test_approved_archive_routes_through_archive_list(board, settings, now_utc, db_path):
    mut = _mut()
    result = ex.ExecutionResult()
    action = {"type": "recovery_archive", "card_ids": ["rec_s71_1"]}
    ex._execute_approved(db_path, mut, board, action, settings, now_utc,
                         "2026-07-11T13:00:00+00:00", result)
    archive = board.list_by_name(settings.archive_list_name).id
    assert any(e["card_id"] == "rec_s71_1" and e["target_list_id"] == archive
               and e.get("position") == "top" for e in _ops(mut, "move_card"))
    assert storage.archive_entry_ts(db_path, "rec_s71_1") == "2026-07-11T13:00:00+00:00"


# ── Nothing hard-deleted, ever ─────────────────────────────────────────────

def test_nothing_hard_deleted_ever(board, settings, now_utc, db_path):
    mut = ex.BoardMutator(MagicMock(), dry_run=False)
    result = ex.ExecutionResult()
    ex.expire_labels_and_archive(db_path, mut, board, settings, now_utc, result)
    ex.execute_merge(db_path, mut, board,
                     {"relation": "duplicate", "cluster_ids": ["dup_exact_a", "dup_exact_b"],
                      "survivor_id": "dup_exact_b", "new_name": None}, settings,
                     "2026-07-11T13:00:00+00:00", result)
    assert _ops(mut, "delete") == []
    assert not mut.trello.delete_card.called
