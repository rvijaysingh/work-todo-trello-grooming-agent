"""Section 8 — weekly spine-review reminder card (create / skip / off)."""

from unittest.mock import MagicMock

import storage
from phases import execute as ex

NOW_ISO = "2026-07-11T13:00:00+00:00"  # a Saturday


def _mut():
    return ex.BoardMutator(MagicMock(), dry_run=True)


def _ops(mut, op):
    return [e for e in mut.log if e["op"] == op]


def test_reminder_created_on_or_after_review_day(board, settings, spine, db_path, now_utc):
    # spine_review_day default "monday"; now is Saturday → on/after → create.
    mut = _mut()
    result = ex.ExecutionResult()
    ex.maybe_create_spine_reminder(db_path, mut, board, settings, spine, now_utc, result)
    created = [e for e in _ops(mut, "create_card") if e["name"] == ex.REMINDER_CARD_TITLE]
    assert created and result.reminder_created is True
    assert storage.kv_get(db_path, "last_reminder_week") is not None


def test_reminder_skipped_when_already_open(fresh_board, settings, spine, db_path, now_utc):
    def add_reminder(data):
        data["cards"].append({
            "id": "existing_reminder", "idList": "L_today", "name": ex.REMINDER_CARD_TITLE,
            "desc": "", "due": None, "dateLastActivity": "2026-07-10T12:00:00.000Z",
            "idLabels": [], "labels": [], "badges": {"attachments": 0, "checkItems": 0},
            "shortUrl": "http://trello/rem", "closed": False})
    board = fresh_board(add_reminder)
    mut = _mut()
    result = ex.ExecutionResult()
    ex.maybe_create_spine_reminder(db_path, mut, board, settings, spine, now_utc, result)
    assert [e for e in _ops(mut, "create_card") if e["name"] == ex.REMINDER_CARD_TITLE] == []
    assert result.reminder_created is False


def test_reminder_off_disables(board, make_settings, spine, db_path, now_utc):
    settings = make_settings(spine_review_day="off")
    mut = _mut()
    result = ex.ExecutionResult()
    ex.maybe_create_spine_reminder(db_path, mut, board, settings, spine, now_utc, result)
    assert _ops(mut, "create_card") == []


def test_reminder_skipped_before_review_day(board, make_settings, spine, db_path, now_utc):
    # now is Saturday (weekday 5); review day Sunday (6) → not yet this week.
    settings = make_settings(spine_review_day="sunday")
    mut = _mut()
    result = ex.ExecutionResult()
    ex.maybe_create_spine_reminder(db_path, mut, board, settings, spine, now_utc, result)
    assert _ops(mut, "create_card") == []


def test_reminder_created_once_per_week(board, settings, spine, db_path, now_utc):
    mut1 = _mut()
    r1 = ex.ExecutionResult()
    ex.maybe_create_spine_reminder(db_path, mut1, board, settings, spine, now_utc, r1)
    mut2 = _mut()
    r2 = ex.ExecutionResult()
    ex.maybe_create_spine_reminder(db_path, mut2, board, settings, spine, now_utc, r2)
    assert r1.reminder_created is True
    assert [e for e in _ops(mut2, "create_card") if e["name"] == ex.REMINDER_CARD_TITLE] == []


def test_reminder_title_card_is_protected_from_hygiene(fresh_board, settings):
    def add_reminder(data):
        data["cards"].append({
            "id": "existing_reminder", "idList": "L_today", "name": ex.REMINDER_CARD_TITLE,
            "desc": "", "due": None, "dateLastActivity": "2026-07-10T12:00:00.000Z",
            "idLabels": [], "labels": [], "badges": {"attachments": 0, "checkItems": 0},
            "shortUrl": "http://trello/rem", "closed": False})
    board = fresh_board(add_reminder)
    protected = ex._protected_card_ids(board, settings)
    assert "existing_reminder" in protected
