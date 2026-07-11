"""Section 11 — LLM output schema validation, name grounding, timeout fallback."""

from unittest.mock import MagicMock

from agent_shared.llm.client import LLMClient
from agent_shared.models import LLMResponse

from phases import judgment as judge


def test_llm_valid_json_parsed():
    obj = judge.parse_json('{"verdicts": [{"card_id": "a"}]}')
    assert obj == {"verdicts": [{"card_id": "a"}]}


def test_llm_markdown_fenced_json_recovered():
    obj = judge.parse_json('```json\n{"a": 1}\n```')
    assert obj == {"a": 1}


def test_llm_trailing_prose_json_recovered():
    obj = judge.parse_json('Here you go: {"a": 1} — done.')
    assert obj == {"a": 1}


def test_llm_invalid_json_dropped_and_logged():
    assert judge.parse_json("not json at all") is None


def test_llm_unknown_card_id_item_dropped():
    items = [{"card_id": "known"}, {"card_id": "ghost"}]
    valid, dropped = judge.validate_items(items, {"known"}, ["card_id"], ["card_id"])
    assert [v["card_id"] for v in valid] == ["known"]
    assert [d["card_id"] for d in dropped] == ["ghost"]


def test_llm_item_missing_required_key_dropped():
    valid, dropped = judge.validate_items([{"foo": 1}], {"known"}, ["card_id"], ["card_id"])
    assert valid == [] and len(dropped) == 1


def test_llm_partial_batch_valid_items_kept():
    items = [{"card_id": "known", "disposition": "inbox"}, {"card_id": "ghost"}, "not-a-dict"]
    valid, dropped = judge.validate_items(items, {"known"}, ["card_id"], ["card_id"])
    assert len(valid) == 1 and len(dropped) == 2


def test_llm_extract_items_from_dict_or_list():
    assert judge.extract_items({"verdicts": [1, 2]}, "verdicts") == [1, 2]
    assert judge.extract_items([1, 2], "verdicts") == [1, 2]
    assert judge.extract_items({"other": 1}, "verdicts") == []


def test_llm_name_not_in_source_rejected(spine):
    item = {"new_name": "Send comp plan to Zephyr"}
    assert judge.ground_name(item, "new_name", ["Send RSM comp plan to Colin"], spine.all_terms()) is False


def test_llm_grounded_name_accepted(spine):
    item = {"new_name": "Send comp plan to Colin"}
    assert judge.ground_name(item, "new_name", ["Send RSM comp plan to Colin"], spine.all_terms()) is True


def test_llm_timeout_falls_back_to_ollama():
    """Primary raises builtin TimeoutError; fallback runs on identical inputs."""
    llm = LLMClient(anthropic_api_key="key", ollama_host="http://x", ollama_model="qwen3:8b",
                    anthropic_model="claude-sonnet-4-6")
    seen = {}

    def fake_anthropic(prompt, system, max_tokens, temperature, cache):
        seen["anthropic_prompt"] = prompt
        raise TimeoutError("boom")

    def fake_ollama(prompt, system, max_tokens, temperature):
        seen["ollama_prompt"] = prompt
        return LLMResponse(text='{"verdicts": []}', provider_used="ollama", model="qwen3:8b")

    llm._call_anthropic = fake_anthropic
    llm._call_ollama = fake_ollama

    text = judge.call_llm(llm, "PROMPT", "SYSTEM")
    assert judge.parse_json(text) == {"verdicts": []}
    # Same unfiltered prompt reached both providers.
    assert seen["anthropic_prompt"] == seen["ollama_prompt"]
