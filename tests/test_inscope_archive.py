"""Item 2 — in-scope 'no longer needed' archiving, and precedence merge > archive
> date/label/title fixes."""

from unittest.mock import MagicMock

import guardrails as g
import storage
from main import _merge_owner_archives
from phases import execute as ex

NOW_ISO = "2026-07-11T13:00:00+00:00"


def _mut():
    return ex.BoardMutator(MagicMock(), dry_run=False)


def _ops(mut, op):
    return [e for e in mut.log if e["op"] == op]


def _inscope(board, settings):
    ids = {board.list_by_name(n).id for n in settings.edit_scope_lists}
    return [c for c in board.cards if c.list_id in ids and not c.closed]


def test_owner_titled_is_deterministic_archive_candidate():
    assert g.is_owner_titled("[Owner: Marah] Send Cooper the dashboard") is True
    assert g.is_owner_titled("Send Cooper the dashboard") is False


def test_merge_owner_archives_flags_owner_cards(board, settings):
    inscope = _inscope(board, settings)
    verdicts = _merge_owner_archives(inscope, [])
    assert any(v["card_id"] == "owner_dead" and v["confidence"] == 100 for v in verdicts)


def test_inscope_owner_card_archived_not_date_fixed(board, settings, db_path, now_utc):
    # Regression (item 2): an in-scope [Owner: X] card produces an ARCHIVE action,
    # not a due-date fix — even though it is also dead-due.
    mut = _mut()
    result = ex.ExecutionResult()
    archives = _merge_owner_archives(_inscope(board, settings), [])
    tier2, archived = ex.execute_inscope_archive(db_path, mut, board, archives, settings,
                                                 now_utc, NOW_ISO, result, skip_ids=set())
    assert "owner_dead" in archived
    arch_list = board.list_by_name(settings.archive_list_name).id
    assert any(e["card_id"] == "owner_dead" and e["target_list_id"] == arch_list
               and e.get("position") == "top" for e in _ops(mut, "move_card"))

    # Now the due handler must SKIP the archived card (precedence).
    mut2 = _mut()
    r2 = ex.ExecutionResult()
    due_verdicts = [{"card_id": "owner_dead", "due_status": "no_longer_matters", "confidence": 95}]
    ex.execute_hygiene(db_path, mut2, board, due_verdicts, set(), settings, now_utc, NOW_ISO, r2,
                       dead_due_ids={"owner_dead"}, skip_ids=archived)
    assert _ops(mut2, "clear_due") == [] and _ops(mut2, "set_due") == []
    assert all(s["card_id"] != "owner_dead" for s in r2.still_overdue)


def test_inscope_archive_borderline_proposed(board, settings, db_path, now_utc):
    mut = _mut()
    result = ex.ExecutionResult()
    verdicts = [{"card_id": "name_clean", "confidence": 40, "reason": "maybe done"}]
    tier2, archived = ex.execute_inscope_archive(db_path, mut, board, verdicts, settings,
                                                 now_utc, NOW_ISO, result)
    assert archived == set()
    assert any(a["type"] == "inscope_archive" for a in tier2)


def test_inscope_archive_proposed_when_flag_off(board, make_settings, db_path, now_utc):
    settings = make_settings(tier1_recovery_archive=False)
    mut = _mut()
    result = ex.ExecutionResult()
    verdicts = [{"card_id": "owner_dead", "confidence": 100}]
    tier2, archived = ex.execute_inscope_archive(db_path, mut, board, verdicts, settings,
                                                 now_utc, NOW_ISO, result)
    assert archived == set() and any(a["type"] == "inscope_archive" for a in tier2)


def test_inscope_archive_respects_cap(board, make_settings, db_path, now_utc):
    settings = make_settings(max_inscope_archives_per_run=1)
    mut = _mut()
    result = ex.ExecutionResult()
    verdicts = [{"card_id": "name_clean", "confidence": 100},
                {"card_id": "name_lower", "confidence": 100}]
    tier2, archived = ex.execute_inscope_archive(db_path, mut, board, verdicts, settings,
                                                 now_utc, NOW_ISO, result)
    assert result.counters["inscope_archives"] == 1


def test_inscope_archive_skips_merge_claimed(board, settings, db_path, now_utc):
    mut = _mut()
    result = ex.ExecutionResult()
    verdicts = [{"card_id": "owner_dead", "confidence": 100}]
    tier2, archived = ex.execute_inscope_archive(db_path, mut, board, verdicts, settings,
                                                 now_utc, NOW_ISO, result, skip_ids={"owner_dead"})
    assert archived == set() and _ops(mut, "move_card") == []


# ── Precedence: duplicates are owned by the merge pass, never archived ───────

def test_is_duplicate_archive_reason_matches_variants():
    assert ex.is_duplicate_archive_reason("Redundant copy of dup_exact_b") is True
    assert ex.is_duplicate_archive_reason("This duplicates another card") is True
    assert ex.is_duplicate_archive_reason("A redundant duplicate") is True
    assert ex.is_duplicate_archive_reason("Workstream marked Done on the spine") is False
    assert ex.is_duplicate_archive_reason("") is False
    assert ex.is_duplicate_archive_reason(None) is False


def test_inscope_archive_skips_duplicate_reasoned_verdict(board, settings, db_path, now_utc):
    # Regression: the in-scope archive pass must NOT archive (or propose to archive)
    # a card whose reason says it is a duplicate/redundant copy — even when no merge
    # claimed it. Duplicates belong to the merge pipeline (merge > archive).
    mut = _mut()
    result = ex.ExecutionResult()
    verdicts = [{"card_id": "dup_exact_a", "confidence": 95,
                 "reason": "Redundant duplicate copy of dup_exact_b"}]
    tier2, archived = ex.execute_inscope_archive(db_path, mut, board, verdicts, settings,
                                                 now_utc, NOW_ISO, result)
    assert archived == set()
    assert tier2 == []                       # not proposed either
    assert _ops(mut, "move_card") == []
    assert any("duplicate" in n.lower() for n in result.notes)
