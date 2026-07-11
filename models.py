"""
Agent domain models.

Card / BoardView are the grooming agent's own representation of the board,
richer than the shared TrelloCard (they carry list name, label names, and the
attachment/checklist badges the merge-tiering rule in design.md §5.3 needs).
These are plain data holders — no API calls, no business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Card:
    """A Trello card as the grooming agent sees it."""

    id: str
    name: str
    desc: str = ""
    list_id: str = ""
    list_name: str = ""
    label_ids: list[str] = field(default_factory=list)
    label_names: list[str] = field(default_factory=list)
    due: str | None = None            # ISO 8601 UTC, or None
    last_activity: str = ""           # ISO 8601 UTC
    has_attachments: bool = False
    has_checklist: bool = False
    closed: bool = False
    url: str = ""
    pos: float = 0.0

    def has_label(self, label_name: str) -> bool:
        return label_name in self.label_names


@dataclass
class Comment:
    """A comment (commentCard action) on a card."""

    id: str
    card_id: str
    text: str
    date: str = ""          # ISO 8601 UTC
    member: str = ""


@dataclass
class ListInfo:
    """A board list (column)."""

    id: str
    name: str
    closed: bool = False
    pos: float = 0.0


@dataclass
class BoardView:
    """Parsed board: lists, labels, cards, and comments, with lookup indexes."""

    lists: list[ListInfo] = field(default_factory=list)
    labels: list[dict] = field(default_factory=list)   # {id, name, color}
    cards: list[Card] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)

    # -- lookups ------------------------------------------------------------

    def list_by_name(self, name: str) -> ListInfo | None:
        for lst in self.lists:
            if lst.name == name:
                return lst
        return None

    def list_by_id(self, list_id: str) -> ListInfo | None:
        for lst in self.lists:
            if lst.id == list_id:
                return lst
        return None

    def label_id(self, label_name: str) -> str | None:
        for lbl in self.labels:
            if lbl.get("name") == label_name:
                return lbl.get("id")
        return None

    def cards_in_list(self, list_id: str) -> list[Card]:
        return [c for c in self.cards if c.list_id == list_id and not c.closed]

    def card_by_id(self, card_id: str) -> Card | None:
        for c in self.cards:
            if c.id == card_id:
                return c
        return None

    def comments_for(self, card_id: str) -> list[Comment]:
        return [cm for cm in self.comments if cm.card_id == card_id]

    def all_card_ids(self) -> set[str]:
        return {c.id for c in self.cards}
