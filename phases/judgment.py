"""
Phase 3 — LLM judgment (Sonnet primary, Ollama fallback), schema-validated.

Three batched calls share a cached prefix (spine + board summary + rules). All
facts (card ids, list/label names, counts) are computed in Python and passed as
constraints; the LLM judges. Every response is parsed as JSON and schema-checked
in Python: items that are malformed, missing required keys, or reference unknown
card ids are dropped and logged — never partially applied. LLM-generated names
are grounded against source card text + spine before any write.

The shared LLMClient already falls back Anthropic -> Ollama on any exception
(including builtin TimeoutError). call_llm additionally catches builtin
TimeoutError and LLMUnavailableError explicitly so a total LLM outage degrades
to "skip this batch" rather than crashing the run.
"""

from __future__ import annotations

import json
import logging
import re

from agent_shared.llm.client import LLMJSONParseError, LLMUnavailableError

from guardrails import name_is_grounded

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)


# ---------------------------------------------------------------------------
# LLM call wrapper
# ---------------------------------------------------------------------------

def call_llm(llm, prompt: str, system_prompt: str, max_tokens: int = 2000):
    """Call the LLM (JSON output, cached prefix). Returns text or None on outage.

    The shared client handles the Anthropic->Ollama fallback with identical
    unfiltered inputs. We additionally catch builtin TimeoutError and
    LLMUnavailableError so a total outage skips the batch instead of raising.
    """
    try:
        resp = llm.call(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=0.2,
            cache_system_prompt=True,
            json_output=True,
        )
        return resp.text
    except LLMJSONParseError as exc:
        logger.error("LLM returned unparseable JSON; dropping batch. raw=%r", exc.raw_text[:500])
        return None
    except (TimeoutError, LLMUnavailableError) as exc:
        logger.error("LLM unavailable (%s: %s); skipping batch", type(exc).__name__, exc)
        return None


# ---------------------------------------------------------------------------
# JSON parsing + schema validation
# ---------------------------------------------------------------------------

def parse_json(text: str):
    """Parse LLM text to a Python object, tolerating markdown fences / prose.

    Returns the parsed object, or None if no valid JSON can be extracted.
    """
    if text is None:
        return None
    cleaned = _FENCE_RE.sub("", text).strip().strip("`").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Trailing/leading prose: grab the outermost JSON object or array.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue
    logger.error("Could not parse JSON from LLM response: %r", text[:500])
    return None


def extract_items(obj, list_key: str) -> list:
    """Return a list of items from a parsed object (dict[list_key] or a raw list)."""
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        val = obj.get(list_key)
        if isinstance(val, list):
            return val
    return []


def validate_items(items: list, known_ids: set[str], required_keys, id_fields) -> tuple[list, list]:
    """Split items into (valid, dropped).

    An item is dropped if it is not a dict, is missing a required key, or names a
    card id (in any of id_fields) that is not on the board. id_fields values may
    be a string id or a list of ids.
    """
    valid, dropped = [], []
    for item in items:
        if not isinstance(item, dict):
            dropped.append(item)
            logger.warning("Dropping non-dict LLM item: %r", item)
            continue
        if any(k not in item for k in required_keys):
            dropped.append(item)
            logger.warning("Dropping LLM item missing required keys: %r", item)
            continue
        bad_id = False
        for field in id_fields:
            v = item.get(field)
            ids = v if isinstance(v, list) else ([v] if v is not None else [])
            for cid in ids:
                if cid not in known_ids:
                    bad_id = True
                    logger.warning("Dropping LLM item with unknown card id %r: %r", cid, item)
                    break
            if bad_id:
                break
        if bad_id:
            dropped.append(item)
            continue
        valid.append(item)
    if dropped:
        logger.info("Schema validation dropped %d/%d LLM item(s)", len(dropped), len(items))
    return valid, dropped


