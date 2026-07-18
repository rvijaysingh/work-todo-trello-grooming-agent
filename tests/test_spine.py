"""Spine parsing: bullet-format attributes and the live page's toggle + native
Notion table format (Active / Complete-or-Inactive Workstreams tables)."""

import pytest

from spine import load_spine_from_dict, parse_spine_blocks, read_spine


def _heading(text):
    return {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": text}]}}


def _bullet(text):
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": text}]}}


# ── Native-table block builders (Notion table_row cells = rich_text arrays) ──

def _rt(text):
    return [{"plain_text": text}]


def _row(cells):
    return {"type": "table_row", "table_row": {"cells": [_rt(c) for c in cells]}}


def _table(rows):
    return {"type": "table", "table": {}, "children": rows}


def _toggle(text, children):
    return {"type": "toggle", "toggle": {"rich_text": [{"plain_text": text}]},
            "children": children}


_HEADER = ["Workstream", "Status", "Time Sensitivity", "Context"]


def test_parse_priority_and_time_sensitive_from_blocks():
    blocks = [
        _heading("Active Workstreams"),
        _bullet("RSM comp — Active — Comp plan. Priority: High. Time-sensitive: Yes"),
        _bullet("Forge — Winding down — Wrapping up"),
    ]
    s = parse_spine_blocks(blocks)
    rsm = s.workstream("RSM comp")
    assert rsm.priority == "High" and rsm.time_sensitive is True
    assert "Priority" not in rsm.context and "Time-sensitive" not in rsm.context
    forge = s.workstream("Forge")
    assert forge.priority == "Normal" and forge.time_sensitive is False  # defaults


def test_defaults_when_attributes_absent():
    s = load_spine_from_dict({"workstreams": [{"name": "X", "status": "Active", "context": "c"}]})
    w = s.workstream("X")
    assert w.priority == "Normal" and w.time_sensitive is False


def test_dict_attributes_parsed():
    s = load_spine_from_dict({"workstreams": [
        {"name": "Y", "status": "Active", "context": "c", "priority": "Low", "time_sensitive": "Yes"}]})
    w = s.workstream("Y")
    assert w.priority == "Low" and w.time_sensitive is True


def test_dict_time_sensitive_graded_string():
    # Graded values (not just Yes/No) map through the same rule for the dict path.
    s = load_spine_from_dict({"workstreams": [
        {"name": "M", "status": "Active", "context": "c", "time_sensitive": "Medium"},
        {"name": "L", "status": "Active", "context": "c", "time_sensitive": "Low"}]})
    assert s.workstream("M").time_sensitive is True
    assert s.workstream("L").time_sensitive is False


# ── Native-table format (item 1: parse tables under both toggles) ────────────

def test_native_table_under_both_toggles_parsed():
    active = _toggle("Active Workstreams", [_table([
        _row(_HEADER),
        _row(["RSM comp", "Active", "High", "Comp plan with Colin"]),
        _row(["Q3 objectives", "Active", "Low", "Forecast"]),
    ])])
    complete = _toggle("Complete or Inactive Workstreams", [_table([
        _row(_HEADER),
        _row(["Sales Summit", "Complete", "No", "Event wrapped"]),
    ])])
    s = parse_spine_blocks([active, complete])
    assert {w.name for w in s.workstreams} == {"RSM comp", "Q3 objectives", "Sales Summit"}
    assert s.workstream("RSM comp").status == "Active"
    assert s.workstream("RSM comp").time_sensitive is True
    assert s.workstream("Sales Summit").context == "Event wrapped"
    # Rows from the second toggle land in the same workstreams list.
    assert "Sales Summit" in s.done_workstream_names()


def test_columns_mapped_by_header_not_position():
    # Reordered columns are still read correctly because mapping is header-driven.
    tbl = _toggle("Active Workstreams", [_table([
        _row(["Status", "Time Sensitivity", "Workstream", "Context"]),
        _row(["Active", "High", "RSM comp", "Comp plan"]),
    ])])
    w = parse_spine_blocks([tbl]).workstream("RSM comp")
    assert w.status == "Active" and w.time_sensitive is True and w.context == "Comp plan"


def test_toggleable_heading_form_also_parsed():
    # The section marker can be a toggleable heading rather than a plain toggle.
    head = {"type": "heading_2",
            "heading_2": {"rich_text": [{"plain_text": "Active Workstreams"}],
                          "is_toggleable": True},
            "children": [_table([_row(_HEADER), _row(["W", "Active", "High", "c"])])]}
    assert parse_spine_blocks([head]).workstream("W").time_sensitive is True


# ── Item 2: graded Time Sensitivity column ──────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("High", True),
    ("Medium", True),
    ("Low", False),
    ("No", False),
    ("Yes", True),               # tolerated legacy value
    ("Medium to High", True),    # range → higher grade (High)
    ("Low to Medium", True),     # range → higher grade (Medium) still time-sensitive
    ("Low to No", False),        # range → higher grade (Low)
    ("", False),                 # empty cell → not time-sensitive
])
def test_time_sensitivity_grades(value, expected):
    tbl = _toggle("Active Workstreams", [_table([
        _row(_HEADER), _row(["W", "Active", value, "c"])])])
    assert parse_spine_blocks([tbl]).workstream("W").time_sensitive is expected


