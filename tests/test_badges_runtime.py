"""
Forced-Tier-2 merge rule verified through the REAL runtime parsing path:
raw Trello card dict -> agent_shared _parse_card -> card_from_trello -> Card,
then _merge_action_facts + assign_tier. Not fixture badges.
"""

import guardrails as g
from agent_shared.trello.client import _parse_card
from models import BoardView, ListInfo
from phases import execute as ex
from phases.snapshot_diff import card_from_trello


def _raw(cid, name, list_id, attachments=0, check_items=0):
    return {"id": cid, "name": name, "idList": list_id, "desc": "body",
            "badges": {"attachments": attachments, "checkItems": check_items},
            "labels": [], "dateLastActivity": "2026-06-20T12:00:00.000Z"}


def _board_from_raw(raw_cards):
    board = BoardView(lists=[ListInfo(id="L_today", name="Today")], labels=[])
    for rc in raw_cards:
        tc = _parse_card(rc)  # shared-library parse (real path)
        board.cards.append(card_from_trello(tc, "Today"))
    return board


def test_card_from_trello_maps_badges_from_real_parse():
    tc = _parse_card(_raw("c1", "Card", "L_today", attachments=2, check_items=3))
    card = card_from_trello(tc, "Today")
    assert card.has_attachments is True
    assert card.has_checklist is True


def test_merge_loser_attachment_forces_tier2_via_runtime_path(board, settings):
    b = _board_from_raw([
        _raw("survivor", "Weekend referral strategy notes", "L_today"),
        _raw("loser", "Weekend referral strategy notes", "L_today", attachments=2),
    ])
    verdict = {"survivor_id": "survivor", "cluster_ids": ["survivor", "loser"], "llm_tier": 1}
    facts = ex._merge_action_facts(verdict, b, settings, {"L_today"})
    assert facts["loser_has_attachment"] is True
    assert g.assign_tier(facts, settings) == 2


def test_merge_loser_checklist_forces_tier2_via_runtime_path(board, settings):
    b = _board_from_raw([
        _raw("survivor", "Onboarding checklist for new hire", "L_today"),
        _raw("loser", "Onboarding checklist for new hire", "L_today", check_items=4),
    ])
    verdict = {"survivor_id": "survivor", "cluster_ids": ["survivor", "loser"], "llm_tier": 1}
    facts = ex._merge_action_facts(verdict, b, settings, {"L_today"})
    assert facts["loser_has_checklist"] is True
    assert g.assign_tier(facts, settings) == 2


def test_merge_no_badges_not_forced_by_attachment(board, settings):
    b = _board_from_raw([
        _raw("survivor", "Plain duplicate task here", "L_today"),
        _raw("loser", "Plain duplicate task here", "L_today"),
    ])
    verdict = {"survivor_id": "survivor", "cluster_ids": ["survivor", "loser"], "llm_tier": 1}
    facts = ex._merge_action_facts(verdict, b, settings, {"L_today"})
    assert facts["loser_has_attachment"] is False
    assert facts["loser_has_checklist"] is False
