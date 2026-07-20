"""
July-19 report-review update: expanded signals, judgment fixes, 3-bullet comments,
and the limited-live-test run mode. See the spine's "How this works" / Problem 5 /
Notes / Rules sections.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import settings as settings_mod
import signals as sig
import storage
from phases import candidates as cand
from phases import execute as ex
from phases import report as rep
from phases import reprioritize as repri
from phases import snapshot_diff as sd
from spine import apply_notion_overrides, load_spine_from_dict

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-07-01T12:00:00.000Z"

_LISTS = [
    {"id": "L_today", "name": "Today", "pos": 1},
    {"id": "L_inbox", "name": "Inbox / Triage", "pos": 2},
    {"id": "L_nfd", "name": "Next Few Days", "pos": 3},
    {"id": "L_week", "name": "This Week", "pos": 4},
    {"id": "L_arch", "name": "Agent Archive", "pos": 5},
    {"id": "L_bkgm", "name": "Backlog - GM", "pos": 6},
]
_LABELS = [
    {"id": "LB_t", "name": "1. Today (must do)"},
    {"id": "LB_n", "name": "2. Next Few Days (must do)"},
    {"id": "LB_auto", "name": "Agent: Auto-Updated"},
    {"id": "LB_prop", "name": "Agent: Proposed"},
    {"id": "LB_p0", "name": "P0 - High"},
    {"id": "LB_cd", "name": "Career Development"},
]


def _card(cid, name, list_id, labels=None, due=None, desc="", last=OLD):
    labels = labels or []
    idb = {lb["name"]: lb["id"] for lb in _LABELS}
    return {"id": cid, "name": name, "desc": desc, "idList": list_id,
            "idLabels": [idb[n] for n in labels if n in idb], "labels": [{"name": n} for n in labels],
            "due": due, "dateLastActivity": last, "closed": False, "shortUrl": "", "pos": 1, "badges": {}}


def _board(cards):
    return sd.build_board({"lists": _LISTS, "labels": _LABELS, "cards": cards, "comments": []})


def _spine():
    return load_spine_from_dict({
        "workstreams": [
            {"name": "GM onboarding", "status": "Active", "context": "Ramp new GMs.",
             "priority": "High", "time_sensitive": True},
            {"name": "Sales Summit", "status": "Complete", "context": "Event finished."},
        ],
        "people": [
            {"name": "Logan", "role": "co-CEO",
             "context": "Generally action items involving Logan are more important and time-sensitive."},
            {"name": "Marah", "role": "Clinical", "context": "Outcomes dashboard."},
        ],
        "notes": [], "naming_standard": [], "rules": {},
    })


@pytest.fixture
def cfg():
    def _make(**overrides):
        s = settings_mod.load_settings("agent_config.json")
        for k, v in overrides.items():
            setattr(s, k, v)
        return s
    return _make


def _run_repri(db_path, board, verdicts, settings, mut=None):
    result = ex.ExecutionResult()
    mut = mut or ex.BoardMutator(None, dry_run=True)
    tier2 = repri.run_reprioritization(db_path, mut, board, verdicts, settings, _spine(),
                                       NOW, NOW.isoformat(), result)
    return result, mut, tier2


def _moves(mut):
    return [e for e in mut.log if e["op"] == "move_card"]


class _FakeTrello:
    """No-op Trello client so limited_test 'real' actions don't hit the network."""
    def update_card(self, *a, **k): return {}
    def move_card(self, *a, **k): return {}
    def add_comment(self, *a, **k): return {}
    def create_card(self, *a, **k): return {"id": "new", "url": ""}
    def create_list(self, *a, **k): return type("L", (), {"id": "new", "name": "x", "position": 1})()


# ── People-section priority parsing ────────────────────────────────────────

def test_people_priority_parsed_from_context_cue(cfg):
    s = _spine()
    assert "Logan" in s.priority_person_names()
    assert "Marah" not in s.priority_person_names()  # no priority cue


