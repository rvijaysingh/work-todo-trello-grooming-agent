"""Report rendering — numbered entries, 4-line layout, local due formatting, no
URLs/ISO, single decision instruction, archive-only-in-recently-archived,
Done-automatically subsections, section counts, health deltas, dry-run gating."""

from phases import execute as ex
from phases import report as rep

NOW = "2026-07-11T13:00:00+00:00"


def _stats(**kw):
    base = {"scratch_backlog": 5, "hygiene_coverage_pct": 90.0, "scratch_lists": ["Scratch 7-1"]}
    base.update(kw)
    return base


def _build(result, board, settings, dry_run=True, prev_stats=None, stats=None):
    return rep.build_report(result, board, settings, NOW, dry_run=dry_run, first_run=False,
                            stats=stats or _stats(), prev_stats=prev_stats,
                            approval_rates={"approved": 0, "rejected": 0, "rate": "n/a"})


# ── Local due formatting ────────────────────────────────────────────────────

def test_fmt_due_local_no_iso(settings):
    # 2026-06-01T00:00Z → local (UTC-4 DST) 2026-05-31 20:00 → "5/31 8:00pm".
    assert rep._fmt_due("2026-06-01T00:00:00.000Z", settings) == "5/31 8:00pm"


def test_fmt_due_morning(settings):
    assert rep._fmt_due("2026-06-27T15:00:00.000Z", settings) == "6/27 11:00am"


# ── Header + at-a-glance + section counts ───────────────────────────────────

def test_header_at_a_glance_and_counts(board, settings):
    result = ex.ExecutionResult()
    result.still_overdue.append({"card_id": "dead_due", "name": "Follow up with payer",
                                 "due": "2026-06-01T00:00:00.000Z", "reason": "Workstream active"})
    result.applied.append({"type": "rename", "card_id": "name_pipe", "new_name": "Call Dana"})
    text = _build(result, board, settings, dry_run=False)
    assert "At a glance: 1 need your attention | 0 proposals awaiting answer | 0 archived | 1 automatic changes" in text
    assert "== Still overdue and possibly urgent (1) ==" in text
    assert "== Awaiting your decision (0) ==" in text
    assert "== Recently archived (0) ==" in text
    assert "== Done automatically (1) ==" in text


# ── Escalation entry layout ─────────────────────────────────────────────────

def test_escalation_entry_layout(board, settings):
    result = ex.ExecutionResult()
    result.still_overdue.append({"card_id": "stale_label", "name": "Draft dashboard spec",
                                 "due": "2026-06-01T00:00:00.000Z", "reason": "Workstream still active"})
    text = _build(result, board, settings)
    assert "#1 Card Name: Draft dashboard spec (due 5/31 8:00pm)" in text
    assert "Action needed: Workstream still active." in text
    assert "   Card Labels: 1. Today (must do)" in text
    assert "   Card Description: Dashboard spec." in text
    assert "http" not in text                       # no URLs anywhere
    assert "T00:00" not in text                     # no ISO timestamps


# ── Awaiting your decision: single instruction, no per-card arrow ───────────

def test_awaiting_single_instruction_no_arrow(board, settings):
    result = ex.ExecutionResult()
    result.proposals_opened.append({
        "proposal_id": 1, "type": "inscope_archive", "card_id": "owner_dead",
        "card_ids": ["owner_dead"], "title": "[Owner: Marah] Send Cooper the dashboard",
        "action_desc": "move to the Agent Archive list (no longer needed)",
        "reason": "Delegated item", "confidence": 65})
    text = _build(result, board, settings)
    assert text.count("To answer any proposal, do exactly one") == 1
    assert "reply 'yes'/'approve' on the card" not in text  # old per-card arrow gone
    assert "Proposed: move to the Agent Archive list (no longer needed). Reason: Delegated item. Confidence: 65%. Expires 7/25." in text


