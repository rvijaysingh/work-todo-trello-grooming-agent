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
