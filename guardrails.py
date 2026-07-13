"""
Guardrails and deterministic logic — pure functions, no I/O, no LLM.

Everything the design says must be "enforced in Python, never rely on the LLM"
lives here: token/Jaccard blocking math, name-quality heuristics, rejection
fingerprints, wall-clock window predicates (via the shared stdlib timeutil
helper — no zoneinfo), the never-touch filter, per-run caps, the merge
string-containment invariant, LLM-name grounding, and tier assignment
(design.md §4 forced-tier rules + the two manual toggles).

All time math runs through agent_shared.infra.timeutil, driven by the agent's
local_tz_offsets. "now" is always passed in (a UTC datetime) so runs are
deterministic and testable.
"""

from __future__ import annotations

import logging
import re
import string
from datetime import datetime, timezone

from agent_shared.infra import utc_to_local

logger = logging.getLogger(__name__)

# Small English stopword set for name-token normalization.
_STOPWORDS = frozenset(
    """
    a an and are as at be by for from has have in into is it its of on or that
    the to with re vs via w wo x amp
    """.split()
)

_PUNCT_TABLE = {ord(c): " " for c in string.punctuation}

# Time-based labels are named like "1. Today (must do)", "2. Next Few Days ...".
_TIME_LABEL_RE = re.compile(r"^\s*\d+\.\s")

# Proper-noun-ish tokens for name grounding.
_PROPER_RE = re.compile(r"\b[A-Z][A-Za-z][A-Za-z'&/-]*\b")

# Capitalized words that are common enough not to be treated as named entities.
_COMMON_CAP_WORDS = frozenset(
    w.lower()
    for w in (
        "Monday Tuesday Wednesday Thursday Friday Saturday Sunday "
        "Jan Feb Mar Apr Jun Jul Aug Sep Sept Oct Nov Dec "
        "January February March April May June July August September October "
        "November December Today Tomorrow Q1 Q2 Q3 Q4 EOD EOW TBD Review "
        "Update Send Set Fix Draft Prep Follow Schedule Confirm Check Email Call"
    ).split()
)


# ---------------------------------------------------------------------------
# Tokenization / Jaccard blocking
# ---------------------------------------------------------------------------

def normalize_tokens(name: str) -> set[str]:
    """Lowercase, strip punctuation, drop stopwords → set of tokens."""
    if not name:
        return set()
    lowered = name.lower().translate(_PUNCT_TABLE)
    return {t for t in lowered.split() if t and t not in _STOPWORDS}


def jaccard(a: str, b: str) -> float:
    """Jaccard similarity of two names' normalized token sets (0.0–1.0)."""
    ta, tb = normalize_tokens(a), normalize_tokens(b)
    if not ta and not tb:
        return 0.0
    inter = ta & tb
    union = ta | tb
    return len(inter) / len(union) if union else 0.0


def shares_entity_keyword(name: str, entity_keywords: set[str]) -> bool:
    """True if the (lowercased) name contains any entity keyword phrase."""
    low = name.lower()
    return any(kw and kw in low for kw in entity_keywords)


# ---------------------------------------------------------------------------
# Name-quality heuristics (design.md §5.1 — prioritization filter, not a gate)
# ---------------------------------------------------------------------------

def is_name_flagged(name: str, min_length: int, max_length: int) -> bool:
    """Return True if the card name trips any name-quality heuristic.

    Flagged if: contains a pipe; shorter than min_length or longer than
    max_length; entirely lowercase; contains a consecutive double space; or has
    leading/trailing whitespace.
    """
    if name != name.strip():
        return True
    if "|" in name:
        return True
    if len(name) < min_length or len(name) > max_length:
        return True
    if "  " in name:
        return True
    letters = [c for c in name if c.isalpha()]
    if letters and name.lower() == name and name.upper() != name:
        return True
    return False


