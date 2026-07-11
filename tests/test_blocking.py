"""Section 3 — duplicate candidate blocking (deterministic, no LLM)."""

from datetime import datetime, timezone

from phases import candidates as cand


def _in_scope(board, settings):
    ids = {board.list_by_name(n).id for n in settings.edit_scope_lists}
    return [c for c in board.cards if c.list_id in ids and not c.closed]


def _wide(board, settings):
    in_ids = {board.list_by_name(n).id for n in settings.edit_scope_lists}
    scratch_ids = {l.id for l in __import__("phases.snapshot_diff", fromlist=["x"]).recovery_source_lists(board, settings)}
    comp_ids = {board.list_by_name(n).id for n in settings.comparison_extra_lists if board.list_by_name(n)}
    return [c for c in board.cards if (c.list_id in scratch_ids or c.list_id in comp_ids)
            and c.list_id not in in_ids and not c.closed]


def _pair_present(pairs, a_id, b_id):
    return any(p["a"].id == a_id and p["b"].id == b_id for p in pairs)


def test_narrow_track_passes_all_in_scope_names(board, settings, spine):
    kws = cand.build_entity_keywords(settings, spine)
    in_scope = _in_scope(board, settings)
    result = cand.narrow_track(in_scope, kws, settings)
    assert len(result["names"]) == len(in_scope)


def test_narrow_track_preclusters_hint_at_narrow_hint_jaccard(board, settings, spine):
    kws = cand.build_entity_keywords(settings, spine)
    result = cand.narrow_track(_in_scope(board, settings), kws, settings)
    assert any({"dup_exact_a", "dup_exact_b"} <= set(h) for h in result["hints"])


def test_narrow_track_below_hint_jaccard_not_clustered(board, settings, spine):
    kws = cand.build_entity_keywords(settings, spine)
    result = cand.narrow_track(_in_scope(board, settings), kws, settings)
    for h in result["hints"]:
        assert not ({"name_clean", "live_due"} <= set(h))


def test_wide_track_blocks_pair_at_wide_block_jaccard(board, settings, spine):
    kws = cand.build_entity_keywords(settings, spine)
    pairs = cand.wide_track(_in_scope(board, settings), _wide(board, settings), kws, settings)
    assert _pair_present(pairs, "dup_scratch_active", "dup_scratch_src")


def test_wide_track_blocks_shared_entity_plus_person_low_jaccard(board, settings, spine):
    kws = cand.build_entity_keywords(settings, spine)
    pairs = cand.wide_track(_in_scope(board, settings), _wide(board, settings), kws, settings)
    assert _pair_present(pairs, "wide_ep_active", "wide_ep_scratch")


def test_wide_track_entity_only_not_blocked(board, settings, spine):
    kws = cand.build_entity_keywords(settings, spine)
    pairs = cand.wide_track(_in_scope(board, settings), _wide(board, settings), kws, settings)
    assert not _pair_present(pairs, "wide_eo_active", "wide_eo_scratch")


def test_wide_track_below_threshold_not_sent(board, settings, spine):
    kws = cand.build_entity_keywords(settings, spine)
    pairs = cand.wide_track(_in_scope(board, settings), _wide(board, settings), kws, settings)
    assert not _pair_present(pairs, "wide_neg_active", "wide_neg_scratch")


def test_spine_person_names_appended_to_entity_keywords(settings, spine):
    kws = cand.build_entity_keywords(settings, spine)
    for name in ("colin", "logan", "dana", "ty"):
        assert name in kws


def test_weekly_sweep_on_sunday_sends_all_open_names(board, settings):
    sunday = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)  # local Sun 11:00 EDT
    assert cand.is_sweep_day(sunday, settings) is True
    names = cand.full_sweep_names(board)
    assert len(names) == len([c for c in board.cards if not c.closed])


def test_weekly_sweep_skipped_off_day(board, settings, now_utc):
    # now_utc is 2026-07-11 (Saturday)
    assert cand.is_sweep_day(now_utc, settings) is False
