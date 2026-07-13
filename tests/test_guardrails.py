"""
Sections 4, 5, 6 (pure-function parts) — tiering, name heuristics, fingerprints,
merge invariant, caps, name grounding, window predicates, never-touch.
"""

import guardrails as g


# ── Section 6: name-quality heuristics ─────────────────────────────────────

def test_name_flag_pipe_character():
    assert g.is_name_flagged("Call Dana | prep | send", 4, 100) is True


def test_name_flag_too_short():
    assert g.is_name_flagged("Fix", 4, 100) is True


def test_name_flag_too_long():
    assert g.is_name_flagged("x" * 101, 4, 100) is True


def test_name_flag_all_lowercase():
    assert g.is_name_flagged("review the intake queue", 4, 100) is True


def test_name_flag_double_space():
    assert g.is_name_flagged("Send  the deck", 4, 100) is True


def test_name_flag_leading_trailing_whitespace():
    assert g.is_name_flagged(" Trailing task ", 4, 100) is True


def test_name_clean_title_not_flagged():
    assert g.is_name_flagged("Review RSM comp with Colin", 4, 100) is False


# ── Section 1: fingerprints ────────────────────────────────────────────────

def test_fingerprint_sorts_card_ids_and_includes_new_name_for_renames():
    assert g.fingerprint("rename", ["b", "a"], "New Name") == "rename|a,b|New Name"


def test_fingerprint_merge_sorted_ids_no_name():
    assert g.fingerprint("merge", ["z", "a", "m"]) == "merge|a,m,z"


# ── Section 4: tier assignment ─────────────────────────────────────────────

def test_tier_exact_name_merge_forced_tier1(settings):
    assert g.assign_tier({"type": "merge", "exact_or_near_name_match": True}, settings) == 1


def test_tier_rename_forced_tier1(settings):
    assert g.assign_tier({"type": "rename"}, settings) == 1


def test_tier_desc_restructure_forced_tier1(settings):
    assert g.assign_tier({"type": "desc_restructure"}, settings) == 1


def test_tier_merge_diff_person_labels_forced_tier2(settings):
    assert g.assign_tier({"type": "merge", "cross_person_labels": True}, settings) == 2


def test_tier_merge_conflicting_due_forced_tier2(settings):
    assert g.assign_tier({"type": "merge", "conflicting_due": True}, settings) == 2


def test_tier_merge_survivor_outside_edit_scope_forced_tier2(settings):
    assert g.assign_tier({"type": "merge", "survivor_outside_edit_scope": True}, settings) == 2


def test_tier_merge_loser_has_attachment_forced_tier2(settings):
    assert g.assign_tier({"type": "merge", "loser_has_attachment": True}, settings) == 2


def test_tier_merge_loser_has_checklist_forced_tier2(settings):
    assert g.assign_tier({"type": "merge", "loser_has_checklist": True}, settings) == 2


def test_tier_recovery_archive_default_tier1_auto_mode(settings):
    # Automatic mode is the shipped default (tier1_recovery_archive=true).
    assert settings.tier1_recovery_archive is True
    assert g.assign_tier({"type": "recovery_archive", "confidence": 90}, settings) == 1


def test_tier_recovery_archive_tier2_when_flag_off(make_settings):
    s = make_settings(tier1_recovery_archive=False)
    assert g.assign_tier({"type": "recovery_archive", "confidence": 90}, s) == 2


def test_tier_recovery_archive_borderline_to_tier2(settings):
    # Flag on, but a low-confidence/borderline call still becomes a proposal.
    assert g.assign_tier({"type": "recovery_archive", "confidence": 40}, settings) == 2
    assert g.assign_tier({"type": "recovery_archive", "borderline": True}, settings) == 2


def test_tier_stale_label_removal_default_tier1_auto_mode(settings):
    assert settings.tier1_stale_label_removal is True
    assert g.assign_tier({"type": "stale_label_removal", "confidence": 100}, settings) == 1


def test_tier_stale_label_removal_tier2_when_flag_off(make_settings):
    s = make_settings(tier1_stale_label_removal=False)
    assert g.assign_tier({"type": "stale_label_removal", "confidence": 100}, s) == 2


def test_tier_due_clear_default_tier1_auto_mode(settings):
    assert settings.tier1_due_date_clear is True
    assert g.assign_tier({"type": "dead_due_clear", "confidence": 90}, settings) == 1
    assert g.assign_tier({"type": "due_redate", "confidence": 90}, settings) == 1


def test_tier_due_clear_tier2_when_flag_off(make_settings):
    s = make_settings(tier1_due_date_clear=False)
    assert g.assign_tier({"type": "dead_due_clear", "confidence": 90}, s) == 2


def test_tier_due_clear_borderline_to_tier2(settings):
    assert g.assign_tier({"type": "dead_due_clear", "confidence": 50}, settings) == 2


def test_is_borderline_below_auto_min_confidence(settings):
    assert g.is_borderline({"confidence": settings.auto_min_confidence - 1}, settings) is True
    assert g.is_borderline({"confidence": settings.auto_min_confidence}, settings) is False
    assert g.is_borderline({}, settings) is False  # unscored deterministic action


def test_tier_llm_high_confidence_cannot_override_forced_tier2(settings):
    """LLM says Tier 1 (exact match + llm_tier 1) but loser has an attachment → Tier 2."""
    action = {"type": "merge", "exact_or_near_name_match": True,
              "loser_has_attachment": True, "llm_tier": 1}
    assert g.assign_tier(action, settings) == 2


