"""Spine: Active Workstreams Priority / Time-sensitive attributes (item 3 support)."""

from spine import load_spine_from_dict, parse_spine_blocks


def _heading(text):
    return {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": text}]}}


def _bullet(text):
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": text}]}}


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
