"""
Shared pytest fixtures.

All external dependencies (Trello, LLM, Notion, SMTP) are mocked; no network
calls. "now" is a fixed injected UTC datetime so window math is deterministic.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import settings as settings_mod
import storage
from phases import snapshot_diff as sd
from spine import load_spine_from_dict

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[1]

# Fixed reference "now" — matches the fixture timestamps (2026-07-11).
NOW_UTC = datetime(2026, 7, 11, 13, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def now_utc():
    return NOW_UTC


@pytest.fixture
def raw_board():
    return json.loads((FIXTURES / "board_export.json").read_text(encoding="utf-8"))


@pytest.fixture
def board(raw_board):
    return sd.build_board(raw_board)


@pytest.fixture
def fresh_board(raw_board):
    """A deep-copied board so a test can mutate the raw dict without bleed."""
    def _make(mutate=None):
        data = copy.deepcopy(raw_board)
        if mutate:
            mutate(data)
        return sd.build_board(data)
    return _make


@pytest.fixture
def spine():
    return load_spine_from_dict(json.loads((FIXTURES / "spine.json").read_text(encoding="utf-8")))


@pytest.fixture
def settings():
    """Base settings loaded from the repo-root agent_config.json."""
    return settings_mod.load_settings(str(REPO_ROOT / "agent_config.json"))


@pytest.fixture
def make_settings():
    """Factory returning settings with field overrides (e.g. flip a toggle)."""
    def _make(**overrides):
        s = settings_mod.load_settings(str(REPO_ROOT / "agent_config.json"))
        for k, v in overrides.items():
            setattr(s, k, v)
        return s
    return _make


@pytest.fixture
def db_path(tmp_path):
    """An initialized SQLite state db in a temp dir."""
    p = str(tmp_path / "state.db")
    storage.init_storage(p)
    return p