# ---------------------------------------------------------------------------
# Rejection fingerprints (design.md §6)
# ---------------------------------------------------------------------------

def fingerprint(action_type: str, card_ids, new_name: str | None = None) -> str:
    """Fingerprint = action type + sorted card ids (+ new name for renames)."""
    ids = ",".join(sorted(card_ids))
    base = f"{action_type}|{ids}"
    if new_name is not None:
        base += f"|{new_name}"
    return base


# ---------------------------------------------------------------------------
# Time / window predicates (via shared stdlib timeutil — no zoneinfo)
# ---------------------------------------------------------------------------

def parse_utc(ts: str | None) -> datetime | None:
    """Parse a Trello ISO 8601 UTC timestamp to an aware UTC datetime."""
    if not ts:
        return None
    cleaned = ts.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        logger.warning("Unparseable timestamp: %r", ts)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_now(now_utc: datetime, standard_offset: int, daylight_offset: int) -> datetime:
    """Convert the run's UTC 'now' to naive local time via the shared helper.

    This is the single seam the timezone tests exercise: it applies the standard
    or daylight offset from local_tz_offsets using the US DST rule encoded in
    agent_shared.infra.timeutil.
    """
    return utc_to_local(now_utc, standard_offset, daylight_offset)


def _local(ts_utc: datetime, standard_offset: int, daylight_offset: int) -> datetime:
    return utc_to_local(ts_utc, standard_offset, daylight_offset)


def hours_since(ts: str | None, now_utc: datetime, standard_offset: int, daylight_offset: int) -> float | None:
    """Local wall-clock hours between a timestamp and now (None if unparseable)."""
    dt = parse_utc(ts)
    if dt is None:
        return None
    delta = local_now(now_utc, standard_offset, daylight_offset) - _local(dt, standard_offset, daylight_offset)
    return delta.total_seconds() / 3600.0


def days_since(ts: str | None, now_utc: datetime, standard_offset: int, daylight_offset: int) -> float | None:
    """Local wall-clock days between a timestamp and now (None if unparseable)."""
    hrs = hours_since(ts, now_utc, standard_offset, daylight_offset)
    return None if hrs is None else hrs / 24.0


def edited_within_no_touch(last_activity: str | None, now_utc: datetime, settings) -> bool:
    """True if the card was edited within no_touch_hours of now."""
    hrs = hours_since(last_activity, now_utc, settings.tz_standard_offset, settings.tz_daylight_offset)
    return hrs is not None and 0 <= hrs < settings.no_touch_hours


def due_is_dead(due: str | None, now_utc: datetime, settings) -> bool:
    """True if the due date is more than dead_due_days overdue."""
    d = days_since(due, now_utc, settings.tz_standard_offset, settings.tz_daylight_offset)
    return d is not None and d > settings.dead_due_days


def label_expired(applied_ts: str | None, now_utc: datetime, settings) -> bool:
    """True if an Agent: Auto-Updated label is older than optimistic_label_days.

    (The old quarantine_days window is gone; the Auto-Updated label — a purely
    cosmetic "the agent touched this" marker — expires on the same short window
    as optimistic time-based labels.)
    """
    d = days_since(applied_ts, now_utc, settings.tz_standard_offset, settings.tz_daylight_offset)
    return d is not None and d >= settings.optimistic_label_days


def archive_list_expired(entered_ts: str | None, now_utc: datetime, settings) -> bool:
    """True if a card has sat in the Agent Archive list longer than archive_list_days.

    Such a card is then archived via Trello's built-in (restorable) archive.
    """
    d = days_since(entered_ts, now_utc, settings.tz_standard_offset, settings.tz_daylight_offset)
    return d is not None and d >= settings.archive_list_days


def proposal_expired(opened_ts: str | None, now_utc: datetime, settings) -> bool:
    """True if an open proposal is older than proposal_timeout_days."""
    d = days_since(opened_ts, now_utc, settings.tz_standard_offset, settings.tz_daylight_offset)
    return d is not None and d >= settings.proposal_timeout_days


