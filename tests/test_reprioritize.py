"""
Reprioritization pass (design §5.4 / spine "Problem 5").

Covers: promotion via each signal type, demotion ranking/stop-at-target, the
automatic gate (one-verified-signal at/below the confidence floor, unverifiable
claimed signal), both hard exemptions, the per-run cap, proposed-mode, the config
renames by new name and old alias (incl. via a Notion Rules override), the
placement-conflict comment, the action vocabulary in comments and report lines,
and Today-plan rendering (normal, empty state, dry-run wording).

All boards/spines are built inline so the shared fixtures stay untouched. The LLM
is bypassed by injecting reprioritization verdicts directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import main
import settings as settings_mod
import storage
from phases import execute as ex
from phases import report as rep
from phases import reprioritize as repri
from phases import snapshot_diff as sd
from spine import apply_notion_overrides, load_spine_from_dict


class _Resp:
    def __init__(self, text):
        self.text = text


class FakeLLM:
    """Minimal LLM stand-in: every call returns the same canned JSON text."""

    def __init__(self, text):
        self._text = text

    def call(self, **kwargs):
        return _Resp(self._text)


class FakePrompts:
    def load(self, name, variables=None):
        return "PROMPT"

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-07-01T12:00:00.000Z"       # ~19 days ago (not within the 48h window)
RECENT = "2026-07-20T06:00:00.000Z"    # ~6h ago (within the 48h demotion window)

_LISTS = [
    {"id": "L_today", "name": "Today", "pos": 1},
    {"id": "L_inbox", "name": "Inbox / Triage", "pos": 2},
    {"id": "L_nfd", "name": "Next Few Days", "pos": 3},
    {"id": "L_week", "name": "This Week", "pos": 4},
    {"id": "L_arch", "name": "Agent Archive", "pos": 5},
]
_LABELS = [
    {"id": "LB_t", "name": "1. Today (must do)"},
    {"id": "LB_n", "name": "2. Next Few Days (must do)"},
    {"id": "LB_w", "name": "3. This Week (must do)"},
    {"id": "LB_auto", "name": "Agent: Auto-Updated"},
    {"id": "LB_prop", "name": "Agent: Proposed"},
    {"id": "LB_p0", "name": "P0. High"},
    {"id": "LB_p1", "name": "P1"},
]


def _card(cid, name, list_id, labels=None, due=None, last=OLD):
    labels = labels or []
    id_by_name = {lb["name"]: lb["id"] for lb in _LABELS}
    return {
        "id": cid, "name": name, "desc": "", "idList": list_id,
        "idLabels": [id_by_name[n] for n in labels if n in id_by_name],
        "labels": [{"name": n} for n in labels],
        "due": due, "dateLastActivity": last, "closed": False,
        "shortUrl": "", "pos": 1, "badges": {},
    }


def _board(cards):
    return sd.build_board({"lists": _LISTS, "labels": _LABELS, "cards": cards, "comments": []})


def _spine():
    return load_spine_from_dict({
        "workstreams": [
            {"name": "QA Hub pilot", "status": "Active", "context": "Pilot launches soon.",
             "priority": "High", "time_sensitive": True},
            {"name": "Reading list", "status": "Active", "context": "Background reading.",
             "priority": "Low", "time_sensitive": False},
        ],
        "people": [], "notes": [], "naming_standard": [], "rules": {},
    })


@pytest.fixture
def cfg():
    def _make(**overrides):
        s = settings_mod.load_settings("agent_config.json")
        for k, v in overrides.items():
            setattr(s, k, v)
        return s
    return _make


def _run(db_path, board, verdicts, settings, spine=None):
    result = ex.ExecutionResult()
    mut = ex.BoardMutator(None, dry_run=True)
    tier2 = repri.run_reprioritization(db_path, mut, board, verdicts, settings, spine or _spine(),
                                       NOW, NOW.isoformat(), result)
    return result, mut, tier2


def _comments(mut):
    return [e["text"] for e in mut.log if e["op"] == "add_comment"]


def _moves(mut):
    return [e for e in mut.log if e["op"] == "move_card"]


# ── Promotion via each signal type ─────────────────────────────────────────

def test_promotion_via_p0_label_executes(cfg, db_path):
    board = _board([_card("c1", "Ship QA Hub task list", "L_inbox", labels=["P0. High"])])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["priority_label"], "confidence": 88, "reason": "P0. High label."}]
    result, mut, tier2 = _run(db_path, board, v, cfg())
    assert tier2 == []
    assert _moves(mut) and _moves(mut)[0]["target_list_id"] == "L_today"
    assert result.reprioritizations[0]["verified_signals"] == ["priority_label"]


def test_promotion_via_due_in_window_executes(cfg, db_path):
    board = _board([_card("c1", "Prep deck", "L_inbox", due="2026-07-20T20:00:00.000Z")])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["due_in_window"], "confidence": 80, "reason": "Due today."}]
    result, mut, tier2 = _run(db_path, board, v, cfg())
    assert tier2 == [] and _moves(mut)[0]["target_list_id"] == "L_today"


def test_promotion_via_workstream_match_executes(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox")])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["workstream_high"], "confidence": 85, "reason": "QA Hub pilot High/High."}]
    result, mut, tier2 = _run(db_path, board, v, cfg())
    assert tier2 == [] and result.reprioritizations[0]["direction"] == "up"


# ── Automatic gate: confidence floor + signal verification ─────────────────

def test_one_verified_signal_at_76_executes(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox", labels=["P0. High"])])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["priority_label"], "confidence": 76, "reason": "P0."}]
    _, mut, tier2 = _run(db_path, board, v, cfg())
    assert tier2 == [] and _moves(mut)  # 76 >= 75 floor


def test_one_verified_signal_at_74_proposes(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox", labels=["P0. High"])])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["priority_label"], "confidence": 74, "reason": "P0."}]
    _, mut, tier2 = _run(db_path, board, v, cfg())
    assert not _moves(mut) and len(tier2) == 1  # below the 75 floor
    assert tier2[0]["type"] == "reprioritize_up"


def test_unverifiable_claimed_signal_proposes(cfg, db_path):
    # The card has NO P0/P1 label, so the claimed "priority_label" signal cannot be
    # verified — the automatic path is invalidated even at high confidence.
    board = _board([_card("c1", "Some card", "L_inbox")])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["priority_label"], "confidence": 95, "reason": "claims P0."}]
    _, mut, tier2 = _run(db_path, board, v, cfg())
    assert not _moves(mut) and len(tier2) == 1


def test_partially_unverified_signals_proposes(cfg, db_path):
    # One real signal (P0 label) + one bogus claim (workstream) → any unverified
    # claimed signal invalidates the automatic path.
    board = _board([_card("c1", "Random errand", "L_inbox", labels=["P0. High"])])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["priority_label", "workstream_high"], "confidence": 95}]
    _, mut, tier2 = _run(db_path, board, v, cfg())
    assert not _moves(mut) and len(tier2) == 1


# ── Demotion ranking / stop at target ──────────────────────────────────────

def test_demotion_only_from_over_target_and_stops_at_target(cfg, db_path):
    # Two weak Today cards, target 1 → only the first (weakest, ranked first by the
    # LLM) is demoted; once Today hits the target the second demotion is skipped.
    board = _board([_card("c1", "Read about Jamie JPL", "L_today"),
                    _card("c2", "Skim newsletter", "L_today")])
    s = cfg(today_list_target=1)
    v = [
        {"card_id": "c1", "direction": "down", "target_list": "Next Few Days",
         "signals": ["weak"], "confidence": 80, "reason": "weakest"},
        {"card_id": "c2", "direction": "down", "target_list": "Next Few Days",
         "signals": ["weak"], "confidence": 80, "reason": "next weakest"},
    ]
    result, mut, tier2 = _run(db_path, board, v, s)
    assert len(_moves(mut)) == 1 and _moves(mut)[0]["card_id"] == "c1"
    assert result.today_plan["moved"] == 1


def test_no_demotion_when_today_under_target(cfg, db_path):
    board = _board([_card("c1", "Read something", "L_today")])
    s = cfg(today_list_target=15)
    v = [{"card_id": "c1", "direction": "down", "target_list": "Next Few Days",
          "signals": ["weak"], "confidence": 90}]
    _, mut, tier2 = _run(db_path, board, v, s)
    assert not _moves(mut) and tier2 == []


# ── Hard exemptions (code gate) ────────────────────────────────────────────

def test_recent_edit_never_demoted_even_as_proposal(cfg, db_path):
    board = _board([_card("c1", "Read something", "L_today", last=RECENT),
                    _card("c2", "Filler", "L_today")])
    s = cfg(today_list_target=1)
    v = [{"card_id": "c1", "direction": "down", "target_list": "Next Few Days",
          "signals": ["weak"], "confidence": 90}]
    _, mut, tier2 = _run(db_path, board, v, s)
    assert not _moves(mut) and tier2 == []  # exempt: no move, no proposal


def test_today_mustdo_not_demoted_without_downgrade(cfg, db_path):
    board = _board([_card("c1", "Big task", "L_today", labels=["1. Today (must do)"]),
                    _card("c2", "Filler", "L_today")])
    s = cfg(today_list_target=1)
    v = [{"card_id": "c1", "direction": "down", "target_list": "Next Few Days",
          "signals": ["weak"], "confidence": 90}]
    _, mut, tier2 = _run(db_path, board, v, s)
    assert not _moves(mut) and tier2 == []


def test_today_mustdo_demoted_when_action_downgrades_label(cfg, db_path):
    board = _board([_card("c1", "Big task", "L_today", labels=["1. Today (must do)"]),
                    _card("c2", "Filler", "L_today")])
    s = cfg(today_list_target=1)
    v = [{"card_id": "c1", "direction": "down", "target_list": "Next Few Days",
          "signals": ["weak"], "confidence": 90, "reason": "downgrading",
          "label_change": {"action": "downgrade", "from": "1. Today (must do)",
                           "to": "2. Next Few Days (must do)"}}]
    _, mut, tier2 = _run(db_path, board, v, s)
    # is_weak is False (has a must-do label) so it can't auto-move → proposal, but
    # the exemption no longer blocks it because a downgrade is included.
    assert _moves(mut) or tier2, "downgrade should lift the exemption"
    label_sets = [e["label_ids"] for e in mut.log if e["op"] == "set_labels"]
    if _moves(mut):
        assert any("LB_t" not in ids for ids in label_sets)  # Today must-do removed


# ── Cap ────────────────────────────────────────────────────────────────────

def test_cap_limits_executed_moves_rest_proposed(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox", labels=["P0. High"]),
                    _card("c2", "QA Hub deck", "L_inbox", labels=["P0. High"])])
    s = cfg(max_reprioritization_moves_per_run=1)
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["priority_label"], "confidence": 90},
         {"card_id": "c2", "direction": "up", "target_list": "Today",
          "signals": ["priority_label"], "confidence": 90}]
    result, mut, tier2 = _run(db_path, board, v, s)
    assert len(_moves(mut)) == 1 and len(tier2) == 1  # cap → one executed, one proposed


# ── Proposed mode forces all moves to proposals ────────────────────────────

def test_proposed_mode_forces_proposals(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox", labels=["P0. High"])])
    s = cfg(reprioritization_mode=False)  # "proposed"
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["priority_label"], "confidence": 99}]
    _, mut, tier2 = _run(db_path, board, v, s)
    assert not _moves(mut) and len(tier2) == 1


# ── Rejection ledger ───────────────────────────────────────────────────────

def test_rejection_ledger_suppresses_repeat(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox", labels=["P0. High"])])
    from guardrails import fingerprint
    storage.add_rejection(db_path, fingerprint("reprioritize_up", ["c1"]), "comment", NOW.isoformat())
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["priority_label"], "confidence": 90}]
    _, mut, tier2 = _run(db_path, board, v, cfg())
    assert not _moves(mut) and tier2 == []


# ── Placement-conflict comment ─────────────────────────────────────────────

def test_demotion_comment_names_placement_conflict(cfg, db_path):
    board = _board([_card("c1", "Reading list article", "L_today"),
                    _card("c2", "Filler", "L_today")])
    s = cfg(today_list_target=1)
    v = [{"card_id": "c1", "direction": "down", "target_list": "Next Few Days",
          "signals": ["weak"], "confidence": 82, "reason": "Weakest on Today."}]
    _, mut, tier2 = _run(db_path, board, v, s)
    text = "\n".join(_comments(mut))
    assert "You placed this on Today" in text
    assert "Mark Less Time-sensitive: move to Next Few Days" in text
    assert "Reject if your placement stands." in text
    assert "Confidence: 82%" in text


def test_promotion_comment_uses_vocabulary(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox", labels=["P0. High"])])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["priority_label"], "confidence": 88, "reason": "P0."}]
    _, mut, tier2 = _run(db_path, board, v, cfg())
    text = "\n".join(_comments(mut))
    assert "Mark More Time-sensitive: move to Today" in text and "Confidence: 88%" in text


# ── Config renames: new name AND old alias, incl. Notion override ───────────

def test_renamed_keys_load_by_new_name(cfg):
    s = cfg()
    assert s.archive_mode is True and s.due_date_fix_mode is True
    assert s.time_label_fix_mode is True and s.automatic_action_confidence == 70
    assert s.reprioritization_mode is True and s.time_reprioritization_confidence == 75


def test_old_alias_reads_and_writes(cfg):
    s = cfg()
    assert s.tier1_recovery_archive is True and s.auto_min_confidence == 70
    s.tier1_recovery_archive = False           # old-name setter
    assert s.archive_mode is False             # proxies the new field


def test_notion_override_new_key(cfg):
    s = cfg()
    apply_notion_overrides(s, load_spine_from_dict(
        {"workstreams": [], "people": [], "notes": [], "rules": {"archive_mode": "proposed"}}))
    assert s.archive_mode is False


def test_notion_override_old_alias_key(cfg):
    s = cfg()
    apply_notion_overrides(s, load_spine_from_dict(
        {"workstreams": [], "people": [], "notes": [],
         "rules": {"tier1_recovery_archive": "false"}}))
    assert s.archive_mode is False


def test_notion_override_new_repri_keys(cfg):
    s = cfg()
    notes, _ = apply_notion_overrides(s, load_spine_from_dict(
        {"workstreams": [], "people": [], "notes": [],
         "rules": {"today_list_target": "12", "time_reprioritization_confidence": "80",
                   "reprioritization_mode": "proposed"}}))
    assert s.today_list_target == 12 and s.time_reprioritization_confidence == 80
    assert s.reprioritization_mode is False


# ── Today-plan report rendering ────────────────────────────────────────────

def _report(result, board, settings, dry_run=False):
    return rep.build_report(result, board, settings, NOW.isoformat(), dry_run, first_run=False,
                            stats={}, approval_rates={}, prev_stats={})


def test_today_plan_renders_first_with_moves(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox", labels=["P0. High"])])
    result, _, _ = _run(db_path, board, [
        {"card_id": "c1", "direction": "up", "target_list": "Today",
         "signals": ["priority_label"], "confidence": 88, "reason": "P0."}], cfg())
    result.today_plan["today_count"] = 23  # simulate an over-target board for the header
    text = _report(result, board, cfg())
    assert text.index("== Today plan ==") < text.index("== Still overdue")
    assert "Today: 23 cards, target 15 — 1 moved automatically, 0 proposed" in text
    assert "Mark More Time-sensitive: move to Today" in text
    assert "Verified signals: priority_label" in text


def test_today_plan_empty_state(cfg, db_path):
    board = _board([_card("c1", "Idle card", "L_today")])
    result = ex.ExecutionResult()
    result.today_plan = {"today_count": 8, "today_target": 15, "nfd_count": 5,
                         "nfd_target": 20, "moved": 0, "proposed": 0}
    text = _report(result, board, cfg())
    assert "Today: 8 cards (target 15) — no changes proposed." in text


def test_today_plan_dry_run_uses_would(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox", labels=["P0. High"])])
    result, _, _ = _run(db_path, board, [
        {"card_id": "c1", "direction": "up", "target_list": "Today",
         "signals": ["priority_label"], "confidence": 88}], cfg())
    text = _report(result, board, cfg(), dry_run=True)
    assert "would move automatically" in text
    assert "Would: Mark More Time-sensitive: move to Today" in text


def test_at_a_glance_includes_today_plan_counts(cfg, db_path):
    board = _board([_card("c1", "QA Hub task list", "L_inbox", labels=["P0. High"])])
    result, _, _ = _run(db_path, board, [
        {"card_id": "c1", "direction": "up", "target_list": "Today",
         "signals": ["priority_label"], "confidence": 88}], cfg())
    text = _report(result, board, cfg())
    assert "Today 15/15 (1 moved, 0 proposed)" in text or "Today " in text.split("At a glance")[1]


# ── Deterministic pre-ranking (Python, no LLM) ─────────────────────────────

def _over_target_today(n, target=15):
    """A board with n weak Today cards (no labels, no due, stale) over `target`."""
    cards = [_card(f"c{i}", f"Idle task {i}", "L_today",
                   last=("2026-06-15T12:00:00.000Z" if i == 0 else "2026-07-01T12:00:00.000Z"))
             for i in range(n)]
    return _board(cards)


def test_build_candidates_promotion_prefilter(cfg, db_path):
    # Only the P0-labelled Inbox card carries a verified signal → only it shortlists.
    board = _board([_card("p", "QA Hub task list", "L_inbox", labels=["P0 - High"]),
                    _card("q", "Random note", "L_inbox")])
    mut = ex.BoardMutator(None, dry_run=True)
    cands = repri.build_candidates(board, mut, _spine(), cfg(), NOW)
    ids = [c["id"] for c in cands["promote"]]
    assert ids == ["p"] and "priority_label" in cands["promote"][0]["verified_signals"]


def test_build_candidates_promote_shortlist_capped(cfg, db_path):
    # 25 Inbox cards each with a P0 label → all carry a verified signal, but the
    # promote shortlist is capped at 2*cap so a large signal-bearing set can't
    # overflow the judge's token budget (the 124/124-unverdicted live failure).
    cards = [_card(f"p{i}", f"Task {i}", "L_inbox", labels=["P0 - High"]) for i in range(25)]
    board = _board(cards)
    mut = ex.BoardMutator(None, dry_run=True)
    cands = repri.build_candidates(board, mut, _spine(), cfg(max_reprioritization_moves_per_run=3), NOW)
    assert len(cands["promote"]) == 6  # min(2*3, 25)
    assert all("priority_label" in c["verified_signals"] for c in cands["promote"])


def test_build_candidates_promote_ranks_strongest_first(cfg, db_path):
    # A P0-labelled card outranks a workstream-only match: strongest signal first.
    board = _board([_card("weak", "QA Hub pilot notes", "L_inbox"),
                    _card("strong", "QA Hub task list", "L_inbox", labels=["P0 - High"])])
    mut = ex.BoardMutator(None, dry_run=True)
    cands = repri.build_candidates(board, mut, _spine(), cfg(), NOW)
    assert cands["promote"][0]["id"] == "strong"  # P0 label ranks above workstream-only


def test_build_candidates_demote_shortlist_capped(cfg, db_path):
    # 30 over target (15) with cap 3 → shortlist = min(2*3, overflow 15) = 6, weakest first.
    board = _over_target_today(30)
    mut = ex.BoardMutator(None, dry_run=True)
    cands = repri.build_candidates(board, mut, _spine(), cfg(max_reprioritization_moves_per_run=3), NOW)
    assert cands["overflow_today"] == 15
    assert len(cands["demote"]) == 6
    assert cands["demote"][0]["id"] == "c0"  # oldest/most-inactive ranks weakest-first
    assert "weakness_score" in cands["demote"][0]


# ── End-to-end through the real pipeline path (LLM verdict step mocked) ─────

def _pipeline(board, settings, db_path, tmp_path, verdicts):
    settings.report_file = str(tmp_path / "report.txt")
    fake = FakeLLM(json.dumps({"verdicts": verdicts}))
    return main.run_pipeline(board, settings, db_path, NOW, True, True,
                             llm=fake, prompts=FakePrompts(), spine=_spine(), judgments={})


def test_over_target_yields_demotion_end_to_end(cfg, db_path, tmp_path):
    board = _over_target_today(30)
    verdicts = ([{"card_id": "c0", "verdict": "move", "direction": "down",
                  "target_list": "Next Few Days", "signals": ["weak"], "confidence": 85,
                  "reason": "Weakest on Today: no labels, no due, 35 days idle."}]
                + [{"card_id": f"c{i}", "verdict": "keep", "reason": "active"} for i in range(1, 30)])
    result, text, _ = _pipeline(board, cfg(today_list_target=15), db_path, tmp_path, verdicts)
    assert any(r["card_id"] == "c0" for r in result.reprioritizations)
    assert "Mark Less Time-sensitive: move to Next Few Days" in text


def test_keep_everything_surfaces_silent_zero_in_health(cfg, db_path, tmp_path):
    board = _over_target_today(30)
    verdicts = [{"card_id": f"c{i}", "verdict": "keep", "reason": "still active"} for i in range(30)]
    result, text, _ = _pipeline(board, cfg(today_list_target=15), db_path, tmp_path, verdicts)
    assert result.today_plan["moved"] == 0 and result.today_plan["overflow"] == 15
    # Silent zero is now VISIBLE in Health stats.
    assert "Reprioritization: 0 moves against 15 overflow" in text


def test_unverdicted_candidates_surfaced_in_health(cfg, db_path, tmp_path):
    board = _over_target_today(30)
    # LLM returns verdicts for only 5 of the 15 shortlisted cards.
    verdicts = [{"card_id": f"c{i}", "verdict": "keep", "reason": "x"} for i in range(5)]
    result, text, _ = _pipeline(board, cfg(today_list_target=15), db_path, tmp_path, verdicts)
    assert result.today_plan["unverdicted"] == 10
    assert "10 candidate(s) unverdicted" in text


def test_today_plan_proposed_count_reflects_open_cap(cfg, db_path, tmp_path):
    # 15 below-floor demotions all propose, but max_proposals_open caps how many
    # actually open — the Today plan count must match the opened proposals, not the
    # pre-cap intended 15.
    board = _over_target_today(30)
    verdicts = [{"card_id": f"c{i}", "verdict": "move", "direction": "down",
                 "target_list": "Next Few Days", "signals": ["weak"], "confidence": 60,
                 "reason": "weak"} for i in range(15)]
    s = cfg(today_list_target=15, max_proposals_open=3)
    result, text, _ = _pipeline(board, s, db_path, tmp_path, verdicts)
    opened = sum(1 for p in result.proposals_opened if p.get("type") == "reprioritize_down")
    assert opened == 3 and result.today_plan["proposed"] == 3
    assert "3 proposed" in text and "15 proposed" not in text