def test_spine_people_priority_signal_verifies_and_promotes(cfg, db_path):
    board = _board([_card("c1", "Send Logan the RSM comp update", "L_inbox")])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["spine_people_priority"], "confidence": 80, "reason": "Logan ask."}]
    result, mut, tier2 = _run_repri(db_path, board, v, cfg())
    assert _moves(mut) and result.reprioritizations[0]["verified_signals"] == ["spine_people_priority"]
    # value rendered in the comment
    assert any("spine_people_priority: Logan" in e["text"] for e in mut.log if e["op"] == "add_comment")


# ── Staleness veto blocks promotion ────────────────────────────────────────

def test_high_staleness_vetoes_promotion(cfg, db_path):
    # Old source-meeting date → high staleness; a P0 label would otherwise promote.
    board = _board([_card("c1", "Model comp scenarios", "L_inbox", labels=["P0 - High"],
                          desc="**Date:** 2026-05-01\nOne-off analysis.")])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["existing_label_priority"], "confidence": 95}]
    result, mut, tier2 = _run_repri(db_path, board, v, cfg())
    assert not _moves(mut)                       # promotion vetoed
    assert any(t["card_ids"] == ["c1"] for t in tier2)  # routed to archive/backlog


# ── Complete-workstream match blocks promotion, routes to archive ──────────

def test_complete_workstream_never_promoted_routes_to_archive(cfg, db_path):
    board = _board([_card("c1", "Sales Summit recap deck", "L_inbox", labels=["P0 - High"])])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["existing_label_priority"], "confidence": 95}]
    result, mut, tier2 = _run_repri(db_path, board, v, cfg())
    assert not _moves(mut)
    assert any(t["type"] == "inscope_archive" and t["card_ids"] == ["c1"] for t in tier2)


# ── implied_task_urgency never the sole verified signal ────────────────────

def test_implied_task_urgency_alone_proposes(cfg, db_path):
    board = _board([_card("c1", "Increase staffing update frequency", "L_inbox")])
    v = [{"card_id": "c1", "direction": "up", "target_list": "Today",
          "signals": ["implied_task_urgency"], "confidence": 95,
          "implied_value": "increasing update frequency"}]
    result, mut, tier2 = _run_repri(db_path, board, v, cfg())
    assert not _moves(mut) and any(t["type"] == "reprioritize_up" for t in tier2)


# ── Move to Backlog matching ───────────────────────────────────────────────

def test_backlog_routes_to_existing_matching_list(cfg, db_path):
    board = _board([_card("c1", "GM onboarding playbook polish", "L_inbox", labels=["P0 - High"],
                          desc="**Date:** 2026-05-01\nStrategic but stale.")])
    result, mut, tier2 = _run_repri(db_path, board, [], cfg())
    backlog = [t for t in tier2 if t["type"] == "backlog"]
    assert backlog and backlog[0]["dest_name"] == "Backlog - GM" and backlog[0]["suggest_create"] is False


def test_backlog_suggests_create_when_no_list(cfg, db_path):
    board = _board([_card("c1", "GM onboarding playbook polish", "L_inbox", labels=["P0 - High"],
                          desc="**Date:** 2026-05-01\nStrategic but stale.")])
    # Board without the Backlog - GM list.
    raw = {"lists": [l for l in _LISTS if l["id"] != "L_bkgm"], "labels": _LABELS,
           "cards": [_card("c1", "GM onboarding playbook polish", "L_inbox", labels=["P0 - High"],
                           desc="**Date:** 2026-05-01\nStrategic but stale.")], "comments": []}
    board = sd.build_board(raw)
    result, mut, tier2 = _run_repri(db_path, board, [], cfg())
    backlog = [t for t in tier2 if t["type"] == "backlog"]
    assert backlog and backlog[0]["dest_name"] is None and backlog[0]["suggest_create"] is True


# ── Description / source-meeting based dedup candidate ─────────────────────