def test_proposals_sorted_by_confidence_desc(board, settings):
    result = ex.ExecutionResult()
    for cid, conf in [("dead_due", 40), ("stale_label", 90), ("name_pipe", 65)]:
        result.proposals_opened.append({
            "proposal_id": 1, "type": "dead_due_clear", "card_id": cid, "card_ids": [cid],
            "title": cid, "action_desc": "clear a due date", "reason": "r", "confidence": conf})
    text = _build(result, board, settings)
    # Order by confidence desc: stale_label (90) → name_pipe (65) → dead_due (40).
    assert text.index("Draft dashboard spec") < text.index("Call Dana")     # 90 before 65
    assert text.index("Call Dana") < text.index("Follow up with payer")     # 65 before 40


def test_merge_proposal_renders_subletters(board, settings):
    result = ex.ExecutionResult()
    result.proposals_opened.append({
        "proposal_id": 1, "type": "merge", "card_id": "dup_person_a", "survivor_id": "dup_person_a",
        "card_ids": ["dup_person_a", "dup_person_b"], "title": "Review Forge rollout",
        "action_desc": "merge duplicates into 'Review Forge rollout'",
        "reason": "Same task, different owners", "confidence": 72})
    text = _build(result, board, settings)
    assert "#1a Card Name: Review Forge rollout" in text
    assert "#1b Card Name: Review Forge rollout" in text
    # Action line appears once, on the first sub-card.
    assert text.count("Proposed: merge duplicates into") == 1
    # Each sub-card keeps its own labels.
    assert "Card Labels: Colin" in text and "Card Labels: Logan" in text


# ── Recently archived: parenthetical, no per-line suffix, Reason line ───────

def test_recently_archived_dry_run_parenthetical(board, settings):
    result = ex.ExecutionResult()
    result.recently_archived.append({"card_id": "owner_dead", "name": "[Owner: Marah] Send Cooper the dashboard",
                                     "url": "http://x", "note": ex.archive_list_wording(settings),
                                     "reason": "Titled [Owner: …] delegated item"})
    text = _build(result, board, settings, dry_run=True)
    assert "(would move to the Agent Archive list — visible 60 days, then Trello's restorable archive)" in text
    assert "Reason: Titled [Owner: …] delegated item." in text
    # Per-line "visible 60 days" suffix is NOT repeated on the entry.
    body = text.split("== Recently archived")[1].split("== Done automatically")[0]
    assert body.count("visible 60 days") == 1  # only in the parenthetical


def test_recently_archived_live_parenthetical(board, settings):
    result = ex.ExecutionResult()
    result.recently_archived.append({"card_id": "owner_dead", "name": "x", "url": "",
                                     "note": ex.archive_list_wording(settings), "reason": "gone"})
    text = _build(result, board, settings, dry_run=False)
    assert "(moved to the Agent Archive list — visible 60 days" in text


# ── Archive moves appear ONLY in Recently archived ─────────────────────────

def test_archive_move_not_duplicated_in_done_automatically(board, settings):
    result = ex.ExecutionResult()
    # An in-scope archive is both an applied action and a recently-archived entry.
    result.applied.append({"type": "inscope_archive", "card_id": "owner_dead", "reason": "delegated"})
    result.recently_archived.append({"card_id": "owner_dead", "name": "[Owner: Marah] Send Cooper the dashboard",
                                     "url": "", "note": ex.archive_list_wording(settings),
                                     "reason": "delegated"})
    text = _build(result, board, settings, dry_run=False)
    done = text.split("== Done automatically")[1].split("== Health stats")[0]
    assert "Send Cooper the dashboard" not in done       # not under Done automatically
    assert "== Done automatically (0) ==" in text        # the archive is not an "automatic change"
    arch = text.split("== Recently archived")[1].split("== Done automatically")[0]
    assert "Send Cooper the dashboard" in arch


# ── Done automatically subsections ─────────────────────────────────────────

def test_done_automatically_subsections_order_and_verbs(board, settings):
    result = ex.ExecutionResult()
    result.applied.append({"type": "rename", "card_id": "name_pipe", "new_name": "Call Dana"})
    result.applied.append({"type": "dead_due_clear", "card_id": "dead_due"})
    result.applied.append({"type": "stale_label_removal", "card_id": "stale_label",
                           "label": "1. Today (must do)"})
    result.reminder_created = True
    text = _build(result, board, settings, dry_run=False)
    # Subsections present in canonical order; verbs are past tense ("Did:").
    order = ["-- Date fixes --", "-- Label changes --", "-- Renames --", "-- Other --"]
    idxs = [text.index(s) for s in order]
    assert idxs == sorted(idxs)
    assert "-- Recovered from Scratch --" not in text     # empty subsection omitted
    assert "Did: Fix Due Date — clear the long-overdue due date" in text
    assert "Did: Rename Card — rename to 'Call Dana'" in text
    assert "Would:" not in text                           # live run
    assert "create the weekly spine-review reminder card" in text  # reminder under Other


