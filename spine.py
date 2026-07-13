"""
Notion context spine reader.

The spine ('Trello Grooming Agent Spine') is world state read at run start:
Active Workstreams (name/status/context), People (name/role/context), and
standing Notes for the agent. It is human-edited in P0 (the agent only reads).

Two entry points:
  - load_spine_from_dict(d): build SpineData from a plain dict (tests / cache).
  - read_spine(notion_client, page_id): fetch the Notion page blocks and parse
    them into the same SpineData (runtime).

Person names from the People section are appended to the agent's
entity_keywords_seed at blocking time (see candidates.py); all spine terms feed
LLM-name grounding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_SECTION_ALIASES = {
    "active workstreams": "workstreams",
    "workstreams": "workstreams",
    "people": "people",
    "notes for the agent": "notes",
    "notes": "notes",
    "rules and thresholds": "rules",
    "rules": "rules",
    "card naming standard": "naming",
    "naming standard": "naming",
}


@dataclass
class Workstream:
    name: str
    status: str = ""
    context: str = ""
    priority: str = "Normal"        # High / Normal / Low (default Normal)
    time_sensitive: bool = False    # from "Time-sensitive: Yes/No" (default No)


@dataclass
class Person:
    name: str
    role: str = ""
    context: str = ""


@dataclass
class SpineData:
    workstreams: list[Workstream] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Live config overrides parsed from the "Rules and thresholds" section:
    # {config_key: raw_value_string} (raw string kept; typed/validated at apply).
    rules: dict[str, str] = field(default_factory=dict)
    # Free-text lines from the "Card naming standard" section (fed to the LLM).
    naming_standard: list[str] = field(default_factory=list)
    # The spine page URL (put in the weekly review-reminder card's description).
    page_url: str = ""

    def active_workstream_names(self) -> list[str]:
        return [w.name for w in self.workstreams if w.status.lower() == "active"]

    def person_names(self) -> list[str]:
        return [p.name for p in self.people if p.name]

    def workstream_names(self) -> list[str]:
        return [w.name for w in self.workstreams if w.name]

    def workstream_status(self, name: str) -> str | None:
        for w in self.workstreams:
            if w.name.lower() == name.lower():
                return w.status
        return None

    def workstream(self, name: str) -> "Workstream | None":
        for w in self.workstreams:
            if w.name.lower() == name.lower():
                return w
        return None

    def done_workstream_names(self) -> list[str]:
        return [w.name for w in self.workstreams if w.status.lower() in ("done", "winding down")]

    def all_terms(self) -> list[str]:
        """Every spine text fragment, for LLM-name grounding."""
        terms: list[str] = []
        for w in self.workstreams:
            terms += [w.name, w.context]
        for p in self.people:
            terms += [p.name, p.role, p.context]
        terms += self.notes
        return [t for t in terms if t]


def load_spine_from_dict(d: dict) -> SpineData:
    """Build SpineData from a plain dict (spine.json fixture shape)."""
    workstreams = [
        Workstream(
            name=str(w.get("name", "")),
            status=str(w.get("status", "")),
            context=str(w.get("context", "")),
            priority=str(w.get("priority", "Normal")) or "Normal",
            time_sensitive=_as_bool(w.get("time_sensitive", False)),
        )
        for w in d.get("workstreams", [])
    ]
    people = [
        Person(
            name=str(p.get("name", "")),
            role=str(p.get("role", "")),
            context=str(p.get("context", "")),
        )
        for p in d.get("people", [])
    ]
    notes = [str(n) for n in d.get("notes", [])]
    rules = {str(k): str(v) for k, v in d.get("rules", {}).items()}
    naming = [str(n) for n in d.get("naming_standard", [])]
    return SpineData(
        workstreams=workstreams, people=people, notes=notes,
        rules=rules, naming_standard=naming, page_url=str(d.get("page_url", "")),
    )


# ---------------------------------------------------------------------------
# Runtime: read + parse Notion blocks
# ---------------------------------------------------------------------------

def _plain_text(block: dict) -> str:
    """Concatenate rich-text plain_text for a block's type payload."""
    btype = block.get("type", "")
    payload = block.get(btype, {})
    rich = payload.get("rich_text", []) if isinstance(payload, dict) else []
    return "".join(rt.get("plain_text", "") for rt in rich).strip()


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "true", "1", "on")