def ground_name(item: dict, name_key: str, source_texts, spine_terms) -> bool:
    """Validate an LLM-proposed name against source + spine; True if grounded.

    On failure the caller drops the rename/merge-name (falls back to a safe title).
    """
    name = item.get(name_key)
    if not name:
        return True
    return name_is_grounded(name, source_texts, spine_terms)


# ---------------------------------------------------------------------------
# Prompt prefix
# ---------------------------------------------------------------------------

def build_system_prefix(prompt_loader, spine, board_summary: str, rules_text: str) -> str:
    """Assemble the cached shared prefix: spine + board summary + rules."""
    spine_text = _render_spine(spine)
    try:
        return prompt_loader.load(
            "shared_prefix.md",
            {"spine": spine_text, "board_summary": board_summary, "rules": rules_text},
        )
    except FileNotFoundError:
        return f"{rules_text}\n\nSPINE:\n{spine_text}\n\nBOARD:\n{board_summary}"


def _render_spine(spine) -> str:
    if spine is None:
        return "(no spine)"
    lines = ["Active Workstreams:"]
    for w in spine.workstreams:
        lines.append(f"- {w.name} [{w.status}]: {w.context}")
    lines.append("People:")
    for p in spine.people:
        lines.append(f"- {p.name} ({p.role}): {p.context}")
    lines.append("Notes:")
    for n in spine.notes:
        lines.append(f"- {n}")
    naming = getattr(spine, "naming_standard", None)
    if naming:
        lines.append("Card naming standard (follow these when renaming):")
        for n in naming:
            lines.append(f"- {n}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The three judgment passes
# ---------------------------------------------------------------------------

def adjudicate_clusters(llm, prompt_loader, system_prefix, clusters_payload, known_ids,
                        source_text_by_id, spine_terms):
    """Cluster adjudication. Returns validated verdict dicts.

    Each verdict: {relation, cluster_ids, survivor_id, new_name, merged_desc, llm_tier, reason}.
    Malformed/unknown-id items dropped; ungrounded names cleared (fall back later).
    """
    prompt = prompt_loader.load("cluster.md", {"clusters_json": json.dumps(clusters_payload)})
    text = call_llm(llm, prompt, system_prefix)
    items = extract_items(parse_json(text), "verdicts")
    valid, _ = validate_items(items, known_ids, ["relation", "cluster_ids"], ["cluster_ids", "survivor_id"])
    out = []
    for v in valid:
        srcs = [source_text_by_id.get(cid, "") for cid in v.get("cluster_ids", [])]
        if not ground_name(v, "new_name", srcs, spine_terms):
            v["new_name"] = None  # drop invented name; execute uses survivor's own
        out.append(v)
    return out


def hygiene_pass(llm, prompt_loader, system_prefix, hygiene_payload, known_ids,
                 source_text_by_id, spine_terms):
    """Hygiene pass. Returns validated verdicts.

    Each verdict: {card_id, new_name?, new_desc?, clear_due?, remove_labels?}.
    """
    prompt = prompt_loader.load("hygiene.md", {"hygiene_json": json.dumps(hygiene_payload)})
    text = call_llm(llm, prompt, system_prefix)
    items = extract_items(parse_json(text), "edits")
    valid, _ = validate_items(items, known_ids, ["card_id"], ["card_id"])
    out = []
    for v in valid:
        src = [source_text_by_id.get(v["card_id"], "")]
        if v.get("new_name") and not ground_name(v, "new_name", src, spine_terms):
            v["new_name"] = None
        out.append(v)
    return out


def recovery_triage(llm, prompt_loader, system_prefix, recovery_payload, known_ids):
    """Recovery triage. Returns validated verdicts.

    Each verdict: {card_id, disposition, merge_into?, reason}. disposition ∈
    {today, next_few_days, this_week, inbox, merge, archive}.
    """
    prompt = prompt_loader.load("recovery.md", {"recovery_json": json.dumps(recovery_payload)})
    text = call_llm(llm, prompt, system_prefix)
    items = extract_items(parse_json(text), "dispositions")
    valid, _ = validate_items(items, known_ids, ["card_id", "disposition"], ["card_id"])
    return valid