# ── Section 4: never-touch (card edited within 12h → no action) ────────────

def test_tier_card_edited_within_no_touch_never_touched(board, make_settings, now_utc):
    settings = make_settings(no_touch_hours=12)
    card = board.card_by_id("recent_edit")  # last_activity 4h before now
    in_scope = {board.list_by_name(n).id for n in settings.edit_scope_lists}
    assert g.is_never_touch(card, now_utc, settings, in_scope, set(), None) is True


def test_no_touch_zero_recent_edit_is_touchable(board, settings, now_utc):
    assert settings.no_touch_hours == 0
    card = board.card_by_id("recent_edit")
    in_scope = {board.list_by_name(n).id for n in settings.edit_scope_lists}
    assert g.is_never_touch(card, now_utc, settings, in_scope, set(), None) is False


def test_never_touch_clean_in_scope_card_is_touchable(board, settings, now_utc):
    card = board.card_by_id("name_clean")
    in_scope = {board.list_by_name(n).id for n in settings.edit_scope_lists}
    assert g.is_never_touch(card, now_utc, settings, in_scope, set(), None) is False


def test_never_touch_out_of_scope_card(board, settings, now_utc):
    card = board.card_by_id("dup_scratch_src")  # in a Scratch list
    in_scope = {board.list_by_name(n).id for n in settings.edit_scope_lists}
    assert g.is_never_touch(card, now_utc, settings, in_scope, set(), None) is True


def test_never_touch_open_proposed_card(board, settings, now_utc):
    card = board.card_by_id("proposed_card")
    in_scope = {board.list_by_name(n).id for n in settings.edit_scope_lists}
    assert g.is_never_touch(card, now_utc, settings, in_scope, set(), None) is True


# ── Section 5: merge content invariant ─────────────────────────────────────

def test_merge_invariant_survivor_contains_all_source_text(board):
    from phases.execute import compose_survivor_desc
    survivor = board.card_by_id("dup_exact_b")
    loser = board.card_by_id("dup_exact_a")
    desc = compose_survivor_desc(survivor, [loser])
    assert g.merge_contains_all_sources(desc, [survivor, loser]) is True
    assert survivor.name in desc and loser.name in desc
    assert loser.desc in desc


def test_merge_invariant_detects_missing_source():
    from models import Card
    survivor = Card(id="s", name="S", desc="sd")
    loser = Card(id="l", name="LoserName", desc="loser body text")
    bad_desc = "Original title: S\n\nsd\n"  # loser content missing
    assert g.merge_contains_all_sources(bad_desc, [survivor, loser]) is False


# ── Section 5: caps ────────────────────────────────────────────────────────

def test_apply_cap_limits_count():
    allowed, deferred = g.apply_cap([1, 2, 3, 4], 2)
    assert allowed == [1, 2] and deferred == [3, 4]


def test_apply_cap_prioritizes_flagged():
    items = [{"id": "a", "flag": False}, {"id": "b", "flag": True}]
    allowed, deferred = g.apply_cap(items, 1, priority=lambda x: x["flag"])
    assert allowed[0]["id"] == "b"


# ── Section 5: name grounding ──────────────────────────────────────────────

def test_name_grounding_allows_names_from_source(spine):
    assert g.name_is_grounded("Send comp plan to Colin", ["Send RSM comp plan to Colin"], spine.all_terms()) is True


def test_name_grounding_rejects_invented_entity(spine):
    assert g.name_is_grounded("Send comp plan to Zephyr", ["Send RSM comp plan to Colin"], spine.all_terms()) is False


# ── Date grounding (re-date only from a written source) ────────────────────

def test_value_written_in_sources_true_when_present():
    assert g.value_written_in_sources("2026-08-15", ["Payer deadline moved to 2026-08-15"]) is True


def test_value_written_in_sources_false_when_absent():
    assert g.value_written_in_sources("2026-08-15", ["no date here"]) is False


def test_value_written_in_sources_empty_is_false():
    assert g.value_written_in_sources(None, ["anything"]) is False


# ── Archive-list expiry predicate ──────────────────────────────────────────

def test_archive_list_expired_beyond_window(settings, now_utc):
    from datetime import timedelta
    old = (now_utc - timedelta(days=settings.archive_list_days + 1)).isoformat()
    assert g.archive_list_expired(old, now_utc, settings) is True


def test_archive_list_not_expired_within_window(settings, now_utc):
    from datetime import timedelta
    recent = (now_utc - timedelta(days=5)).isoformat()
    assert g.archive_list_expired(recent, now_utc, settings) is False


# ── Section 5/1: window predicates ─────────────────────────────────────────

def test_dead_due_beyond_window_is_dead(board, settings, now_utc):
    assert g.due_is_dead(board.card_by_id("dead_due").due, now_utc, settings) is True


def test_dead_due_within_window_left_alone(board, settings, now_utc):
    assert g.due_is_dead(board.card_by_id("live_due").due, now_utc, settings) is False


def test_label_expired_beyond_window(board, settings, now_utc):
    assert g.label_expired(board.card_by_id("auto_old").last_activity, now_utc, settings) is True


def test_label_within_window_not_expired(board, settings, now_utc):
    assert g.label_expired(board.card_by_id("auto_fresh").last_activity, now_utc, settings) is False
