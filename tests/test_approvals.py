"""Section 2 — comment-based approval parsing and proposal timeout."""

from unittest.mock import MagicMock

import storage
from phases import execute as ex
from phases import snapshot_diff as sd


def _add_open_proposal(db_path, card_id, action=None, opened="2026-07-10T00:00:00+00:00", fp=None):
    action = action or {"type": "merge", "card_ids": [card_id]}
    fp = fp or ("merge|" + card_id)
    return storage.add_proposal(db_path, "prev", fp, [card_id], action, "reason", opened)


def test_approval_comment_yes_executes_proposed_action(board, settings, db_path):
    action = {"type": "stale_label_removal", "card_ids": ["stale_label"], "label_id": "LB_t"}
    pid = _add_open_proposal(db_path, "stale_label", action, fp="stale_label_removal|stale_label")
    approvals = sd.parse_approvals(board, storage.get_open_proposals(db_path), settings)
    # prop for stale_label has no comment → force approve via prop_yes card instead:
    # Use prop_yes which has a "yes" comment.
    pid2 = _add_open_proposal(db_path, "prop_yes", action, fp="stale_label_removal|prop_yes")
    approvals = sd.parse_approvals(board, storage.get_open_proposals(db_path), settings)
    yes = [a for a in approvals if a["proposal"]["card_ids"] == ["prop_yes"]][0]
    assert yes["decision"] == "approve"

    trello = MagicMock()
    mutator = ex.BoardMutator(trello, dry_run=False)
    result = ex.ExecutionResult()
    ex.process_approvals(db_path, mutator, board, [yes], settings, None, "2026-07-11T13:00:00+00:00", result)
    assert trello.update_card.called  # label removal executed
    statuses = {p["proposal_id"]: p for p in _all_proposals(db_path)}
    assert statuses[yes["proposal"]["proposal_id"]]["status"] == "approved"


def test_approval_comment_approve_executes(board, settings, db_path, fresh_board):
    def set_approve(data):
        data["comments"].append({"id": "c", "idCard": "prop_no", "text": "Approve",
                                 "date": "2026-07-11T09:00:00.000Z", "memberName": "V"})
        # remove the existing "no" so approve wins
        data["comments"] = [c for c in data["comments"] if c["id"] != "cm_no"]
    b = fresh_board(set_approve)
    _add_open_proposal(db_path, "prop_no")
    approvals = sd.parse_approvals(b, storage.get_open_proposals(db_path), settings)
    assert approvals[0]["decision"] == "approve"


def test_approval_comment_no_rejects(board, settings, db_path):
    _add_open_proposal(db_path, "prop_no")
    approvals = sd.parse_approvals(board, storage.get_open_proposals(db_path), settings)
    entry = approvals[0]
    assert entry["decision"] == "reject"
    trello = MagicMock()
    mutator = ex.BoardMutator(trello, dry_run=False)
    result = ex.ExecutionResult()
    ex.process_approvals(db_path, mutator, board, [entry], settings, None, "2026-07-11T13:00:00+00:00", result)
    assert _proposal_status(db_path, entry["proposal"]["proposal_id"]) == "rejected"
    assert storage.is_rejected(db_path, entry["proposal"]["fingerprint"])


def test_approval_label_removed_rejects(board, settings, db_path, fresh_board):
    def drop_prop(data):
        for c in data["cards"]:
            if c["id"] == "prop_yes":
                c["idLabels"] = []
                c["labels"] = []
    b = fresh_board(drop_prop)
    _add_open_proposal(db_path, "prop_yes")
    approvals = sd.parse_approvals(b, storage.get_open_proposals(db_path), settings)
    assert approvals[0]["decision"] == "reject"


def test_approval_unrelated_comment_ignored(board, settings, db_path):
    _add_open_proposal(db_path, "proposed_card")
    approvals = sd.parse_approvals(board, storage.get_open_proposals(db_path), settings)
    assert approvals[0]["decision"] == "open"


def test_proposal_timeout_expires_and_fingerprints(board, make_settings, db_path, now_utc):
    settings = make_settings()  # proposal_timeout_days default 14
    pid = _add_open_proposal(db_path, "proposed_card", opened="2026-06-20T00:00:00+00:00",
                             fp="merge|proposed_card")
    result = ex.ExecutionResult()
    ex.expire_proposals(db_path, board, storage.get_open_proposals(db_path), settings,
                        now_utc, "2026-07-11T13:00:00+00:00", result)
    assert _proposal_status(db_path, pid) == "expired"
    assert storage.is_rejected(db_path, "merge|proposed_card")


def test_proposal_timeout_independent_of_archive_list_days(board, make_settings, db_path, now_utc):
    # opened 3 days ago; timeout=2 (expire) even though archive_list_days=30 (would not).
    settings = make_settings(proposal_timeout_days=2, archive_list_days=30)
    pid = _add_open_proposal(db_path, "proposed_card", opened="2026-07-08T00:00:00+00:00",
                             fp="merge|proposed_card")
    result = ex.ExecutionResult()
    ex.expire_proposals(db_path, board, storage.get_open_proposals(db_path), settings,
                        now_utc, "2026-07-11T13:00:00+00:00", result)
    assert _proposal_status(db_path, pid) == "expired"


# -- helpers --

def _all_proposals(db_path):
    with storage.db_connection(db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM proposals").fetchall()]


def _proposal_status(db_path, pid):
    with storage.db_connection(db_path) as conn:
        row = conn.execute("SELECT status FROM proposals WHERE proposal_id = ?", (pid,)).fetchone()
    return row["status"] if row else None
