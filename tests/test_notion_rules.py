"""Notion-read live config: the spine "Rules and thresholds" section (§7).

Valid values override agent_config.json for the run; invalid values and unknown
keys are ignored with a report note; a missing section or unreachable spine falls
back silently to the file; a Notion-set dry_run behaves like the file flag.
"""

import spine as spine_mod
from spine import apply_notion_overrides, load_spine_from_dict, parse_spine_blocks


def _spine(rules):
    return load_spine_from_dict({"workstreams": [], "people": [], "notes": [], "rules": rules})


# ── Block parsing ──────────────────────────────────────────────────────────

def _heading(text):
    return {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": text}]}}


def _bullet(text):
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": text}]}}


def test_rules_section_parsed_from_blocks():
    blocks = [
        _heading("Rules and thresholds"),
        _bullet("recovery_batch_size: 5   (how many scratch cards per run)"),
        _bullet("dry_run: true"),
        _heading("Card naming standard"),
        _bullet("Start with a verb."),
    ]
    s = parse_spine_blocks(blocks)
    assert s.rules["recovery_batch_size"].startswith("5")
    assert s.rules["dry_run"] == "true"
    assert s.naming_standard == ["Start with a verb."]


# ── Override application ────────────────────────────────────────────────────

def test_valid_override_applies_and_notes(settings):
    notes, dry = apply_notion_overrides(settings, _spine({"recovery_batch_size": "5 per run"}))
    assert settings.recovery_batch_size == 5
    assert any("recovery_batch_size" in n and "spine" in n for n in notes)
    assert dry is False


def test_tier_flag_override_applies(settings):
    apply_notion_overrides(settings, _spine({"tier1_recovery_archive": "false"}))
    assert settings.tier1_recovery_archive is False


def test_invalid_value_ignored_with_note(settings):
    before = settings.recovery_batch_size
    notes, _ = apply_notion_overrides(settings, _spine({"recovery_batch_size": "abc"}))
    assert settings.recovery_batch_size == before
    assert any("invalid value" in n and "recovery_batch_size" in n for n in notes)


def test_unknown_key_ignored_with_note(settings):
    notes, _ = apply_notion_overrides(settings, _spine({"totally_made_up": "9"}))
    assert any("unknown key" in n for n in notes)


def test_value_rejected_by_validation_is_reverted(settings):
    before = settings.recovery_today_max
    notes, _ = apply_notion_overrides(settings, _spine({"recovery_today_max": "-3"}))
    assert settings.recovery_today_max == before
    assert any("recovery_today_max" in n for n in notes)


def test_dry_run_from_notion(settings):
    settings.dry_run = False
    notes, dry = apply_notion_overrides(settings, _spine({"dry_run": "true"}))
    assert settings.dry_run is True
    assert dry is True
    assert any("dry_run" in n for n in notes)


def test_missing_section_falls_back_silently(settings):
    before = settings.recovery_batch_size
    notes, dry = apply_notion_overrides(settings, _spine({}))
    assert notes == [] and dry is False
    assert settings.recovery_batch_size == before


def test_unreachable_spine_falls_back_silently(settings):
    notes, dry = apply_notion_overrides(settings, None)
    assert notes == [] and dry is False


def test_spine_review_day_override(settings):
    apply_notion_overrides(settings, _spine({"spine_review_day": "off"}))
    assert settings.spine_review_day == "off"
