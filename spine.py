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
    "complete or inactive workstreams": "workstreams",
    "complete/inactive workstreams": "workstreams",
    "inactive workstreams": "workstreams",
    "workstreams": "workstreams",
    "people": "people",
    "notes for the agent": "notes",
    "notes": "notes",
    "rules and thresholds": "rules",
    "rules": "rules",
    "card naming standard": "naming",
    "naming standard": "naming",
}

# Statuses that mean a workstream is finished (the "no longer needed" test treats
# its cards as archive candidates). "Complete"/"Completed" is the live page's
# wording; "Done"/"Winding down" are the older bullet-format values.
_DONE_STATUSES = frozenset({"done", "winding down", "complete", "completed"})

# Statuses that mean a workstream is finished/parked for the promotion veto: a
# card matching one of these is NEVER promoted (it routes to the archive path).
_TERMINAL_STATUSES = frozenset({"done", "complete", "completed", "paused"})

# A People-section entry is "priority-raising" when its context flags it so (the
# spine writes "more important and time-sensitive" for Logan/Alex/Hunter/etc.).
import re as _re
_PRIORITY_CUE_RE = _re.compile(r"more important|time.?sensitiv|priority[- ]?rais|priority", _re.IGNORECASE)


def _person_priority_from_text(*texts) -> bool:
    return any(_PRIORITY_CUE_RE.search(t or "") for t in texts)


@dataclass
class Workstream:
    name: str
    status: str = ""
    context: str = ""
    priority: str = "Normal"        # High / Normal / Low (default Normal)
    time_sensitive: bool = False    # from the graded "Time Sensitivity" column /
                                    # "Time-sensitive:" attr; High/Medium => True


