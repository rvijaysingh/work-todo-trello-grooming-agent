"""Section 5.1 (revised) — dead-due classification against the spine.

Still-matters cards are never touched and surfaced under "Still overdue"; no-
longer-matters cards are re-dated ONLY from a written source, else cleared, with
the old date preserved in a comment. Auto vs proposed follows tier1_due_date_clear
and the borderline test.
"""

from unittest.mock import MagicMock

from phases import execute as ex

NOW_ISO = "2026-07-11T13:00:00+00:00"
DEAD = {"dead_due"}


def _mut():
    return ex.BoardMutator(MagicMock(), dry_run=True)


def _ops(mut, op):
    return [e for e in mut.log if e["op"] == op]


def _run(board, settings, db_path, now_utc, verdicts, spine_terms=None):
    mut = _mut()
    result = ex.ExecutionResult()
    tier2 = ex.execute_hygiene(db_path, mut, board, verdicts, set(), settings,
                               now_utc, NOW_ISO, result, dead_due_ids=DEAD,
                               spine_terms=spine_terms or [])
    return mut, result, tier2


def test_still_matters_due_never_touched_and_surfaced(board, settings, db_path, now_utc):
    mut, result, tier2 = _run(board, settings, db_path, now_utc,
                              [{"card_id": "dead_due", "due_status": "still_matters",
                                "reason": "Payer workstream still active"}])
    assert _ops(mut, "clear_due") == [] and _ops(mut, "set_due") == []
    assert any(s["card_id"] == "dead_due" for s in result.still_overdue)


def test_no_longer_matters_cleared_auto_with_comment(board, settings, db_path, now_utc):
    mut, result, tier2 = _run(board, settings, db_path, now_utc,
                              [{"card_id": "dead_due", "due_status": "no_longer_matters",
                                "confidence": 90, "reason": "Event passed"}])
    assert any(e["card_id"] == "dead_due" for e in _ops(mut, "clear_due"))
    comment = [e for e in _ops(mut, "add_comment") if e["card_id"] == "dead_due"][0]["text"]
    assert "Old due date was" in comment and "Confidence: 90%" in comment
    assert any(a["type"] == "dead_due_clear" for a in result.applied)


def test_redate_only_from_written_source(board, settings, db_path, now_utc):
    # New date is cited from the spine terms → re-date applied, not cleared.
    mut, result, tier2 = _run(board, settings, db_path, now_utc,
                              [{"card_id": "dead_due", "due_status": "no_longer_matters",
                                "new_due": "2026-08-15T00:00:00.000Z", "new_due_source": "2026-08-15",
                                "confidence": 90, "reason": "Rescheduled per spine deadline"}],
                              spine_terms=["Payer deadline moved to 2026-08-15"])
    assert any(e["card_id"] == "dead_due" for e in _ops(mut, "set_due"))
    assert _ops(mut, "clear_due") == []


def test_redate_rejected_when_source_absent_falls_back_to_clear(board, settings, db_path, now_utc):
    # The cited source substring is nowhere in the card text or spine → drop the
    # re-date and clear instead (never guess a date).
    mut, result, tier2 = _run(board, settings, db_path, now_utc,
                              [{"card_id": "dead_due", "due_status": "no_longer_matters",
                                "new_due": "2026-08-15T00:00:00.000Z", "new_due_source": "next quarter",
                                "confidence": 90}])
    assert _ops(mut, "set_due") == []
    assert any(e["card_id"] == "dead_due" for e in _ops(mut, "clear_due"))


def test_borderline_due_becomes_proposal(board, settings, db_path, now_utc):
    mut, result, tier2 = _run(board, settings, db_path, now_utc,
                              [{"card_id": "dead_due", "due_status": "no_longer_matters",
                                "confidence": 40}])
    assert _ops(mut, "clear_due") == []
    assert any(a["type"] == "dead_due_clear" for a in tier2)


def test_overdue_card_never_silently_dropped(board, settings, db_path, now_utc):
    # Regression (issue 5): an overdue in-scope dead-due card the LLM did NOT
    # classify must still surface as an escalation — never nothing.
    mut, result, tier2 = _run(board, settings, db_path, now_utc, verdicts=[])  # no verdicts at all
    assert any(s["card_id"] == "dead_due" for s in result.still_overdue)
    assert _ops(mut, "clear_due") == [] and _ops(mut, "set_due") == []


def test_every_dead_due_produces_escalation_or_fix(board, settings, db_path, now_utc):
    # Each dead-due card resolves to exactly one of: escalation (still_overdue) or
    # a fix (clear/redate action). Mix a still-matters and a no-longer-matters.
    verdicts = [{"card_id": "dead_due", "due_status": "no_longer_matters", "confidence": 90}]
    mut, result, tier2 = _run(board, settings, db_path, now_utc, verdicts)
    escalated = {s["card_id"] for s in result.still_overdue}
    fixed = {a["card_id"] for a in result.applied if a["type"] in ("dead_due_clear", "due_redate")}
    proposed = {a["card_ids"][0] for a in tier2}
    assert "dead_due" in (escalated | fixed | proposed)


def test_rename_verdict_does_not_clear_dead_due_date(board, settings, db_path, now_utc):
    # A rename verdict for a dead-due card must NOT trigger a due clear; the card
    # is escalated (unclassified) instead — the due decision needs an explicit
    # due_status from the dedicated pass.
    mut, result, tier2 = _run(board, settings, db_path, now_utc,
                              [{"card_id": "dead_due", "new_name": "Follow up with the payer"}])
    assert _ops(mut, "clear_due") == [] and _ops(mut, "set_due") == []
    assert any(s["card_id"] == "dead_due" for s in result.still_overdue)


def test_full_classification_produces_no_unclassified_fallback(board, settings, db_path, now_utc):
    # When the dedicated due pass classifies the card, the "Not classified" safety
    # net does not fire.
    mut, result, tier2 = _run(board, settings, db_path, now_utc,
                              [{"card_id": "dead_due", "due_status": "still_matters",
                                "reason": "Payer workstream active", "confidence": 85}])
    reasons = [s["reason"] for s in result.still_overdue if s["card_id"] == "dead_due"]
    assert reasons and all("Not classified" not in r for r in reasons)


def test_due_proposed_when_flag_off(board, make_settings, db_path, now_utc):
    settings = make_settings(tier1_due_date_clear=False)
    mut, result, tier2 = _run(board, settings, db_path, now_utc,
                              [{"card_id": "dead_due", "due_status": "no_longer_matters",
                                "confidence": 95}])
    assert _ops(mut, "clear_due") == []
    assert any(a["type"] == "dead_due_clear" for a in tier2)