_ATTR_RE = None  # compiled lazily below


def _parse_workstream_attrs(context: str) -> tuple[str, bool, str]:
    """Extract 'Priority: X' and 'Time-sensitive: Y' from a workstream's context.

    Returns (priority, time_sensitive, remaining_context). Defaults: Normal / No.
    The matched attribute clauses are stripped from the returned context.
    """
    import re

    priority = "Normal"
    time_sensitive = False
    remaining = context or ""

    m = re.search(r"priority\s*:\s*(high|normal|low)", remaining, re.IGNORECASE)
    if m:
        priority = m.group(1).capitalize()
        remaining = remaining[: m.start()] + remaining[m.end():]
    m = re.search(r"time[\s-]*sensitive\s*:\s*(yes|no|true|false)", remaining, re.IGNORECASE)
    if m:
        time_sensitive = m.group(1).lower() in ("yes", "true")
        remaining = remaining[: m.start()] + remaining[m.end():]
    remaining = remaining.strip(" .;—-").strip()
    return priority, time_sensitive, remaining


def _split_entry(text: str) -> tuple[str, str, str]:
    """Parse 'Name — status — context' / 'Name (status): context' style bullets."""
    name, mid, rest = text, "", ""
    if "—" in text:
        parts = [p.strip() for p in text.split("—")]
        name = parts[0]
        if len(parts) >= 2:
            mid = parts[1]
        if len(parts) >= 3:
            rest = " — ".join(parts[2:])
        return name, mid, rest
    if "(" in text and ")" in text:
        name = text.split("(", 1)[0].strip()
        mid = text.split("(", 1)[1].split(")", 1)[0].strip()
        rest = text.split(")", 1)[1].lstrip(": ").strip()
        return name, mid, rest
    if ":" in text:
        name, rest = [p.strip() for p in text.split(":", 1)]
        return name, "", rest
    return name.strip(), "", ""


def parse_spine_blocks(blocks: list[dict]) -> SpineData:
    """Parse Notion block dicts into SpineData by section heading."""
    spine = SpineData()
    section = None
    for block in blocks:
        btype = block.get("type", "")
        text = _plain_text(block)
        if not text:
            continue
        if btype.startswith("heading"):
            section = _SECTION_ALIASES.get(text.lower())
            continue
        if btype in ("bulleted_list_item", "numbered_list_item", "paragraph"):
            if section == "workstreams":
                name, status, context = _split_entry(text)
                priority, time_sensitive, context = _parse_workstream_attrs(context)
                spine.workstreams.append(Workstream(
                    name=name, status=status, context=context,
                    priority=priority, time_sensitive=time_sensitive))
            elif section == "people":
                name, role, context = _split_entry(text)
                spine.people.append(Person(name=name, role=role, context=context))
            elif section == "notes":
                spine.notes.append(text)
            elif section == "rules":
                key, value = _parse_rule_line(text)
                if key:
                    spine.rules[key] = value
            elif section == "naming":
                spine.naming_standard.append(text)
    return spine


def _parse_rule_line(text: str) -> tuple[str, str]:
    """Parse a '- key: value  (trailing prose)' rule line into (key, raw_value).

    key is lowercased/stripped; raw_value is everything after the first colon,
    stripped (trailing prose is left in place and ignored by numeric/bool/day
    coercion, which reads only the first token).
    """
    if ":" not in text:
        return "", ""
    left, right = text.split(":", 1)
    return left.strip().lower(), right.strip()


