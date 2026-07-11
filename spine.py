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
}


@dataclass
class Workstream:
    name: str
    status: str = ""
    context: str = ""


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

    def person_names(self) -> list[str]:
        return [p.name for p in self.people if p.name]

    def workstream_names(self) -> list[str]:
        return [w.name for w in self.workstreams if w.name]

    def workstream_status(self, name: str) -> str | None:
        for w in self.workstreams:
            if w.name.lower() == name.lower():
                return w.status
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
    return SpineData(workstreams=workstreams, people=people, notes=notes)


# ---------------------------------------------------------------------------
# Runtime: read + parse Notion blocks
# ---------------------------------------------------------------------------

def _plain_text(block: dict) -> str:
    """Concatenate rich-text plain_text for a block's type payload."""
    btype = block.get("type", "")
    payload = block.get(btype, {})
    rich = payload.get("rich_text", []) if isinstance(payload, dict) else []
    return "".join(rt.get("plain_text", "") for rt in rich).strip()


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
                spine.workstreams.append(Workstream(name=name, status=status, context=context))
            elif section == "people":
                name, role, context = _split_entry(text)
                spine.people.append(Person(name=name, role=role, context=context))
            elif section == "notes":
                spine.notes.append(text)
    return spine


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
    logger.info(
        "Read spine: %d workstreams, %d people, %d notes",
        len(spine.workstreams), len(spine.people), len(spine.notes),
    )
    return spine