def label_is_stale(applied_ts: str | None, now_utc: datetime, settings) -> bool:
    """True if a time-based label was applied more than optimistic_label_days ago."""
    d = days_since(applied_ts, now_utc, settings.tz_standard_offset, settings.tz_daylight_offset)
    return d is not None and d > settings.optimistic_label_days


def is_time_based_label(label_name: str) -> bool:
    """True for the diluted time-based labels (e.g. '1. Today (must do)')."""
    return bool(_TIME_LABEL_RE.match(label_name))


_OWNER_TITLE_RE = re.compile(r"^\s*\[\s*owner\s*:", re.IGNORECASE)


def is_owner_titled(name: str) -> bool:
    """True for cards whose title is of the form '[Owner: Name] ...'.

    Such cards are delegated/handed-off items and are archive candidates under the
    'no longer needed' test (design §4).
    """
    return bool(_OWNER_TITLE_RE.match(name or ""))


# ---------------------------------------------------------------------------
# Never-touch filter (design.md §6)
# ---------------------------------------------------------------------------

def is_never_touch(
    card,
    now_utc: datetime,
    settings,
    in_scope_list_ids: set[str],
    open_proposed_card_ids: set[str],
    protected_card_ids=None,
) -> bool:
    """True if the card must never be edited this run.

    Never touch: protected cards (the Grooming Report card and the weekly
    spine-review reminder card), any card edited within no_touch_hours, any card
    carrying an open Agent: Proposed decision, anything out of edit scope.

    protected_card_ids may be a set of ids or a single id string (or None). When
    no_touch_hours is 0 the no-touch window is disabled, but the open-proposal
    lock and out-of-scope guard here (and the rejection ledger elsewhere) still
    protect their cards.
    """
    if protected_card_ids is not None:
        if isinstance(protected_card_ids, str):
            if card.id == protected_card_ids:
                return True
        elif card.id in protected_card_ids:
            return True
    if card.id in open_proposed_card_ids or card.has_label(settings.label_proposed):
        return True
    if card.list_id not in in_scope_list_ids:
        return True
    if edited_within_no_touch(card.last_activity, now_utc, settings):
        return True
    return False


def value_written_in_sources(value: str | None, source_texts) -> bool:
    """True if a verbatim substring `value` appears in any source text.

    Used to enforce "re-date only from a date written in the card text or the
    spine" — the LLM must cite the exact substring it read the new date from, and
    Python confirms that substring is really present before trusting the new date.
    """
    if not value:
        return False
    needle = value.strip().lower()
    if not needle:
        return False
    return any(needle in (t or "").lower() for t in source_texts)


# ---------------------------------------------------------------------------
# Per-run caps (design.md §6)
# ---------------------------------------------------------------------------

def apply_cap(items: list, cap: int, priority=None) -> tuple[list, list]:
    """Split items into (allowed, deferred) honoring a per-run cap.

    If priority is given, items where priority(item) is truthy are taken first,
    so heuristic-flagged renames win when max_renames_per_run binds.
    """
    if priority is not None:
        ordered = [x for x in items if priority(x)] + [x for x in items if not priority(x)]
    else:
        ordered = list(items)
    return ordered[:cap], ordered[cap:]


# ---------------------------------------------------------------------------
# Merge content invariant (design.md §6)
# ---------------------------------------------------------------------------

def merge_contains_all_sources(survivor_desc: str, source_cards) -> bool:
    """Verify the survivor description contains every source card's name and desc.

    String-containment check enforced before any losing card moves to the archive.
    """
    for src in source_cards:
        if src.name and src.name not in survivor_desc:
            logger.error("Merge invariant: source name %r missing from survivor desc", src.name)
            return False
        if src.desc and src.desc not in survivor_desc:
            logger.error("Merge invariant: source desc for %s missing from survivor desc", src.id)
            return False
    return True