@dataclass
class Person:
    name: str
    role: str = ""
    context: str = ""
    priority: bool = False          # People-section "priority-raising" flag


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
        return [w.name for w in self.workstreams if w.status.lower() in _DONE_STATUSES]

    def terminal_workstream_names(self) -> list[str]:
        """Workstreams that are Done/Complete/Paused — a match NEVER promotes."""
        return [w.name for w in self.workstreams if w.status.lower() in _TERMINAL_STATUSES]

    def priority_person_names(self) -> list[str]:
        """People flagged as priority-raising in the People section."""
        return [p.name for p in self.people if p.priority and p.name]

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
            time_sensitive=_time_sensitive_to_bool(w.get("time_sensitive", False)),
        )
        for w in d.get("workstreams", [])
    ]
    people = [
        Person(
            name=str(p.get("name", "")),
            role=str(p.get("role", "")),
            context=str(p.get("context", "")),
            priority=bool(p.get("priority")) or _person_priority_from_text(
                str(p.get("role", "")), str(p.get("context", ""))),
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


# --- Formatting / graded-value helpers (Notion native-table cells) ----------

import re

# Cells may arrive wrapped in inline markup (e.g. a color span:
# '<span style="color:red">High</span>'). Strip any tags before parsing.
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_markup(text: str) -> str:
    """Remove inline HTML-style tags (color spans etc.), keeping the inner text."""
    return _TAG_RE.sub("", text or "")


# Graded "Time Sensitivity" column. Higher rank == more time-sensitive; a range
# like "Medium to High" resolves to its highest grade. "Yes" is tolerated (==High).
_TS_RANK = {"no": 0, "low": 1, "medium": 2, "high": 3, "yes": 3}
_TS_WORD_RE = re.compile(r"\b(high|medium|low|no|yes)\b", re.IGNORECASE)


def _time_sensitive_to_bool(raw) -> bool:
    """Map a graded Time Sensitivity value to the time_sensitive flag.

    High/Medium (or Yes) => True; Low/No => False. Ranges ("Medium to High")
    take the highest grade present. Markup is stripped first. Values with no
    recognizable grade word fall back to yes/true/1/on parsing.
    """
    if isinstance(raw, bool):
        return raw
    text = _strip_markup(str(raw or "")).lower()
    words = _TS_WORD_RE.findall(text)
    if not words:
        return _as_bool(raw)
    return max(_TS_RANK[w] for w in words) >= 2  # Medium or High


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


# Block types that carry a section heading. Notion headings can be toggleable
# (children hang off the heading); plain toggles serve the same role on the live
# page ("Active Workstreams" / "Complete or Inactive Workstreams" toggles).
_HEADING_TYPES = ("heading_1", "heading_2", "heading_3")
_LIST_TYPES = ("bulleted_list_item", "numbered_list_item", "paragraph")


def _block_children(block: dict) -> list[dict]:
    """Return a block's child blocks, whether nested under the type payload or
    hydrated onto a top-level 'children' key (see _hydrate_children)."""
    btype = block.get("type", "")
    payload = block.get(btype, {})
    kids = payload.get("children") if isinstance(payload, dict) else None
    if not kids:
        kids = block.get("children")
    return kids or []


def _cell_text(cell) -> str:
    """Plain text of one native-table cell (a rich_text array), markup stripped."""
    if not isinstance(cell, list):
        return ""
    raw = "".join(rt.get("plain_text", "") for rt in cell if isinstance(rt, dict))
    return _strip_markup(raw).strip()


def _row_cells_text(row: dict) -> list[str]:
    cells = row.get("table_row", {}).get("cells", []) if isinstance(row, dict) else []
    return [_cell_text(c) for c in cells]


def _header_key(text: str) -> str | None:
    """Map a table header cell to a canonical workstream column key."""
    t = _strip_markup(text or "").strip().lower()
    if not t:
        return None
    if t == "name" or "workstream" in t:
        return "name"
    if "status" in t:
        return "status"
    if "time" in t and "sens" in t:
        return "time"
    if "priorit" in t:
        return "priority"
    if "context" in t:
        return "context"
    return None


def _map_header(cells: list[str]) -> dict[str, int]:
    col: dict[str, int] = {}
    for i, c in enumerate(cells):
        key = _header_key(c)
        if key and key not in col:
            col[key] = i
    return col


def _cell_at(cells: list[str], idx) -> str:
    if idx is None or not (0 <= idx < len(cells)):
        return ""
    return cells[idx]


def _parse_workstream_table(rows: list[dict], spine: SpineData) -> None:
    """Parse a Notion native table's rows into Workstream entries.

    The first row is treated as a header when its cells name known columns
    (Name/Workstream, Status, Time Sensitivity, Context, Priority); otherwise a
    positional layout (name, status, time-sensitivity, context) is assumed.
    Empty Context cells are valid; rows with no name are skipped.
    """
    rows = [r for r in rows if isinstance(r, dict) and r.get("type") == "table_row"]
    if not rows:
        return
    col = {"name": 0, "status": 1, "time": 2, "context": 3}  # positional default
    data_rows = rows
    header = _map_header(_row_cells_text(rows[0]))
    if "name" in header:  # a recognizable header row — use it and skip it
        col = header
        data_rows = rows[1:]
    for row in data_rows:
        cells = _row_cells_text(row)
        name = _cell_at(cells, col.get("name"))
        if not name:
            continue
        priority = _cell_at(cells, col.get("priority")) or "Normal"
        spine.workstreams.append(Workstream(
            name=name,
            status=_cell_at(cells, col.get("status")),
            context=_cell_at(cells, col.get("context")),
            priority=priority.capitalize(),
            time_sensitive=_time_sensitive_to_bool(_cell_at(cells, col.get("time"))),
        ))


def _consume_line(section: str | None, text: str, spine: SpineData) -> None:
    """Handle one list/paragraph line under the current section (bullet format)."""
    if section == "workstreams":
        name, status, context = _split_entry(text)
        priority, time_sensitive, context = _parse_workstream_attrs(context)
        spine.workstreams.append(Workstream(
            name=name, status=status, context=context,
            priority=priority, time_sensitive=time_sensitive))
    elif section == "people":
        name, role, context = _split_entry(text)
        spine.people.append(Person(name=name, role=role, context=context,
                                   priority=_person_priority_from_text(role, context, text)))
    elif section == "notes":
        spine.notes.append(text)
    elif section == "rules":
        key, value = _parse_rule_line(text)
        if key:
            spine.rules[key] = value
    elif section == "naming":
        spine.naming_standard.append(text)


def _walk_blocks(blocks: list[dict], spine: SpineData, section: str | None) -> None:
    """Recursively parse blocks, carrying the current section into children.

    Supports both the flat heading+bullet format and the live page's toggle +
    native-table format. A heading updates the section for its following siblings
    (and its own children when toggleable); a toggle scopes the section to its
    children only; a table under the workstreams section is parsed as workstreams.
    """
    for block in blocks:
        btype = block.get("type", "")
        text = _plain_text(block)
        children = _block_children(block)

        if btype == "table":
            if section == "workstreams":
                _parse_workstream_table(children, spine)
            continue

        if btype == "toggle":
            mapped = _SECTION_ALIASES.get(text.lower()) if text else None
            child_section = mapped if mapped is not None else section
            if children:
                _walk_blocks(children, spine, child_section)
            continue

        if btype in _HEADING_TYPES:
            if text:
                section = _SECTION_ALIASES.get(text.lower())
            if children:  # toggleable heading — its content hangs off it
                _walk_blocks(children, spine, section)
            continue

        if btype in _LIST_TYPES:
            if text:
                _consume_line(section, text, spine)
            if children:
                _walk_blocks(children, spine, section)
            continue

        # Structural wrappers (column_list, synced_block, etc.) — descend.
        if children:
            _walk_blocks(children, spine, section)


def parse_spine_blocks(blocks: list[dict]) -> SpineData:
    """Parse Notion block dicts into SpineData by section heading/toggle."""
    spine = SpineData()
    _walk_blocks(blocks, spine, None)
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


def _hydrate_children(notion_client, blocks: list[dict], depth: int = 0) -> list[dict]:
    """Recursively fetch and embed children for blocks that have them.

    The Notion blocks API returns only a block's direct children per call and
    flags deeper content with has_children. Toggles and tables keep their content
    (the workstream rows) one level down, so hydrate the tree before parsing.
    Bounded depth guards against pathological nesting; failures are logged, never
    fatal (a section that can't be read is simply skipped).
    """
    if depth > 5:
        return blocks
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("has_children") and "children" not in b:
            try:
                kids = notion_client.get_block_children(b.get("id", ""))
            except Exception as exc:  # one unreadable branch must not fail the run
                logger.warning("Could not read children of block %s (%s)", b.get("id"), exc)
                continue
            b["children"] = kids
            _hydrate_children(notion_client, kids, depth + 1)
    return blocks


def read_spine(notion_client, page_id: str) -> SpineData:
    """Fetch the spine page's blocks from Notion and parse them.

    Args:
        notion_client: an agent_shared NotionClient.
        page_id: the spine page id (config spine_page_id).

    Returns:
        SpineData parsed from the page.
    """
    blocks = notion_client.get_block_children(page_id)
    _hydrate_children(notion_client, blocks)
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
    "automatic_action_confidence": "int",
    "auto_pause_after_failures": "int",
    "name_min_length": "int",
    "name_max_length": "int",
    "wide_block_jaccard": "float",
    "narrow_hint_jaccard": "float",
    # Automatic-mode toggles: accept "automatic"/"proposed" or bool.
    "time_label_fix_mode": "mode",
    "archive_mode": "mode",
    "due_date_fix_mode": "mode",
    # Reprioritization pass (Problem 5).
    "reprioritization_mode": "mode",
    "time_reprioritization_confidence": "int",
    "today_list_target": "int",
    "next_few_days_target": "int",
    "max_reprioritization_moves_per_run": "int",
    "run_mode": "run_mode",
    "limited_test_actions_per_type": "int",
    # NOTE: demotion_exempt_hours, priority_labels, reprioritization_due_days are
    # file-only config (not exposed as Notion Rules keys) — the spine states the
    # 48h placement rule as prose, so keeping them off the parser avoids drift.
    "dry_run": "bool",
    "weekly_sweep_day": "weekday",
    "spine_review_day": "review_day",
    "archive_list_name": "str",
    # ---- Pre-rename aliases: recognized, coerced, applied to the new attr ----
    "tier1_stale_label_removal": "mode",
    "tier1_recovery_archive": "mode",
    "tier1_due_date_clear": "mode",
    "auto_min_confidence": "int",
}

_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}
# Mode words: "automatic" == auto-execute (True), "proposed" == flag only (False).
_MODE_TRUE = {"automatic", "auto", "true", "yes", "1", "on"}
_MODE_FALSE = {"proposed", "propose", "false", "no", "0", "off"}


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
        if typ == "mode":
            # "automatic"/"proposed" (or bool aliases) -> bool (True == automatic).
            low = token.lower()
            if low in _MODE_TRUE:
                return True
            if low in _MODE_FALSE:
                return False
            return None
        if typ == "run_mode":
            low = token.lower()
            return low if low in ("dry_run", "limited_test", "live") else None
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
        # Render mode booleans as their config words (item 5: never True/False).
        shown = ("automatic" if val else "proposed") if _OVERRIDE_TYPES[key] == "mode" else val
        notes.append(f"Notion Rules: '{key}' set to {shown} (from spine)")
        if key == "dry_run" and val is True:
            dry_run_from_notion = True
    logger.info("Applied %d Notion Rules note(s)", len(notes))
    return notes, dry_run_from_notion
