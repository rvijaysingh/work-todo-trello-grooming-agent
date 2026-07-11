"""
Phase 2 — Candidate generation (deterministic, no LLM).

Two-track duplicate blocking plus hygiene and recovery candidate selection. All
facts are computed here in Python and later passed to the LLM as constraints.

Blocking uses the normalized-token Jaccard definitions from docs/config.md:
  - narrow track (in-scope): all in-scope names go to the LLM; Python also
    builds hint clusters (Jaccard >= narrow_hint_jaccard, or a shared person
    label, or a shared entity keyword).
  - wide track (vs Scratch/Archive): a pair is blocked if Jaccard >=
    wide_block_jaccard, OR it shares >=1 entity keyword AND >=1 person label.

Entity keywords are entity_keywords_seed plus the spine People names (appended
at runtime).
"""

from __future__ import annotations

import logging

from guardrails import (
    due_is_dead,
    is_name_flagged,
    is_time_based_label,
    jaccard,
    label_is_stale,
    normalize_tokens,
    shares_entity_keyword,
)
from phases.snapshot_diff import recovery_source_lists

logger = logging.getLogger(__name__)


_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def is_sweep_day(now_utc, settings) -> bool:
    """True if today (local) is the configured weekly full-board name sweep day."""
    from guardrails import local_now

    wd = local_now(now_utc, settings.tz_standard_offset, settings.tz_daylight_offset).weekday()
    return _WEEKDAY_NAMES[wd] == settings.weekly_sweep_day


def full_sweep_names(board) -> list[dict]:
    """All open card names (the Sunday full-board semantic sweep input)."""
    return [{"id": c.id, "name": c.name} for c in board.cards if not c.closed]


def build_entity_keywords(settings, spine) -> set[str]:
    """entity_keywords_seed + spine People names, all lowercased."""
    kws = {k.lower() for k in settings.entity_keywords_seed}
    if spine is not None:
        kws |= {n.lower() for n in spine.person_names() if n}
    return kws


def person_labels(card, settings) -> set[str]:
    """Non-time-based, non-agent labels — treated as person/entity labels."""
    agent = {settings.label_auto_updated, settings.label_proposed}
    return {
        l for l in card.label_names
        if l and not is_time_based_label(l) and l not in agent
    }


def _shared_entity_keyword(a, b, entity_keywords: set[str]) -> bool:
    for kw in entity_keywords:
        if kw and kw in a.name.lower() and kw in b.name.lower():
            return True
    return False


# ---------------------------------------------------------------------------
# Narrow track (in-scope)
# ---------------------------------------------------------------------------

def narrow_track(in_scope_cards, entity_keywords: set[str], settings) -> dict:
    """Return all in-scope names plus Python hint clusters.

    Result: {"names": [{id, name}], "hints": [[card_id, ...], ...]}.
    """
    names = [{"id": c.id, "name": c.name} for c in in_scope_cards]

    def linked(a, b) -> bool:
        if jaccard(a.name, b.name) >= settings.narrow_hint_jaccard:
            return True
        if person_labels(a, settings) & person_labels(b, settings):
            return True
        if _shared_entity_keyword(a, b, entity_keywords):
            return True
        return False

    clusters = _connected_components(in_scope_cards, linked)
    hints = [sorted(c) for c in clusters if len(c) >= 2]
    logger.info("Narrow track: %d in-scope names, %d hint cluster(s)", len(names), len(hints))
    return {"names": names, "hints": hints}


def _connected_components(cards, linked) -> list[list[str]]:
    """Union-find connected components over cards under a `linked(a,b)` relation."""
    parent = {c.id: c.id for c in cards}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i in range(len(cards)):
        for j in range(i + 1, len(cards)):
            if linked(cards[i], cards[j]):
                union(cards[i].id, cards[j].id)

    groups: dict[str, list[str]] = {}
    for c in cards:
        groups.setdefault(find(c.id), []).append(c.id)
    return list(groups.values())


# ---------------------------------------------------------------------------
# Wide track (vs Scratch / Archive)
# ---------------------------------------------------------------------------

def wide_track(in_scope_cards, wide_cards, entity_keywords: set[str], settings) -> list[dict]:
    """Return blocked (in-scope, wide) candidate pairs for the LLM.

    Each pair: {"a": in_scope_card, "b": wide_card}.
    """
    pairs = []
    for a in in_scope_cards:
        a_people = person_labels(a, settings)
        for b in wide_cards:
            block = False
            if jaccard(a.name, b.name) >= settings.wide_block_jaccard:
                block = True
            elif _shared_entity_keyword(a, b, entity_keywords) and (a_people & person_labels(b, settings)):
                block = True
            if block:
                pairs.append({"a": a, "b": b})
    logger.info("Wide track: %d blocked pair(s)", len(pairs))
    return pairs


# ---------------------------------------------------------------------------
# Recovery batch
# ---------------------------------------------------------------------------

def recovery_batch(board, settings, processed_ids: set[str]) -> list:
    """Next recovery_batch_size unprocessed scratch/archive cards, newest list first."""
    batch = []
    for lst in recovery_source_lists(board, settings):
        for card in board.cards_in_list(lst.id):
            if card.id in processed_ids:
                continue
            batch.append(card)
            if len(batch) >= settings.recovery_batch_size:
                logger.info("Recovery batch: %d card(s)", len(batch))
                return batch
    logger.info("Recovery batch: %d card(s)", len(batch))
    return batch


# ---------------------------------------------------------------------------
# Hygiene candidates
# ---------------------------------------------------------------------------

def hygiene_candidates(in_scope_cards, board, settings, now_utc) -> dict:
    """Return hygiene candidate sets.

    {
      "flagged_renames": [card, ...],   # names tripping the heuristic (priority)
      "all_in_scope":    [card, ...],   # LLM may nominate renames beyond flagged
      "dead_dues":       [card, ...],   # due > dead_due_days overdue
      "stale_labels":    [(card, label_name), ...],
    }
    """
    flagged = [c for c in in_scope_cards if is_name_flagged(c.name, settings.name_min_length, settings.name_max_length)]
    dead_dues = [c for c in in_scope_cards if due_is_dead(c.due, now_utc, settings)]

    stale = []
    for c in in_scope_cards:
        for label in c.label_names:
            if not is_time_based_label(label):
                continue
            match_list = _matching_list_name(label, board)
            if match_list is not None and match_list != c.list_name:
                # Proxy: an untouched card's last_activity approximates label age.
                if label_is_stale(c.last_activity, now_utc, settings):
                    stale.append((c, label))
    logger.info(
        "Hygiene: %d flagged renames, %d dead dues, %d stale labels",
        len(flagged), len(dead_dues), len(stale),
    )
    return {
        "flagged_renames": flagged,
        "all_in_scope": list(in_scope_cards),
        "dead_dues": dead_dues,
        "stale_labels": stale,
    }


def _matching_list_name(label_name: str, board) -> str | None:
    """The board list whose name is contained in a time-based label name."""
    best = None
    for lst in board.lists:
        if lst.name and lst.name.lower() in label_name.lower():
            if best is None or len(lst.name) > len(best):
                best = lst.name
    return best
