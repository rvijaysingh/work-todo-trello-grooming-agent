"""
Canonical agent action vocabulary + the structured 3-bullet card-comment format.

One fixed phrase per action type (from the spine's "How this works" / "Actions the
agent can take"), used identically in every card comment and report line. Every
action comment is exactly three bullets:

    - Input signals: (1) <name>: <value> (2) <name>: <value> ...
    - [<Action>] <from/to where applicable>
    - <rationale sentence> Confidence: NN%.

These constants and the builder are the single source of truth; execute.py,
reprioritize.py, and report.py import them. Never hardcode an action phrase.
"""

from __future__ import annotations

ACTION_MERGE = "Merge Duplicates"
ACTION_ARCHIVE = "Move to Archive"
ACTION_BACKLOG = "Move to Backlog"
ACTION_RECOVER = "Recover From Scratch"
ACTION_RENAME = "Rename Card"
ACTION_FIX_DUE = "Fix Due Date"
ACTION_TIME_LABEL = "Update Time Label"
ACTION_INCREASE = "Increase Time-Sensitivity"
ACTION_DECREASE = "Decrease Time-Sensitivity"

# Back-compat aliases (retired names still referenced in a few spots/tests).
ACTION_MARK_MORE = ACTION_INCREASE
ACTION_MARK_LESS = ACTION_DECREASE

# Internal action-type -> canonical phrase.
ACTION_BY_TYPE: dict[str, str] = {
    "merge": ACTION_MERGE,
    "recovery_merge": ACTION_MERGE,
    "inscope_archive": ACTION_ARCHIVE,
    "recovery_archive": ACTION_ARCHIVE,
    "trello_archive": ACTION_ARCHIVE,
    "backlog": ACTION_BACKLOG,
    "recovery_route": ACTION_RECOVER,
    "rename": ACTION_RENAME,
    "dead_due_clear": ACTION_FIX_DUE,
    "due_redate": ACTION_FIX_DUE,
    "stale_label_removal": ACTION_TIME_LABEL,
    "label_swap": ACTION_TIME_LABEL,
    "reprioritize_up": ACTION_INCREASE,
    "reprioritize_down": ACTION_DECREASE,
}


def action_phrase(action_type: str | None) -> str:
    """Canonical action phrase for an internal action type (falls back to the type)."""
    return ACTION_BY_TYPE.get(action_type or "", (action_type or "review"))


def three_bullet(signals, action_label: str, from_to: str | None = None,
                 rationale: str = "", confidence=None, prefix: str = "") -> str:
    """Build the canonical three-bullet card comment.

    signals: an ordered list of (name, value) pairs rendered in bullet 1.
    action_label: a phrase from this module (rendered as "[<label>]").
    from_to: e.g. "from 'Today' to 'This Week' card list" (bullet 2, optional).
    prefix: optional leading note prepended to bullet 3 (e.g. a placement conflict).
    """
    if signals:
        b1 = "Input signals: " + " ".join(
            f"({i}) {name}: {value}" for i, (name, value) in enumerate(signals, 1))
    else:
        b1 = "Input signals: (none)"
    b2 = f"[{action_label}]" + (f" {from_to}" if from_to else "")
    rationale = (rationale or "").rstrip(".")
    b3 = (f"{prefix} " if prefix else "") + (f"{rationale}." if rationale else "")
    if confidence is not None:
        b3 = (b3 + " " if b3 else "") + f"Confidence: {int(confidence)}%."
    return "\n".join(f"- {b}" for b in (b1, b2, b3.strip()))
