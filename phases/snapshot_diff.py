"""
Phase 1 — Snapshot & Diff.

Parses the board into a BoardView, and diffs the current board against the last
post-run snapshot to detect:
  - implicit rejections (Vijay edited / relabeled / pulled a card the agent
    touched last run),
  - approvals on Agent: Proposed cards (comment "yes"/"approve"),
  - new Scratch sweep lists (auto-added to recovery scope).

Deterministic and LLM-free. Board reads use the shared TrelloClient.

NOTE (P0 limitation): the shared TrelloClient.get_list_cards returns parsed
TrelloCard objects without the `badges` field, so at runtime has_attachments /
has_checklist cannot be populated from list reads. build_board (used for the
fixture and any raw payload) reads badges directly. See LESSONS.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from guardrails import fingerprint
from models import BoardView, Card, Comment, ListInfo

logger = logging.getLogger(__name__)


def _desc_hash(desc: str) -> str:
    return hashlib.sha256((desc or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Board construction
# ---------------------------------------------------------------------------

def build_board(raw: dict) -> BoardView:
    """Build a BoardView from a raw Trello-shaped board dict (fixture/export)."""
    lists = [
        ListInfo(
            id=l["id"], name=l["name"],
            closed=bool(l.get("closed", False)), pos=float(l.get("pos", 0.0)),
        )
        for l in raw.get("lists", [])
    ]
    labels = [
        {"id": lb["id"], "name": lb.get("name", ""), "color": lb.get("color")}
        for lb in raw.get("labels", [])
    ]
    list_name = {l.id: l.name for l in lists}
    cards = []
    for c in raw.get("cards", []):
        badges = c.get("badges", {}) or {}
        label_names = [lb.get("name", "") for lb in c.get("labels", [])]
        cards.append(
            Card(
                id=c["id"],
                name=c.get("name", ""),
                desc=c.get("desc", ""),
                list_id=c.get("idList", ""),
                list_name=list_name.get(c.get("idList", ""), ""),
                label_ids=list(c.get("idLabels", [])),
                label_names=label_names,
                due=c.get("due"),
                last_activity=c.get("dateLastActivity", ""),
                has_attachments=int(badges.get("attachments", 0)) > 0,
                has_checklist=int(badges.get("checkItems", 0)) > 0,
                closed=bool(c.get("closed", False)),
                url=c.get("shortUrl", c.get("url", "")),
                pos=float(c.get("pos", 0.0)),
            )
        )
    comments = [
        Comment(
            id=cm["id"], card_id=cm["idCard"], text=cm.get("text", ""),
            date=cm.get("date", ""), member=cm.get("memberName", ""),
        )
        for cm in raw.get("comments", [])
    ]
    return BoardView(lists=lists, labels=labels, cards=cards, comments=comments)


def card_from_trello(tc, list_name: str) -> Card:
    """Build a Card from a shared TrelloCard.

    Attachment/checklist badges come from TrelloCard.attachment_count /
    has_checklist (agent-shared-library >= 0.2.1), so the forced-Tier-2 merge rule
    (design §5.3) fires at runtime — not just against fixture badges.
    """
    return Card(
        id=tc.id,
        name=tc.name,
        desc=tc.description,
        list_id=tc.list_id,
        list_name=list_name,
        label_ids=[lbl.id for lbl in tc.labels],
        label_names=[lbl.name for lbl in tc.labels],
        due=tc.due_date,
        last_activity=tc.last_activity,
        has_attachments=getattr(tc, "attachment_count", 0) > 0,
        has_checklist=getattr(tc, "has_checklist", False),
        closed=tc.closed,
        url=tc.url,
        pos=tc.position,
    )


def snapshot_rows(board: BoardView):
    """Return the board's cards (used as snapshot input)."""
    return list(board.cards)


# ---------------------------------------------------------------------------
# Diff: implicit rejections
# ---------------------------------------------------------------------------