def test_description_based_dedup_links_same_meeting(cfg):
    sm = "**Source meeting:** 7/13 Priscilla midyear feedback (Cody)"
    a = _card("a", "Circle back with Cody after Priscilla conversation", "L_today", desc=sm + "\nfollow up")
    b = _card("b", "• Cody - circle back", "L_today", desc=sm + "\nsame")
    board = _board([a, b])
    incards = [board.card_by_id("a"), board.card_by_id("b")]
    hints = cand.narrow_track(incards, set(), cfg())["hints"]
    assert any(set(h) == {"a", "b"} for h in hints)  # linked despite different titles


# ── Reflection-card protection ─────────────────────────────────────────────

def test_reflection_card_not_promoted_or_routed(cfg, db_path):
    board = _board([_card("c1", "Reflection - week of 7/14", "L_inbox", labels=["P0 - High"])])
    result, mut, tier2 = _run_repri(db_path, board, [
        {"card_id": "c1", "direction": "up", "target_list": "Today",
         "signals": ["existing_label_priority"], "confidence": 95}], cfg())
    assert not _moves(mut) and tier2 == []  # vetoed as reflection, never routed


def test_reflection_card_not_archived(cfg, db_path):
    board = _board([_card("c1", "Reflection - quarterly", "L_today", labels=["Career Development"])])
    result = ex.ExecutionResult()
    mut = ex.BoardMutator(None, dry_run=True)
    tier2, archived = ex.execute_inscope_archive(
        db_path, mut, board, [{"card_id": "c1", "confidence": 95, "reason": "old"}],
        cfg(), NOW, NOW.isoformat(), result)
    assert archived == set() and tier2 == []  # protected from archive


# ── 3-bullet card-comment format for each action type ──────────────────────

def _is_three_bullet(text: str) -> bool:
    bullets = [ln for ln in text.split("\n") if ln.startswith("- ")]
    return len(bullets) == 3 and bullets[0].startswith("- Input signals:") and bullets[1].startswith("- [")


def test_reprioritization_comment_is_three_bullets(cfg, db_path):
    board = _board([_card("c1", "Send Logan the update", "L_inbox")])
    result, mut, _ = _run_repri(db_path, board, [
        {"card_id": "c1", "direction": "up", "target_list": "Today",
         "signals": ["spine_people_priority"], "confidence": 88}], cfg())
    comment = [e["text"] for e in mut.log if e["op"] == "add_comment"][0]
    assert _is_three_bullet(comment) and "[Increase Time-Sensitivity]" in comment


def test_action_comments_are_three_bullets(cfg):
    # rename, due-clear, label-remove, archive each produce a 3-bullet comment.
    from vocab import (ACTION_ARCHIVE, ACTION_FIX_DUE, ACTION_RENAME, ACTION_TIME_LABEL)
    r = ex._action_comment(ACTION_RENAME, [("name_quality", "unclear")], "Cleaned.",
                           confidence=80, from_to="from 'a' to 'b'")
    d = ex._action_comment(ACTION_FIX_DUE, [("due_status", "dead")], "Cleared.", confidence=75)
    l = ex._action_comment(ACTION_TIME_LABEL, [("time_label", "stale")], "Removed.",
                           confidence=90, from_to="remove 'x'")
    a = ex._action_comment(ACTION_ARCHIVE, [("assessment", "done")], "Gone.", confidence=70)
    for c in (r, d, l, a):
        assert _is_three_bullet(c)


# ── Limited-live-test mode ─────────────────────────────────────────────────