# ---------------------------------------------------------------------------
# LLM-name grounding (design.md §6)
# ---------------------------------------------------------------------------

def name_is_grounded(proposed_name: str, source_texts, spine_terms) -> bool:
    """True if the proposed name invents no person/entity absent from sources/spine.

    Every proper-noun-ish token in the proposed name must appear (case-insensitive)
    in the union of source card text and spine terms, unless it is a common
    calendar/verb word.
    """
    allowed = set()
    for text in list(source_texts) + list(spine_terms):
        allowed |= {t.lower() for t in _PROPER_RE.findall(text or "")}
        allowed |= {t for t in (text or "").lower().translate(_PUNCT_TABLE).split()}
    for token in _PROPER_RE.findall(proposed_name or ""):
        low = token.lower()
        if low in _COMMON_CAP_WORDS:
            continue
        if low not in allowed:
            logger.warning("Ungrounded entity %r in proposed name %r", token, proposed_name)
            return False
    return True


# ---------------------------------------------------------------------------
# Tier assignment (design.md §4 forced-tier rules + manual toggles)
# ---------------------------------------------------------------------------

TIER1 = 1
TIER2 = 2


def is_borderline(action: dict, settings) -> bool:
    """True if an auto-eligible action is borderline and should be Proposed instead.

    Borderline when the LLM flags it (`borderline: true`) or reports a confidence
    below `auto_min_confidence`. A missing confidence is treated as NOT borderline
    (deterministic actions the LLM did not score still auto-execute).
    """
    if action.get("borderline"):
        return True
    conf = action.get("confidence")
    if conf is None:
        return False
    try:
        return int(conf) < settings.auto_min_confidence
    except (TypeError, ValueError):
        return True


def assign_tier(action: dict, settings) -> int:
    """Return the enforced tier (1 or 2) for an action after LLM judgment.

    Forced-Tier-2 conditions on merges override any LLM/heuristic Tier-1 call.
    The category toggles (tier1_stale_label_removal, tier1_recovery_archive,
    tier1_due_date_clear) gate auto-execution: when the toggle is True the action
    is Tier 1 unless it is borderline (then Tier 2); when False the whole category
    is Tier 2 (Agent: Proposed).
    """
    atype = action.get("type")

    # Always Tier 1 (loss-free by construction).
    if atype in ("rename", "desc_restructure"):
        return TIER1

    # Category toggles — auto only when the flag is on AND the call isn't borderline.
    if atype in ("dead_due_clear", "due_redate"):
        if settings.tier1_due_date_clear and not is_borderline(action, settings):
            return TIER1
        return TIER2
    if atype in ("stale_label_removal", "label_swap"):
        if settings.tier1_stale_label_removal and not is_borderline(action, settings):
            return TIER1
        return TIER2
    if atype in ("recovery_archive", "inscope_archive"):
        if settings.tier1_recovery_archive and not is_borderline(action, settings):
            return TIER1
        return TIER2

    # Recovery routing to a list is a Tier 1 auto action.
    if atype in ("recovery_route_today", "recovery_route_nfd", "recovery_route_inbox"):
        return TIER1

    # Merges (including recovery merges): Tier-2 forcers win first.
    if atype in ("merge", "recovery_merge"):
        if action.get("loser_has_attachment") or action.get("loser_has_checklist"):
            return TIER2
        if action.get("cross_person_labels"):
            return TIER2
        if action.get("conflicting_due") or action.get("conflicting_owner"):
            return TIER2
        if action.get("survivor_outside_edit_scope"):
            return TIER2
        if action.get("exact_or_near_name_match"):
            return TIER1
        # No forcer fired — honor the LLM's confidence call (default cautious).
        return TIER2 if int(action.get("llm_tier", TIER2)) == TIER2 else TIER1

    # Unknown → cautious.
    return TIER2
