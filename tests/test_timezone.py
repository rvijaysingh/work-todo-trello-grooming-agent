"""Section 12 — time / timezone helper (via shared stdlib timeutil, no zoneinfo)."""

from datetime import datetime, timezone
from pathlib import Path

import guardrails as g

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_window_math_uses_local_tz_offsets_standard():
    """January 'now' → standard offset (-5) applied."""
    now = datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    assert g.local_now(now, -5, -4) == datetime(2026, 1, 15, 10, 0)


def test_window_math_uses_local_tz_offsets_daylight():
    """July 'now' → daylight offset (-4) applied."""
    now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
    assert g.local_now(now, -5, -4) == datetime(2026, 7, 15, 11, 0)


def test_no_zoneinfo_dependency():
    """No agent module imports zoneinfo (the crash-on-Windows workaround guard)."""
    import re

    import_re = re.compile(r"^\s*(?:import\s+zoneinfo|from\s+zoneinfo\s+import)", re.MULTILINE)
    offenders = []
    for py in list(REPO_ROOT.glob("*.py")) + list((REPO_ROOT / "phases").glob("*.py")):
        if import_re.search(py.read_text(encoding="utf-8")):
            offenders.append(py.name)
    assert offenders == [], f"zoneinfo imported in: {offenders}"
