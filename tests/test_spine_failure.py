"""
Spine-failure hardening: when the spine can't be read the run goes DEGRADED.

The in-scope archive, time-label, dead-due, and reprioritization passes are
skipped entirely (they lose their spine grounding); merges still run and Scratch
recovery is forced to Inbox / Triage; the report shows a prominent banner. A
normal run (spine_ok=True) is the control that proves those passes would otherwise
act on the same board + verdicts.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main
import settings as settings_mod
from phases import snapshot_diff as sd

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-06-15T12:00:00.000Z"

_LISTS = [
    {"id": "L_today", "name": "Today", "pos": 1},
    {"id": "L_inbox", "name": "Inbox / Triage", "pos": 2},
    {"id": "L_nfd", "name": "Next Few Days", "pos": 3},
    {"id": "L_week", "name": "This Week", "pos": 4},
    {"id": "L_arch", "name": "Agent Archive", "pos": 5},
    {"id": "L_s71", "name": "Scratch 7-1", "pos": 6},
]
_LABELS = [
    {"id": "LB_t", "name": "1. Today (must do)"},
    {"id": "LB_auto", "name": "Agent: Auto-Updated"},
    {"id": "LB_prop", "name": "Agent: Proposed"},
]


def _card(cid, name, list_id, labels=None, due=None, last=OLD):
    labels = labels or []
    idb = {lb["name"]: lb["id"] for lb in _LABELS}
    return {"id": cid, "name": name, "desc": "", "idList": list_id,
            "idLabels": [idb[n] for n in labels], "labels": [{"name": n} for n in labels],
            "due": due, "dateLastActivity": last, "closed": False, "shortUrl": "", "pos": 1,
            "badges": {}}


def _board():
    cards = [
        _card("dupA", "Review Forge rollout", "L_today"),
        _card("dupB", "Review Forge rollout", "L_today"),
        _card("arch1", "Return name tags", "L_today"),
        _card("due1", "Old task", "L_today", due="2026-05-01T17:00:00.000Z"),
        _card("rep1", "Idle reading", "L_today"),
        _card("stale1", "Buried task", "L_inbox", labels=["1. Today (must do)"]),
        _card("scr1", "Recovered thing", "L_s71"),
    ]
    return sd.build_board({"lists": _LISTS, "labels": _LABELS, "cards": cards, "comments": []})


_JUDGMENTS = {
    "clusters": [{"relation": "duplicate", "cluster_ids": ["dupA", "dupB"], "survivor_id": "dupA",
                  "new_name": None, "confidence": 95, "exact_or_near_name_match": True}],
    "hygiene": [{"card_id": "due1", "due_status": "no_longer_matters", "confidence": 90,
                 "reason": "Deadline long past."}],
    "inscope_archive": [{"card_id": "arch1", "confidence": 95, "reason": "Event passed."}],
    "labels": [{"card_id": "stale1", "disposition": "remove", "confidence": 100}],
    "recovery": [{"card_id": "scr1", "disposition": "today", "reason": "do today"}],
    "reprioritization": [{"card_id": "rep1", "direction": "down", "target_list": "Next Few Days",
                          "signals": ["weak"], "confidence": 90, "reason": "weak"}],
}


@pytest.fixture
def cfg():
    def _make(**overrides):
        s = settings_mod.load_settings("agent_config.json")
        for k, v in overrides.items():
            setattr(s, k, v)
        return s
    return _make


def _run(db_path, settings, spine_ok):
    return main.run_pipeline(_board(), settings, db_path, NOW, True, True,
                             spine=None, spine_ok=spine_ok, judgments=dict(_JUDGMENTS), trello=None)


def _ops(mut, op):
    return [e for e in mut.log if e["op"] == op]


def _moved_to(mut, card_id):
    return [e["target_list_id"] for e in mut.log if e["op"] == "move_card" and e["card_id"] == card_id]


# ── Degraded mode ──────────────────────────────────────────────────────────

def test_degraded_skips_spine_dependent_passes(cfg, db_path):
    s = cfg(spine_review_day="off", today_list_target=0)
    result, text, mut = _run(db_path, s, spine_ok=False)

    assert result.spine_unreadable is True
    assert "SPINE UNREADABLE — archiving, label, date, and reprioritization passes skipped" in text
    # Archive skipped: arch1 not moved to the Agent Archive list.
    assert "L_arch" not in _moved_to(mut, "arch1")
    # Dead-due skipped: no due mutations at all.
    assert _ops(mut, "clear_due") == [] and _ops(mut, "set_due") == []
    # Time-label skipped: stale1's must-do label never stripped.
    stale_sets = [e for e in _ops(mut, "set_labels") if e["card_id"] == "stale1"]
    assert stale_sets == []
    # Reprioritization skipped: rep1 not moved.
    assert _moved_to(mut, "rep1") == []


def test_degraded_still_merges_and_recovers_to_inbox(cfg, db_path):
    s = cfg(spine_review_day="off", today_list_target=0)
    result, text, mut = _run(db_path, s, spine_ok=False)

    # Merge still runs: the duplicate loser moves to the Agent Archive list.
    assert "L_arch" in _moved_to(mut, "dupB")
    assert any(a["type"] == "merge" for a in result.applied)
    # Recovery still runs but is forced to Inbox / Triage (not Today).
    assert _moved_to(mut, "scr1") == ["L_inbox"]


# ── Control: a normal run WOULD act on the same board + verdicts ───────────

def test_normal_run_acts_on_the_skipped_passes(cfg, db_path):
    s = cfg(spine_review_day="off", today_list_target=0)
    result, text, mut = _run(db_path, s, spine_ok=True)

    assert result.spine_unreadable is False
    assert "SPINE UNREADABLE" not in text
    assert "L_arch" in _moved_to(mut, "arch1")          # archived
    assert _ops(mut, "clear_due")                        # due cleared
    assert any(a.get("type") == "stale_label_removal" and a.get("card_id") == "stale1"
               for a in result.applied)                  # label removed
    assert _moved_to(mut, "rep1") == ["L_nfd"]           # reprioritized down
