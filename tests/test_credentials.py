"""
Notion token resolution in load_credentials (agent-specific key + fallback).

The agent's Notion token lives at notion.integration_token_work_trello_grooming_agent;
notion.integration_token is the shared fallback. Neither present → fail loudly
naming both paths. Mirrors the nested anthropic_api_keys lookup. The config file is
mocked (no real .env.json read).
"""

from __future__ import annotations

import logging

import pytest

import settings as settings_mod
from settings import CredentialsError, load_credentials

_BASE = {
    "trello": {"api_key": "k", "token": "t"},
    "gmail_sender": "s@x", "gmail_password": "p", "ollama_endpoint": "http://ollama",
    "anthropic_api_keys": {"work-todo-trello-grooming-agent": "sk-agent"},
}


def _mock_env(monkeypatch, notion: dict):
    data = dict(_BASE)
    data["notion"] = notion
    monkeypatch.setattr(settings_mod, "load_env_config", lambda config_path=None: data)


def test_notion_token_prefers_agent_specific(monkeypatch, caplog):
    _mock_env(monkeypatch, {"integration_token_work_trello_grooming_agent": "AGENT",
                            "integration_token": "SHARED"})
    with caplog.at_level(logging.INFO):
        creds = load_credentials("ignored.json")
    assert creds.notion_token == "AGENT"
    # Logs the KEY selected, never the token value.
    assert "notion.integration_token_work_trello_grooming_agent" in caplog.text
    assert "AGENT" not in caplog.text


def test_notion_token_falls_back_to_shared(monkeypatch, caplog):
    _mock_env(monkeypatch, {"integration_token": "SHARED"})
    with caplog.at_level(logging.INFO):
        creds = load_credentials("ignored.json")
    assert creds.notion_token == "SHARED"
    assert "notion.integration_token" in caplog.text
    assert "SHARED" not in caplog.text


def test_notion_token_missing_both_fails_loudly(monkeypatch):
    _mock_env(monkeypatch, {"some_other_key": "x"})
    with pytest.raises(CredentialsError) as exc:
        load_credentials("ignored.json")
    msg = str(exc.value)
    assert "integration_token_work_trello_grooming_agent" in msg
    assert "integration_token" in msg


def test_notion_section_missing_entirely_fails_loudly(monkeypatch):
    data = dict(_BASE)  # no "notion" key at all
    monkeypatch.setattr(settings_mod, "load_env_config", lambda config_path=None: data)
    with pytest.raises(CredentialsError):
        load_credentials("ignored.json")
