"""Section 1 — snapshot & diff, implicit-rejection detection."""

import copy

import storage
from models import Card
from phases import snapshot_diff as sd


def _seed_prev(db_path, cards, actions=()):
    storage.save_snapshot(db_path, "prev", "2026-07-10T06:30:00+00:00", cards)
    for a in actions:
        storage.record_action(db_path, "prev", "2026-07-10T06:30:00+00:00", 1,
                              a["type"], a["card_ids"], a.get("payload", {}), "success")


def _card(board, cid, **over):
    c = copy.copy(board.card_by_id(cid))
    for k, v in over.items():
        setattr(c, k, v)
    return c


def test_diff_user_edited_touched_card_records_rejection(board, settings, db_path):
    # Prev run: agent renamed auto_edited to "Agent renamed X" and applied the label.
    prev_card = _card(board, "auto_edited", name="Agent renamed X",
                      label_names=[settings.label_auto_updated])
    _seed_prev(db_path, [prev_card],
               actions=[{"type": "rename", "card_ids": ["auto_edited"],
                         "payload": {"new_name": "Agent renamed X"}}])
    prev = storage.get_snapshot(db_path, "prev")
    actions = storage.get_actions(db_path, run_id="prev")
    quar = board.list_by_name(settings.archive_list_name).id

    rejections = sd.detect_implicit_rejections(prev, board, actions, settings, quar)
    rej = [r for r in rejections if r["card_id"] == "auto_edited"]
    assert rej and rej[0]["source"] == "edit"
    assert rej[0]["remove_label"] is True  # label still present → strip it


def test_diff_manual_label_removal_treated_as_rejection(board, settings, db_path, fresh_board):
    # Prev: auto_old had the label; board now has it removed, name/desc unchanged.
    prev_card = _card(board, "auto_old", label_names=[settings.label_auto_updated])
    _seed_prev(db_path, [prev_card],
               actions=[{"type": "rename", "card_ids": ["auto_old"],
                         "payload": {"new_name": "Old auto card"}}])

    def drop_label(data):
        for c in data["cards"]:
            if c["id"] == "auto_old":
                c["idLabels"] = []
                c["labels"] = []
    board2 = fresh_board(drop_label)
    prev = storage.get_snapshot(db_path, "prev")
    actions = storage.get_actions(db_path, run_id="prev")
    quar = board2.list_by_name(settings.archive_list_name).id

    rejections = sd.detect_implicit_rejections(prev, board2, actions, settings, quar)
    assert any(r["card_id"] == "auto_old" and r["source"] == "label-removal" for r in rejections)


def test_diff_archive_pullback_records_rejection(board, settings, db_path):
    # Prev merge: survivor dup_exact_a (unchanged), loser arch_pullback parked in the archive list.
    quar = board.list_by_name(settings.archive_list_name).id
    prev_survivor = _card(board, "dup_exact_a")
    prev_loser = _card(board, "arch_pullback", list_id=quar)
    _seed_prev(db_path, [prev_survivor, prev_loser],
               actions=[{"type": "merge", "card_ids": ["dup_exact_a", "arch_pullback"],
                         "payload": {"survivor_id": "dup_exact_a", "loser_ids": ["arch_pullback"]}}])
    prev = storage.get_snapshot(db_path, "prev")
    actions = storage.get_actions(db_path, run_id="prev")

    # Board has arch_pullback in L_today (dragged back out of the archive list).
    rejections = sd.detect_implicit_rejections(prev, board, actions, settings, quar)
    assert any(r["fingerprint"].startswith("merge|") for r in rejections)


def test_diff_unchanged_touched_card_no_rejection(board, settings, db_path):
    prev_card = _card(board, "auto_fresh", label_names=[settings.label_auto_updated])
    _seed_prev(db_path, [prev_card],
               actions=[{"type": "rename", "card_ids": ["auto_fresh"],
                         "payload": {"new_name": "Fresh auto card"}}])
    prev = storage.get_snapshot(db_path, "prev")
    actions = storage.get_actions(db_path, run_id="prev")
    quar = board.list_by_name(settings.archive_list_name).id

    rejections = sd.detect_implicit_rejections(prev, board, actions, settings, quar)
    assert all(r["card_id"] != "auto_fresh" for r in rejections)


def test_diff_new_scratch_list_added_to_recovery_scope(board, settings):
    names = [l.name for l in sd.recovery_source_lists(board, settings)]
    assert "Scratch 7-1" in names  # newest sweep auto-included
    assert "ARCHIVE Scratch 5-12" not in names
    assert "Archive - April 12" not in names


def test_diff_recovery_sources_ordered_newest_first(board, settings):
    names = [l.name for l in sd.recovery_source_lists(board, settings)]
    assert names[:3] == ["Scratch 7-1", "Scratch 6-24", "Scratch 6-3"]
