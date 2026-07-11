"""Section 7 — quarantine lifecycle (label expiry, auto-archive, no hard delete)."""

from unittest.mock import MagicMock

from phases import execute as ex


def _mut():
    return ex.BoardMutator(MagicMock(), dry_run=True)


def _ops(mut, op):
    return [e for e in mut.log if e["op"] == op]


def test_auto_updated_label_expires_after_quarantine_days(board, settings, now_utc):
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_quarantine(mut, board, settings, now_utc, result)
    stripped = [e for e in _ops(mut, "set_labels") if e["card_id"] == "auto_old"]
    assert stripped and "LB_auto" not in stripped[0]["label_ids"]


def test_auto_updated_label_within_window_kept(board, settings, now_utc):
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_quarantine(mut, board, settings, now_utc, result)
    assert all(e["card_id"] != "auto_fresh" for e in _ops(mut, "set_labels"))


def test_quarantined_card_auto_archives_after_7_days(board, settings, now_utc):
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_quarantine(mut, board, settings, now_utc, result)
    assert any(e["card_id"] == "quar_old" for e in _ops(mut, "archive_card"))


def test_quarantined_card_within_window_not_archived(board, settings, now_utc):
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_quarantine(mut, board, settings, now_utc, result)
    assert all(e["card_id"] != "quar_new" for e in _ops(mut, "archive_card"))
    assert any(q["card_id"] == "quar_new" for q in result.quarantine_items)


def test_quarantine_pullback_not_archived(board, settings, now_utc):
    """A card dragged back out of quarantine (now in Today) is not archived."""
    mut = _mut()
    result = ex.ExecutionResult()
    ex.expire_labels_and_quarantine(mut, board, settings, now_utc, result)
    assert all(e["card_id"] != "quar_pullback" for e in _ops(mut, "archive_card"))


def test_nothing_hard_deleted_ever(board, settings, now_utc, db_path):
    mut = ex.BoardMutator(MagicMock(), dry_run=False)
    result = ex.ExecutionResult()
    ex.expire_labels_and_quarantine(mut, board, settings, now_utc, result)
    ex.execute_merge(db_path, mut, board,
                     {"relation": "duplicate", "cluster_ids": ["dup_exact_a", "dup_exact_b"],
                      "survivor_id": "dup_exact_b", "new_name": None}, settings,
                     "2026-07-11T13:00:00+00:00", result)
    assert _ops(mut, "delete") == []
    assert not mut.trello.delete_card.called