def detect_implicit_rejections(prev_snapshot: dict, board: BoardView, prior_actions,
                               settings, archive_list_id: str | None):
    """Detect cards the agent touched last run that Vijay has since overridden.

    Returns a list of rejection dicts:
        {fingerprint, card_id, source, remove_label}
    source ∈ {'edit', 'label-removal'}. remove_label is True when the card still
    carries Agent: Auto-Updated and the agent should strip it (his version final).
    """
    rejections: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for act in prior_actions:
        if act.get("tier") != 1 or act.get("status") != "success":
            continue
        payload = act.get("payload", {}) or {}
        card_ids = act.get("card_ids", [])
        fp = fingerprint(act["action_type"], card_ids, payload.get("new_name"))

        primary = payload.get("survivor_id") or (card_ids[0] if card_ids else None)
        if primary is None:
            continue
        prev = prev_snapshot.get(primary)
        now = board.card_by_id(primary)

        source = None
        remove_label = False
        if prev and now:
            prev_labels = json.loads(prev.get("labels_json") or "[]")
            if now.name != prev.get("name") or _desc_hash(now.desc) != prev.get("desc_hash"):
                source = "edit"
            elif settings.label_auto_updated in prev_labels and \
                    settings.label_auto_updated not in now.label_names:
                source = "label-removal"
            if settings.label_auto_updated in now.label_names and source == "edit":
                remove_label = True

        # Archive-list pull-back: a loser dragged back out of the Agent Archive list.
        if archive_list_id and act["action_type"] in ("merge", "recovery_merge"):
            for lid in payload.get("loser_ids", []):
                prevL = prev_snapshot.get(lid)
                nowL = board.card_by_id(lid)
                if prevL and nowL and prevL.get("list_id") == archive_list_id \
                        and nowL.list_id != archive_list_id:
                    source = source or "edit"

        if source is not None:
            key = (fp, primary)
            if key not in seen:
                seen.add(key)
                rejections.append({
                    "fingerprint": fp,
                    "card_id": primary,
                    "source": source,
                    "remove_label": remove_label,
                })
    logger.info("Detected %d implicit rejection(s)", len(rejections))
    return rejections


# ---------------------------------------------------------------------------
# Diff: new scratch lists (recovery scope auto-expansion)
# ---------------------------------------------------------------------------

def recovery_source_lists(board: BoardView, settings) -> list[ListInfo]:
    """Lists matching recovery_include_pattern minus recovery_exclude_pattern.

    Ordered newest-first by any trailing 'M-D' date in the list name, falling
    back to board position. New Scratch lists are picked up automatically.
    """
    inc = re.compile(settings.recovery_include_pattern)
    exc = re.compile(settings.recovery_exclude_pattern)
    matches = [
        l for l in board.lists
        if not l.closed and inc.search(l.name) and not exc.search(l.name)
    ]
    matches.sort(key=lambda l: (_list_date_key(l.name), l.pos), reverse=True)
    return matches


_DATE_RE = re.compile(r"(\d{1,2})-(\d{1,2})\b")


def _list_date_key(name: str) -> tuple[int, int]:
    """Extract a (month, day) sort key from a list name like 'Scratch 6-24'."""
    m = _DATE_RE.search(name)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (0, 0)


# ---------------------------------------------------------------------------
# Diff: approvals on Agent: Proposed cards
# ---------------------------------------------------------------------------

_APPROVE_WORDS = {"yes", "y", "approve", "approved", "ok", "okay", "do it"}
_REJECT_WORDS = {"no", "n", "reject", "rejected", "nope"}


def parse_approvals(board: BoardView, open_proposals, settings):
    """Read comments on Agent: Proposed cards and resolve each open proposal.

    Returns a list of {proposal, decision} where decision ∈
    {'approve','reject','open'}. A "yes"/"approve" comment approves; "no"/label
    removal rejects; anything else leaves it open.
    """
    results = []
    for prop in open_proposals:
        card_ids = prop.get("card_ids", [])
        card_id = card_ids[0] if card_ids else None
        card = board.card_by_id(card_id) if card_id else None
        decision = "open"

        # Label removed entirely → rejection.
        if card is not None and settings.label_proposed not in card.label_names:
            decision = "reject"
        else:
            for cm in board.comments_for(card_id) if card_id else []:
                text = cm.text.strip().lower()
                if text in _APPROVE_WORDS or text.startswith(("yes", "approve")):
                    decision = "approve"
                elif text in _REJECT_WORDS or text.startswith(("no", "reject")):
                    decision = "reject"
        results.append({"proposal": prop, "decision": decision})
    return results
