"""
Canonical agent action vocabulary.

One fixed phrase per action type, used *identically* in every card comment and
every report line so the board and the report speak the same language. The
standard comment layout is:

    <Action>: <detail>. Reason: <one line>. Confidence: NN%.

These constants are the single source of truth; execute.py (comments) and
report.py (report lines) both import them. Never hardcode an action phrase.
"""

from __future__ import annotations

ACTION_MERGE = "Merge Duplicates"
ACTION_ARCHIVE = "Move to Archive"
ACTION_RECOVER = "Recover From Scratch"
ACTION_RENAME = "Rename Card"
ACTION_FIX_DUE = "Fix Due Date"
ACTION_TIME_LABEL = "Update Time Label"
ACTION_MARK_MORE = "Mark More Time-sensitive"
ACTION_MARK_LESS = "Mark Less Time-sensitive"

# Internal action-type -> canonical phrase. Covers every type that surfaces in a
# comment or report line (proposals and executed actions share the mapping).
ACTION_BY_TYPE: dict[str, str] = {
    "merge": ACTION_MERGE,
    "recovery_merge": ACTION_MERGE,
    "inscope_archive": ACTION_ARCHIVE,
    "recovery_archive": ACTION_ARCHIVE,
    "trello_archive": ACTION_ARCHIVE,
    "recovery_route": ACTION_RECOVER,
    "rename": ACTION_RENAME,
    "dead_due_clear": ACTION_FIX_DUE,
    "due_redate": ACTION_FIX_DUE,
    "stale_label_removal": ACTION_TIME_LABEL,
    "label_swap": ACTION_TIME_LABEL,
    "reprioritize_up": ACTION_MARK_MORE,
    "reprioritize_down": ACTION_MARK_LESS,
}


def action_phrase(action_type: str | None) -> str:
    """Canonical action phrase for an internal action type (falls back to the type)."""
    return ACTION_BY_TYPE.get(action_type or "", (action_type or "review"))
