"""Section 10 — dry-run mode performs zero board mutations."""

import os
from unittest.mock import MagicMock

from main import run_pipeline

_JUDGMENTS = {
    "clusters": [{"relation": "duplicate", "cluster_ids": ["dup_exact_a", "dup_exact_b"],
                  "survivor_id": "dup_exact_b", "new_name": None,
                  "exact_or_near_name_match": True, "llm_tier": 1}],
    "hygiene": [{"card_id": "name_pipe", "new_name": "Call Dana and prep agenda"}],
    "recovery": [{"card_id": "rec_s71_1", "disposition": "today"}],
}

_WRITE_METHODS = ("update_card", "move_card", "add_comment", "create_card")


def test_dry_run_zero_board_mutations(board, make_settings, db_path, spine, now_utc, tmp_path):
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    trello = MagicMock()
    run_pipeline(board, settings, db_path, now_utc, dry_run=True, first_run=True,
                 spine=spine, trello=trello, judgments=_JUDGMENTS)
    for m in _WRITE_METHODS:
        assert getattr(trello, m).call_count == 0, f"{m} was called in dry-run"


def test_dry_run_writes_report_file(board, make_settings, db_path, spine, now_utc, tmp_path):
    report = str(tmp_path / "r.txt")
    settings = make_settings(report_file=report)
    run_pipeline(board, settings, db_path, now_utc, dry_run=True, first_run=True,
                 spine=spine, trello=MagicMock(), judgments=_JUDGMENTS)
    assert os.path.exists(report)


def test_dry_run_still_reads_and_computes(board, make_settings, db_path, spine, now_utc, tmp_path):
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    result, text, mut = run_pipeline(board, settings, db_path, now_utc, dry_run=True, first_run=True,
                                     spine=spine, trello=MagicMock(), judgments=_JUDGMENTS)
    # Pipeline still planned actions (logged) and produced a report, despite no writes.
    assert len(mut.log) > 0
    assert "Grooming Report" in text


def test_live_run_applies_mutations(board, make_settings, db_path, spine, now_utc, tmp_path):
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    trello = MagicMock()
    run_pipeline(board, settings, db_path, now_utc, dry_run=False, first_run=True,
                 spine=spine, trello=trello, judgments=_JUDGMENTS)
    assert trello.update_card.called


# ── Issue 6: dry-run makes zero board writes AND zero persistent-state changes ──

_JUDGMENTS_PROP = {
    "clusters": [{"relation": "duplicate", "cluster_ids": ["dup_person_a", "dup_person_b"],
                  "survivor_id": "dup_person_a", "exact_or_near_name_match": False,
                  "llm_tier": 2, "confidence": 80, "reason": "Owners differ — please confirm"}],
    "hygiene": [], "recovery": [{"card_id": "rec_s71_1", "disposition": "archive", "confidence": 95}],
}


def test_dry_run_reminder_and_all_writes_zero_trello_calls(board, make_settings, db_path, spine, now_utc, tmp_path):
    # now is a Saturday; spine_review_day default 'monday' → the reminder fires.
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    trello = MagicMock()
    result, _, _ = run_pipeline(board, settings, db_path, now_utc, dry_run=True, first_run=True,
                                spine=spine, trello=trello, judgments=_JUDGMENTS_PROP)
    assert result.reminder_created is True  # simulated
    for m in ("update_card", "move_card", "add_comment", "create_card", "create_list"):
        assert getattr(trello, m).call_count == 0, f"{m} called during dry-run"


def test_dry_run_persists_no_state(board, make_settings, db_path, spine, now_utc, tmp_path):
    import storage
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    run_pipeline(board, settings, db_path, now_utc, dry_run=True, first_run=True,
                 spine=spine, trello=MagicMock(), judgments=_JUDGMENTS_PROP)
    assert storage.processed_recovery_ids(db_path) == set()      # recovery ledger untouched
    assert storage.count_open_proposals(db_path) == 0            # no proposal rows
    assert storage.archive_entry_ts(db_path, "dup_person_b") is None  # no archive-ledger row
    assert storage.kv_get(db_path, "last_reminder_week") is None      # weekly slot not consumed
    assert storage.latest_prior_run_id(db_path) is None              # no snapshot baseline written


def test_dry_run_report_uses_would_wording(board, make_settings, db_path, spine, now_utc, tmp_path):
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    _, text, _ = run_pipeline(board, settings, db_path, now_utc, dry_run=True, first_run=True,
                              spine=spine, trello=MagicMock(), judgments=_JUDGMENTS)
    assert "would create the weekly spine-review reminder" in text
    assert "would merge" in text or "would move" in text or "would rename" in text