def read_spine(notion_client, page_id: str) -> SpineData:
    """Fetch the spine page's blocks from Notion and parse them.

    Args:
        notion_client: an agent_shared NotionClient.
        page_id: the spine page id (config spine_page_id).

    Returns:
        SpineData parsed from the page.
    """
    blocks = notion_client.get_block_children(page_id)
    spine = parse_spine_blocks(blocks)
    try:
        page = notion_client.get_page(page_id)
        spine.page_url = str(page.get("url", "")) if isinstance(page, dict) else ""
    except Exception as exc:  # page-url read is best-effort; never fatal
        logger.warning("Could not read spine page URL (%s); continuing", exc)
    logger.info(
        "Read spine: %d workstreams, %d people, %d notes, %d rule(s), %d naming line(s)",
        len(spine.workstreams), len(spine.people), len(spine.notes),
        len(spine.rules), len(spine.naming_standard),
    )
    return spine


# ---------------------------------------------------------------------------
# Live config overrides from the spine "Rules and thresholds" section
# ---------------------------------------------------------------------------

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# config_key -> coercion type. Attribute names on AgentSettings match these keys.
_OVERRIDE_TYPES: dict[str, str] = {
    "recovery_batch_size": "int",
    "recovery_today_max": "int",
    "archive_list_days": "int",
    "proposal_timeout_days": "int",
    "no_touch_hours": "int",
    "dead_due_days": "int",
    "optimistic_label_days": "int",
    "max_merges_per_run": "int",
    "max_renames_per_run": "int",
    "max_recoveries_per_run": "int",
    "max_inscope_archives_per_run": "int",
    "max_proposals_open": "int",
    "auto_min_confidence": "int",
    "auto_pause_after_failures": "int",
    "name_min_length": "int",
    "name_max_length": "int",
    "wide_block_jaccard": "float",
    "narrow_hint_jaccard": "float",
    "tier1_stale_label_removal": "bool",
    "tier1_recovery_archive": "bool",
    "tier1_due_date_clear": "bool",
    "dry_run": "bool",
    "weekly_sweep_day": "weekday",
    "spine_review_day": "review_day",
    "archive_list_name": "str",
}

_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def _coerce(typ: str, raw: str):
    """Coerce a raw rule value string to the target type, or None if invalid."""
    token = raw.split()[0] if raw.split() else ""
    try:
        if typ == "int":
            return int(token)
        if typ == "float":
            return float(token)
        if typ == "bool":
            low = token.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            return None
        if typ == "weekday":
            return token.lower() if token.lower() in _WEEKDAYS else None
        if typ == "review_day":
            low = token.lower()
            return low if (low in _WEEKDAYS or low == "off") else None
        if typ == "str":
            return raw.strip() or None
    except (ValueError, TypeError):
        return None
    return None


def apply_notion_overrides(settings, spine) -> tuple[list[str], bool]:
    """Apply spine "Rules and thresholds" overrides to settings (in place).

    Returns (notes, dry_run_from_notion):
      - notes: report lines describing each override / ignored key.
      - dry_run_from_notion: True if Notion set dry_run: true.

    Valid values override the file for this run; invalid values and unknown keys
    are ignored with a note; a missing section (empty rules) or unreadable spine
    (spine is None) falls back silently to the file. Each candidate override is
    validated with the full settings validator and reverted if it fails.
    """
    from settings import _validate_settings  # local import avoids load-order issues

    notes: list[str] = []
    dry_run_from_notion = False
    if spine is None or not getattr(spine, "rules", None):
        return notes, dry_run_from_notion

    for key, raw in spine.rules.items():
        if key not in _OVERRIDE_TYPES:
            notes.append(f"Notion Rules: unknown key '{key}' ignored")
            continue
        val = _coerce(_OVERRIDE_TYPES[key], raw)
        if val is None:
            notes.append(f"Notion Rules: invalid value for '{key}' ({raw!r}) ignored")
            continue
        prev = getattr(settings, key)
        setattr(settings, key, val)
        try:
            _validate_settings(settings)
        except Exception as exc:  # invalid combination — revert and note
            setattr(settings, key, prev)
            notes.append(f"Notion Rules: '{key}'={val} rejected by validation ({exc}); ignored")
            continue
        notes.append(f"Notion Rules: '{key}' set to {val} (from spine)")
        if key == "dry_run" and val is True:
            dry_run_from_notion = True
    logger.info("Applied %d Notion Rules note(s)", len(notes))
    return notes, dry_run_from_notion
