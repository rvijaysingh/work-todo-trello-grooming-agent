"""Section 13 — end-to-end happy paths through the full pipeline."""

from unittest.mock import MagicMock

import storage
from main import run_pipeline
from settings import Credentials


def _ops(mut, op):
    return [e for e in mut.log if e["op"] == op]


def test_e2e_dedup_merge_pipeline(board, make_settings, db_path, spine, now_utc, tmp_path):
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    judgments = {"clusters": [{"relation": "duplicate", "cluster_ids": ["dup_exact_a", "dup_exact_b"],
                               "survivor_id": "dup_exact_b", "new_name": None,
                               "exact_or_near_name_match": True, "llm_tier": 1}],
                 "hygiene": [], "recovery": []}
    result, text, mut = run_pipeline(board, settings, db_path, now_utc, dry_run=False, first_run=True,
                                     spine=spine, trello=MagicMock(), judgments=judgments)
    assert any(a["type"] == "merge" for a in result.applied)
    quar = board.list_by_name(settings.quarantine_list).id
    assert any(e["card_id"] == "dup_exact_a" and e["target_list_id"] == quar for e in _ops(mut, "move_card"))
    assert "merge" in text.lower()


def test_e2e_hygiene_pipeline(board, make_settings, db_path, spine, now_utc, tmp_path):
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    judgments = {"clusters": [], "recovery": [],
                 "hygiene": [{"card_id": "name_pipe", "new_name": "Call Dana and prep agenda"},
                             {"card_id": "dead_due", "clear_due": True}]}
    result, text, mut = run_pipeline(board, settings, db_path, now_utc, dry_run=False, first_run=True,
                                     spine=spine, trello=MagicMock(), judgments=judgments)
    assert any(e["card_id"] == "name_pipe" for e in _ops(mut, "rename"))
    assert any(e["card_id"] == "dead_due" for e in _ops(mut, "clear_due"))
    # Original title preserved in the rewritten description.
    descs = [e for e in mut.log if e["op"] == "set_description" and e["card_id"] == "name_pipe"]
    assert descs  # description was rewritten (with Original title line)


def test_e2e_recovery_pipeline(board, make_settings, db_path, spine, now_utc, tmp_path):
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    judgments = {"clusters": [], "hygiene": [],
                 "recovery": [{"card_id": "rec_s71_1", "disposition": "today"}]}
    result, text, mut = run_pipeline(board, settings, db_path, now_utc, dry_run=False, first_run=True,
                                     spine=spine, trello=MagicMock(), judgments=judgments)
    assert any(r["card_id"] == "rec_s71_1" for r in result.recoveries)
    assert "rec_s71_1" in storage.processed_recovery_ids(db_path)


def test_e2e_run_report_generated(board, make_settings, db_path, spine, now_utc, tmp_path):
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    _, text, _ = run_pipeline(board, settings, db_path, now_utc, dry_run=True, first_run=True,
                              spine=spine, trello=MagicMock(), judgments={"clusters": [], "hygiene": [], "recovery": []})
    for section in ("Grooming Report", "Auto-applied actions", "Open proposals",
                    "Quarantine", "Health", "approval rates"):
        assert section in text
    assert "ARCHIVE" in text  # pre-first-run reminder present in dry-run


def test_e2e_snapshot_saved_reflects_agent_changes(board, make_settings, db_path, spine, now_utc, tmp_path):
    settings = make_settings(report_file=str(tmp_path / "r.txt"))
    judgments = {"clusters": [], "recovery": [],
                 "hygiene": [{"card_id": "name_pipe", "new_name": "Call Dana and prep agenda"}]}
    run_pipeline(board, settings, db_path, now_utc, dry_run=False, first_run=True,
                 spine=spine, trello=MagicMock(), judgments=judgments)
    run_id = now_utc.isoformat()
    snap = storage.get_snapshot(db_path, run_id)
    # Post-run snapshot reflects the agent's rename (not the old name).
    assert snap["name_pipe"]["name"] == "Call Dana and prep agenda"


def test_e2e_three_failures_autopause(db_path):
    ts = "2026-07-11T13:00:00+00:00"
    assert storage.record_failure(db_path, ts, 3) == (1, False)
    assert storage.record_failure(db_path, ts, 3) == (2, False)
    failures, paused = storage.record_failure(db_path, ts, 3)
    assert failures == 3 and paused is True
    assert storage.is_paused(db_path)
    storage.clear_pause(db_path, ts)
    assert not storage.is_paused(db_path)


def test_run_exits_and_alerts_when_paused(monkeypatch, make_settings, tmp_path):
    import main
    settings = make_settings(db_path=str(tmp_path / "s.db"))
    storage.init_storage(settings.db_path)
    for _ in range(3):
        storage.record_failure(settings.db_path, "t", 3)

    creds = Credentials(trello_api_key="k", trello_token="t", notion_token="n",
                        gmail_sender="s@x", gmail_password="p", ollama_endpoint="http://x",
                        anthropic_api_key="a")
    monkeypatch.setattr(main, "load_settings", lambda p: settings)
    monkeypatch.setattr(main, "load_credentials", lambda p: creds)
    alert = MagicMock()
    monkeypatch.setattr(main, "send_alert", alert)

    assert main.run([]) == 2
    assert alert.called