# ── Item 3: Complete / Completed status counts as Done ──────────────────────

@pytest.mark.parametrize("status", ["Complete", "Completed", "Done", "Winding down"])
def test_finished_status_counts_as_done(status):
    tbl = _toggle("Complete or Inactive Workstreams", [_table([
        _row(_HEADER), _row(["Old WS", status, "No", "wrapped"])])])
    assert "Old WS" in parse_spine_blocks([tbl]).done_workstream_names()


def test_active_status_not_counted_as_done():
    tbl = _toggle("Active Workstreams", [_table([
        _row(_HEADER), _row(["Live WS", "Active", "High", "ongoing"])])])
    assert "Live WS" not in parse_spine_blocks([tbl]).done_workstream_names()


# ── Item 4: strip color-span / inline markup before parsing ─────────────────

def test_color_span_markup_stripped():
    tbl = _toggle("Active Workstreams", [_table([
        _row(['<span style="color:blue">Workstream</span>', "Status",
              "Time Sensitivity", "Context"]),
        _row(['<span style="color:red">RSM comp</span>',
              '<span style="color:green">Active</span>',
              '<span style="color:orange">High</span>',
              'Comp <b>plan</b>']),
    ])])
    w = parse_spine_blocks([tbl]).workstream("RSM comp")
    assert w is not None
    assert w.status == "Active"
    assert w.time_sensitive is True
    assert w.context == "Comp plan"


# ── Item 5: empty Context cells are valid ───────────────────────────────────

def test_empty_context_cell_valid():
    tbl = _toggle("Active Workstreams", [_table([
        _row(_HEADER), _row(["RSM comp", "Active", "High", ""])])])
    w = parse_spine_blocks([tbl]).workstream("RSM comp")
    assert w is not None and w.context == "" and w.time_sensitive is True


def test_row_without_name_skipped():
    tbl = _toggle("Active Workstreams", [_table([
        _row(_HEADER),
        _row(["", "Active", "High", "orphan context"]),
        _row(["RSM comp", "Active", "Low", "real"]),
    ])])
    s = parse_spine_blocks([tbl])
    assert [w.name for w in s.workstreams] == ["RSM comp"]


# ── Runtime path: read_spine hydrates toggle + table children ───────────────

class _FakeNotion:
    """Minimal NotionClient stand-in: children keyed by block id."""

    def __init__(self, children_by_id):
        self._children = children_by_id

    def get_block_children(self, block_id, page_size=100):
        return self._children.get(block_id, [])

    def get_page(self, page_id):
        return {"url": "https://www.notion.so/spine"}


def test_read_spine_hydrates_nested_toggle_and_table():
    top = [{"id": "tog1", "type": "toggle",
            "toggle": {"rich_text": [{"plain_text": "Active Workstreams"}]},
            "has_children": True}]
    tog_children = [{"id": "tbl1", "type": "table", "table": {}, "has_children": True}]
    tbl_children = [_row(_HEADER), _row(["RSM comp", "Active", "High", "Comp plan"])]
    client = _FakeNotion({"PAGE": top, "tog1": tog_children, "tbl1": tbl_children})
    s = read_spine(client, "PAGE")
    w = s.workstream("RSM comp")
    assert w is not None and w.time_sensitive is True and w.status == "Active"
    assert s.page_url == "https://www.notion.so/spine"