def test_limited_test_executes_exactly_n_per_type(cfg, db_path):
    cards = [_card(f"a{i}", f"Old errand {i}", "L_today") for i in range(4)]
    board = _board(cards)
    s = cfg(max_inscope_archives_per_run=10)
    mut = ex.BoardMutator(_FakeTrello(), run_mode="limited_test", limited_per_type=2)
    result = ex.ExecutionResult()
    verdicts = [{"card_id": f"a{i}", "confidence": 90 - i, "reason": "gone"} for i in range(4)]
    ex.execute_inscope_archive(db_path, mut, board, verdicts, s, NOW, NOW.isoformat(), result)
    archives = [a for a in result.applied if a["type"] == "inscope_archive"]
    assert sum(1 for a in archives if a["real"]) == 2      # exactly N real
    assert sum(1 for a in archives if not a["real"]) == 2  # rest simulated
    # Persistence only for real actions.
    with storage.db_connection(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM actions WHERE action_type='inscope_archive'").fetchone()["n"]
    assert n == 2


def test_limited_test_real_moves_get_auto_label(cfg, db_path):
    cards = [_card(f"c{i}", f"Send Logan item {i}", "L_inbox") for i in range(3)]
    board = _board(cards)
    mut = ex.BoardMutator(_FakeTrello(), run_mode="limited_test", limited_per_type=2)
    result = ex.ExecutionResult()
    verdicts = [{"card_id": f"c{i}", "direction": "up", "target_list": "Today",
                 "signals": ["spine_people_priority"], "confidence": 90 - i} for i in range(3)]
    repri.run_reprioritization(db_path, mut, board, verdicts, cfg(), _spine(),
                               NOW, NOW.isoformat(), result)
    reals = [r for r in result.reprioritizations if r["real"]]
    assert len(reals) == 2  # exactly N real; the rest simulated
    assert sum(1 for r in result.reprioritizations if not r["real"]) == 1


def test_limited_test_report_tags_and_header(cfg, db_path):
    result = ex.ExecutionResult()
    result.run_mode = "limited_test"
    result.limited_per_type = 2
    result.applied.append({"type": "rename", "card_id": "x", "new_name": "New", "real": True})
    result.applied.append({"type": "rename", "card_id": "y", "new_name": "New2", "real": False})
    board = _board([_card("x", "old", "L_today"), _card("y", "old2", "L_today")])
    text = rep.build_report(result, board, cfg(), NOW.isoformat(), dry_run=False, first_run=False,
                            stats={}, approval_rates={}, prev_stats={})
    assert "Mode: LIMITED LIVE TEST — up to 2 real actions per type" in text
    assert "[REAL]" in text and "[simulated]" in text


# ── run_mode supersedes dry_run + mode-word rendering ──────────────────────

def test_run_mode_supersedes_dry_run():
    assert settings_mod._resolve_run_mode({"dry_run": True, "run_mode": "limited_test"}) == "limited_test"
    assert settings_mod._resolve_run_mode({"dry_run": True}) == "dry_run"
    assert settings_mod._resolve_run_mode({"dry_run": False}) == "live"


def test_notion_override_run_mode(cfg):
    s = cfg()
    apply_notion_overrides(s, load_spine_from_dict(
        {"workstreams": [], "people": [], "notes": [], "rules": {"run_mode": "limited_test"}}))
    assert s.run_mode == "limited_test"


def test_mode_word_rendering_in_notion_note(cfg):
    s = cfg()
    notes, _ = apply_notion_overrides(s, load_spine_from_dict(
        {"workstreams": [], "people": [], "notes": [], "rules": {"archive_mode": "proposed"}}))
    assert any("set to proposed" in n for n in notes) and not any("set to False" in n for n in notes)


def test_report_card_truncated_and_best_effort(cfg):
    board = _board([_card("x", "old", "L_today")])
    captured = {}

    class _CapMut:
        def create_card(self, list_id, name, text):
            captured["text"] = text
            return {"id": "r"}

    rep.publish_report_card(_CapMut(), board, cfg(), "y" * 25000)   # oversized
    assert len(captured["text"]) <= 16000 and "truncated" in captured["text"]

    class _RaiseMut:
        def create_card(self, *a):
            raise RuntimeError("400 Bad Request")

    rep.publish_report_card(_RaiseMut(), board, cfg(), "short")     # must NOT raise


# ── Trello text sanitizer (CRLF + control chars → 400 class) ───────────────

def test_sanitize_card_text_normalizes_crlf_and_strips_controls():
    assert ex.sanitize_card_text("a\r\nb\rc") == "a\nb\nc"
    assert ex.sanitize_card_text("x\x00\x07\x1fy") == "xy"           # C0 controls stripped
    assert ex.sanitize_card_text("keep\ttab\nand nl") == "keep\ttab\nand nl"  # tab/nl kept
    assert ex.sanitize_card_text("emdash — ok") == "emdash — ok"     # unicode preserved
    assert ex.sanitize_card_text(None) == ""


def test_mutator_sanitizes_comment_text():
    mut = ex.BoardMutator(None, dry_run=True)
    mut.add_comment("c", "line1\r\nline2\x07end")
    assert mut.log[-1]["text"] == "line1\nline2end"


# ── Condensed report card fits Trello's byte limit ─────────────────────────

def test_card_report_condensed_fits_and_summarizes(cfg):
    result = ex.ExecutionResult()
    result.run_mode = "limited_test"
    result.today_plan = {"today_count": 50, "today_target": 15, "nfd_count": 80,
                         "nfd_target": 20, "moved": 10, "proposed": 12, "overflow": 95,
                         "unverdicted": 0}
    for i in range(18):  # realistic (capped at max_proposals_open)
        result.proposals_opened.append({
            "proposal_id": i, "type": "reprioritize_up", "card_id": f"c{i}", "card_ids": [f"c{i}"],
            "title": f"Proposal {i} " + "x" * 40, "action_desc": "Increase Time-Sensitivity",
            "reason": "r" * 60, "confidence": 80, "dest_name": "Today"})
    for i in range(40):  # bulky sections that must be summarized, not inlined
        result.recently_archived.append({"card_id": f"a{i}", "name": "y" * 80, "reason": "z" * 80})
        result.applied.append({"type": "rename", "card_id": f"r{i}", "new_name": "n" * 60, "real": True})
    board = _board([_card("x", "old", "L_today")])
    text = rep.build_report(result, board, cfg(), NOW.isoformat(), dry_run=False, first_run=False,
                            stats={}, approval_rates={}, prev_stats={}, card=True)
    assert len(text.encode("utf-8")) <= 16384                        # fits Trello's byte limit
    assert "Full report:" in text                                    # pointer present
    assert "== Awaiting your decision (18) ==" in text               # proposals in full
    assert "== Recently archived (40) == (see full report)" in text  # summarized
    assert "== Done automatically" in text and "(see full report)" in text


def test_byte_truncate_respects_limit_and_marks():
    huge = "— " * 20000  # multibyte em-dashes
    out = rep._byte_truncate(huge, 16000)
    assert len(out.encode("utf-8")) <= 16000 and out.endswith("truncated — see the full report file")
    out.encode("utf-8")  # valid utf-8 (no split multibyte char)


# ── Idempotency: crash-after-execute must not duplicate a proposal ─────────

def test_open_proposal_not_duplicated_on_rerun(cfg, db_path):
    board = _board([_card("c1", "Some card", "L_today")])
    action = {"type": "inscope_archive", "card_ids": ["c1"], "anchor_card_id": "c1",
              "confidence": 65, "reason": "delegated"}
    # Run 1: open the proposal (records it + labels the card).
    r1 = ex.ExecutionResult()
    mut1 = ex.BoardMutator(_FakeTrello(), run_mode="live")
    ex.generate_proposals(db_path, mut1, board, [action], cfg(), NOW.isoformat(), r1)
    assert len(r1.proposals_opened) == 1
    # Run 2 (post-crash re-run): the same action must NOT create a second proposal.
    r2 = ex.ExecutionResult()
    mut2 = ex.BoardMutator(_FakeTrello(), run_mode="live")
    ex.generate_proposals(db_path, mut2, board, [action], cfg(), NOW.isoformat(), r2)
    assert r2.proposals_opened == []
    with storage.db_connection(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM proposals").fetchone()["n"]
    assert n == 1  # exactly one proposal row, not two


def test_health_stats_render_mode_words(cfg, db_path):
    result = ex.ExecutionResult()
    board = _board([_card("x", "old", "L_today")])
    text = rep.build_report(result, board, cfg(), NOW.isoformat(), dry_run=True, first_run=False,
                            stats={}, approval_rates={}, prev_stats={})
    assert "archive_mode=automatic" in text and "archive_mode=True" not in text
