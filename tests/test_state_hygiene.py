"""Diff-state hygiene: dry-runs must not contaminate the rejection diff, the
post-run snapshot must faithfully reflect a live run, and reset_state clears
everything."""

from unittest.mock import MagicMock

import storage
from main import _post_snapshot, run_pipeline
from phases import execute as ex

_JUDGMENTS = {
    "clusters": [{"relation": "duplicate", "cluster_ids": ["dup_exact_a", "dup_exact_b"],
                  "survivor_id": "dup_exact_b", "new_name": "Send RSM comp plan to Colin",
                  "exact_or_near_name_match": True, "llm_tier": 1}],
    "hygiene": [{"card_id": "name_pipe", "new_name": "Call Dana and prep agenda"}],
    "recovery": [{"card_id": "rec_s71_1", "disposition": "inbox"}],
}


def test_dry_run_then_next_run_detects_zero_rejections(board, fresh_board, make_settings,
                                                       spine, now_utc, db_path, tmp_path):
    # Regression: a dry-run that "would" rename/merge/move must NOT leave state
    # that a later run misreads as user reversals of actions that never happened.
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    r1, _, _ = run_pipeline(board, settings, db_path, now_utc, dry_run=True, first_run=True,
                            spine=spine, trello=MagicMock(), judgments=_JUDGMENTS)
    assert r1.rejections_recorded == []

    # Second run on the UNCHANGED board (dry-run persisted nothing).
    board2 = fresh_board()
    r2, _, _ = run_pipeline(board2, settings, db_path, now_utc, dry_run=True, first_run=True,
                            spine=spine, trello=MagicMock(), judgments=_JUDGMENTS)
    assert r2.rejections_recorded == []
    assert storage.get_rejections(db_path) == []
    # No contaminating rows persisted by either dry-run.
    assert storage.latest_prior_run_id(db_path) is None
    assert storage.get_actions(db_path) == []
    assert storage.count_open_proposals(db_path) == 0


def test_reset_state_clears_all_tracked_state(db_path):
    ts = "2026-07-11T13:00:00+00:00"
    storage.save_snapshot(db_path, "run1", ts,
                          [type("C", (), {"id": "c", "list_id": "l", "name": "n", "desc": "",
                                          "label_names": [], "due": None, "last_activity": ts})()])
    storage.record_action(db_path, "run1", ts, 1, "merge", ["a", "b"], {}, "success")
    storage.add_proposal(db_path, "run1", "merge|a,b", ["a", "b"], {}, "r", ts)
    storage.add_rejection(db_path, "merge|a,b", "edit", ts)
    storage.add_recovery(db_path, "c", "Scratch", "inbox", ts)
    storage.add_archive_entry(db_path, "c", ts)
    storage.kv_set(db_path, "last_reminder_week", "2026-28")

    deleted = storage.reset_state(db_path)
    assert deleted["snapshots"] >= 1 and deleted["actions"] >= 1 and deleted["proposals"] >= 1

    assert storage.latest_prior_run_id(db_path) is None
    assert storage.get_actions(db_path) == []
    assert storage.count_open_proposals(db_path) == 0
    assert storage.get_rejections(db_path) == []
    assert storage.processed_recovery_ids(db_path) == set()
    assert storage.archive_entry_ts(db_path, "c") is None
    assert storage.kv_get(db_path, "last_reminder_week") is None


def test_post_snapshot_reflects_desc_labels_due(board):
    # The saved snapshot must match the true post-run board on every diffed field,
    # or a live merge/rename/relabel produces a phantom rejection next run.
    mut = ex.BoardMutator(MagicMock(), dry_run=False)
    mut.rename("dup_exact_b", "New Name")
    mut.set_description("dup_exact_b", "brand new description")
    mut.set_labels("dup_exact_b", ["LB_auto"])
    mut.set_due("dup_exact_b", "2026-09-01T00:00:00.000Z")
    mut.move_card("dup_exact_a", "L_archive", position="top")

    snap = {c.id: c for c in _post_snapshot(board, mut)}
    s = snap["dup_exact_b"]
    assert s.name == "New Name"
    assert s.desc == "brand new description"
    assert s.label_names == ["Agent: Auto-Updated"]  # names, not just ids
    assert s.due == "2026-09-01T00:00:00.000Z"
    assert snap["dup_exact_a"].list_id == "L_archive"