def test_done_automatically_dry_run_uses_would(board, settings):
    result = ex.ExecutionResult()
    result.applied.append({"type": "rename", "card_id": "name_pipe", "new_name": "Call Dana"})
    text = _build(result, board, settings, dry_run=True)
    assert "Would: Rename Card — rename to 'Call Dana'" in text
    assert "Did:" not in text


def test_numbering_restarts_per_section(board, settings):
    result = ex.ExecutionResult()
    result.still_overdue.append({"card_id": "dead_due", "name": "Follow up with payer",
                                 "due": None, "reason": "active"})
    result.applied.append({"type": "rename", "card_id": "name_pipe", "new_name": "Call Dana"})
    text = _build(result, board, settings, dry_run=False)
    # Both sections start their own #1.
    assert "#1 Card Name: Follow up with payer" in text
    assert "#1 Card Name: Call Dana | prep agenda | send notes" in text


# ── Health deltas ──────────────────────────────────────────────────────────

def test_health_delta_and_new_scratch_list(board, settings):
    result = ex.ExecutionResult()
    stats = _stats(scratch_backlog=139, hygiene_coverage_pct=88.0,
                   scratch_lists=["Scratch 7-15", "Scratch 7-1"])
    prev = {"scratch_backlog": 94, "hygiene_coverage_pct": 90.0, "scratch_lists": ["Scratch 7-1"]}
    text = _build(result, board, settings, dry_run=True, stats=stats, prev_stats=prev)
    assert "Scratch backlog count: 139 (+45; new list 'Scratch 7-15' entered scope)" in text
    assert "Hygiene coverage (in-scope): 88.0% (-2.0)" in text


def test_health_no_delta_when_no_prior(board, settings):
    result = ex.ExecutionResult()
    text = _build(result, board, settings, dry_run=True, prev_stats=None)
    backlog_line = next(l for l in text.splitlines() if l.startswith("Scratch backlog count:"))
    assert backlog_line == "Scratch backlog count: 5"   # no delta parenthetical


# ── PRE-FIRST-RUN reminder gating ──────────────────────────────────────────

def test_pre_first_run_reminder_only_in_dry_run(board, settings):
    result = ex.ExecutionResult()
    dry = _build(result, board, settings, dry_run=True)
    live = _build(result, board, settings, dry_run=False)
    assert "PRE-FIRST-RUN REMINDER" in dry
    assert "PRE-FIRST-RUN REMINDER" not in live


def test_no_urls_anywhere(board, settings):
    result = ex.ExecutionResult()
    result.applied.append({"type": "rename", "card_id": "name_pipe", "new_name": "Call Dana"})
    result.recently_archived.append({"card_id": "dup_person_a", "name": "Review Forge rollout",
                                     "url": "http://trello/pa", "note": ex.TRELLO_ARCHIVE_WORDING,
                                     "reason": "aged out"})
    text = _build(result, board, settings, dry_run=False)
    assert "http" not in text


def test_description_strips_urls_and_markdown_links(board, fresh_board, settings):
    # A description with a markdown link + bare URL renders with neither URL.
    def _mutate(data):
        for c in data["cards"]:
            if c["id"] == "dead_due":
                c["desc"] = ("**Source:** [7/1 Sync](https://notion.so/x) notes at "
                             "https://example.com/page follow up soon")
    b = fresh_board(_mutate)
    result = ex.ExecutionResult()
    result.still_overdue.append({"card_id": "dead_due", "name": "Follow up with payer",
                                 "due": None, "reason": "active"})
    text = rep.build_report(result, b, settings, NOW, dry_run=True, first_run=False,
                            stats=_stats(), approval_rates={})
    assert "http" not in text
    assert "7/1 Sync" in text            # link text preserved
    assert "Card Description:" in text
