"""
Reprioritization signal computation (spine "How this works" / Problem 5).

The canonical signal names appear verbatim in card comments and the report; each is
rendered WITH its value. This module holds the standalone, code-verifiable pieces —
card age, source-meeting date, staleness, priority-person and terminal-workstream
matching, and reflection-card detection. The list/due/workstream verifiers stay in
phases/reprioritize.py (which imports this module), so there is no import cycle.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# --- canonical signal names -------------------------------------------------
SIG_LABEL = "existing_label_priority"                 # was priority_label
SIG_DUE = "existing_due_date_matches_card_list"       # was due_in_window
SIG_WORKSTREAM = "spine_workstream_priority_high"     # was workstream_high
SIG_PEOPLE = "spine_people_priority"                  # new
SIG_STALE = "staleness_likelihood"                    # new (veto)
SIG_IMPLIED = "implied_task_urgency"                  # new (LLM-only, never sole)
SIG_NO_URGENCY = "no_urgency_markers"                 # was weak

# Signals that, when code-verified, satisfy the "≥1 verified signal" gate.
SATISFYING_UP = frozenset({SIG_LABEL, SIG_DUE, SIG_WORKSTREAM, SIG_PEOPLE})
SATISFYING_DOWN = frozenset({SIG_NO_URGENCY})
# Modifiers: accepted alongside satisfying signals but never satisfy the gate on
# their own and never invalidate the automatic path (implied is not code-checkable;
# staleness is a veto, not a promotion reason).
MODIFIER_SIGNALS = frozenset({SIG_IMPLIED, SIG_STALE})

# Back-compat: retired signal names map to the new ones (older tests / injected
# verdicts keep working).
_ALIASES = {
    "priority_label": SIG_LABEL, "p0_label": SIG_LABEL, "p1_label": SIG_LABEL,
    "due_in_window": SIG_DUE, "due": SIG_DUE, "due_date": SIG_DUE,
    "workstream_high": SIG_WORKSTREAM, "workstream": SIG_WORKSTREAM,
    "workstream_match": SIG_WORKSTREAM,
    "weak": SIG_NO_URGENCY, "weakness": SIG_NO_URGENCY, "no_urgency": SIG_NO_URGENCY,
}


def canonical_signal(name: str) -> str:
    """Map any (possibly retired) signal name to its canonical current name."""
    low = (name or "").strip().lower()
    return _ALIASES.get(low, low)


# --- card age / source-meeting date -----------------------------------------

def created_dt_from_id(card_id: str) -> datetime | None:
    """A Trello object id encodes its creation unix time in the first 8 hex chars."""
    if not card_id or len(card_id) < 8:
        return None
    try:
        return datetime.fromtimestamp(int(card_id[:8], 16), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


_ISO_DATE_RE = re.compile(r"\*\*Date:\*\*\s*(\d{4})-(\d{1,2})-(\d{1,2})")
_MD_MEETING_RE = re.compile(r"\*\*Source meeting:\*\*\s*(\d{1,2})/(\d{1,2})")


def source_meeting_date(desc: str, default_year: int) -> datetime | None:
    """Parse the Granola-written '**Date:** YYYY-MM-DD' (or '**Source meeting:** M/D')."""
    if not desc:
        return None
    m = _ISO_DATE_RE.search(desc)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    m = _MD_MEETING_RE.search(desc)
    if m:
        try:
            return datetime(default_year, int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


# Task-nature hints: one-time / event-bound asks age fastest (halve the thresholds).
_ONE_TIME_KEYWORDS = (
    "return", "download", "photo", "expense", "name tag", "lanyard", "rsvp",
    "reserve", "book ", "print", "circle back", "reminder", "get from", "pick up",
)


def _fmt_md(dt: datetime) -> str:
    return f"{dt.month}/{dt.day}"


def staleness_level(card, now_utc, settings) -> tuple[str, str]:
    """Code-computed staleness (high/medium/low) and its basis string.

    Origin = the source-meeting date if written, else the card's created date. Age
    is days from origin to now. One-time / event-bound asks (keyword heuristic) age
    twice as fast. HIGH vetoes promotion (enforced by the caller).
    """
    origin = source_meeting_date(card.desc, now_utc.year)
    origin_src = f"source meeting {_fmt_md(origin)}" if origin else ""
    if origin is None:
        origin = created_dt_from_id(card.id)
        origin_src = f"created {_fmt_md(origin)}" if origin else ""
    if origin is None:
        return "low", "no date basis"
    age_days = max(0.0, (now_utc - origin).total_seconds() / 86400.0)
    one_time = any(kw in (card.name or "").lower() for kw in _ONE_TIME_KEYWORDS)
    hi = settings.staleness_high_days
    med = settings.staleness_medium_days
    if one_time:
        hi, med = max(1, hi // 2), max(1, med // 2)
    level = "high" if age_days >= hi else "medium" if age_days >= med else "low"
    basis = f"{origin_src}, {int(age_days)}d old" + (" (one-time ask)" if one_time else "")
    return level, basis


# --- people / terminal-workstream / reflection ------------------------------

def _mentions(card, name: str) -> bool:
    if not name:
        return False
    hay = f"{card.name} {card.desc}".lower()
    return re.search(rf"\b{re.escape(name.lower())}\b", hay) is not None


def matched_priority_person(card, spine) -> str | None:
    """The name of a priority-raising person the card mentions, or None."""
    if spine is None:
        return None
    for name in spine.priority_person_names():
        if _mentions(card, name):
            return name
    return None


def _tokens(text: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split() if len(t) > 2}


def matched_terminal_workstream(card, spine):
    """A Done/Complete/Paused workstream the card matches (shared token), or None.

    A match here NEVER promotes — the card routes to the archive/backlog path.
    """
    if spine is None:
        return None
    ctoks = _tokens(card.name)
    for w in spine.workstreams:
        if w.status.lower() in {"done", "complete", "completed", "paused"} and _tokens(w.name) & ctoks:
            return w
    return None


_REFLECTION_RE = re.compile(r"^\s*reflection\b|coaching|career development", re.IGNORECASE)


def is_reflection_card(card, settings) -> bool:
    """Self-reflection / coaching / career-development card (Notes rule): never
    archived, low time-sensitivity by default."""
    if _REFLECTION_RE.search(card.name or ""):
        return True
    return any(l.strip().lower() == "career development" for l in card.label_names)
