"""Report rendering — actionable proposals, archive wording, titles+URLs, no bare IDs."""

from phases import execute as ex
from phases import report as rep


def _stats():
    return {"scratch_backlog": 5, "hygiene_coverage_pct": 90.0, "scratch_lists": ["Scratch 7-1"]}


def _build(result, board, settings, dry_run=True):
    return rep.build_report(result, board, settings, "2026-07-11T13:00:00+00:00",
                            dry_run=dry_run, first_run=False, stats=_stats(),
                            approval_rates={"approved": 0, "rejected": 0, "rate": "n/a"})


def test_proposal_entry_has_title_url_action_reason_confidence(board, settings):
    result = ex.ExecutionResult()
    result.proposals_opened.append({
        "proposal_id": 1, "fingerprint": "merge|x", "type": "merge",
        "card_id": "dup_person_a", "title": "Review Forge rollout",
        "url": "http://trello/pa", "action_desc": "merge duplicates into Review Forge rollout (http://trello/pb)",
        "reason": "Owners differ — confirm", "confidence": 80})
    text = _build(result, board, settings)
    assert "Review Forge rollout" in text
    assert "http://trello/pa" in text
    assert "merge duplicates into" in text
    assert "Owners differ — confirm" in text
    assert "Confidence: 80%" in text


def test_proposal_title_truncated_to_60_chars(board, settings):
    long_title = "X" * 120
    result = ex.ExecutionResult()
    result.proposals_opened.append({
        "proposal_id": 1, "type": "merge", "card_id": "c", "title": long_title,
        "url": "http://u", "action_desc": "merge", "reason": "r", "confidence": 50})
    text = _build(result, board, settings)
    assert "X" * 120 not in text
    assert "…" in text


def test_recently_archived_two_wordings_distinguished(board, settings):
    result = ex.ExecutionResult()
    result.recently_archived.append({"card_id": "a", "name": "Moved this run", "url": "http://a",
                                     "note": ex.archive_list_wording(settings)})
    result.recently_archived.append({"card_id": "b", "name": "Aged out", "url": "http://b",
                                     "note": ex.TRELLO_ARCHIVE_WORDING})
    text = _build(result, board, settings)
    assert "moved to the Agent Archive list (visible 60 days)" in text
    assert "moved to Trello's archive (restorable)" in text


def test_stale_label_entry_names_the_label(board, settings):
    result = ex.ExecutionResult()
    result.applied.append({"type": "stale_label_removal", "card_id": "dup_exact_a",
                           "label": "1. Today (must do)"})
    text = _build(result, board, settings, dry_run=False)
    assert "1. Today (must do)" in text
    assert "Send RSM comp plan to Colin" in text  # title, not the bare id


def test_done_automatically_uses_titles_not_bare_ids(board, settings):
    result = ex.ExecutionResult()
    result.applied.append({"type": "rename", "card_id": "name_pipe", "new_name": "Call Dana"})
    text = _build(result, board, settings, dry_run=False)
    # renders the card's title + url, never the raw id
    assert "name_pipe" not in text
    assert "http://trello/pipe" in text


def test_live_report_uses_past_tense_not_would(board, settings):
    result = ex.ExecutionResult()
    result.applied.append({"type": "rename", "card_id": "name_pipe", "new_name": "Call Dana"})
    text = _build(result, board, settings, dry_run=False)
    assert "renamed" in text and "would rename" not in text
