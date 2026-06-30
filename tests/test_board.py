"""Tests for board column derivation (pure, no I/O)."""

from __future__ import annotations

from ai_autopilot.board import build_board, latest_records
from ai_autopilot.config import Settings
from ai_autopilot.data.entities import ExecutionRecord, ExecutionStatus
from ai_autopilot.models import WorkItemInfo

CFG = Settings()  # default tags: autopilot / autopilot-done / autopilot-review / autopilot-hold


def _item(item_id: int, tags: list[str], state: str = "Active") -> WorkItemInfo:
    return WorkItemInfo(id=item_id, title=f"t{item_id}", work_item_type="Task", state=state, tags=tags)


def _rec(item_id: int, status: ExecutionStatus, pr: str | None = None) -> ExecutionRecord:
    return ExecutionRecord(work_item_id=item_id, status=status, pr_url=pr)


def test_columns_from_tags():
    items = [
        _item(1, ["autopilot"]),
        _item(2, ["autopilot", "autopilot-review"]),
        _item(3, ["autopilot", "autopilot-done"]),
        _item(4, ["autopilot", "autopilot-hold"]),
    ]
    board = build_board(items, {}, CFG)
    assert board["Queued"][0].id == 1
    assert board["In review"][0].id == 2
    assert board["Done"][0].id == 3
    assert board["Needs human"][0].id == 4


def test_columns_from_records():
    items = [_item(1, ["autopilot"]), _item(2, ["autopilot"])]
    recs = {1: _rec(1, ExecutionStatus.RUNNING), 2: _rec(2, ExecutionStatus.FAILED)}
    board = build_board(items, recs, CFG)
    assert board["In progress"][0].id == 1
    assert board["Failed"][0].id == 2


def test_tag_takes_priority_over_record():
    items = [_item(1, ["autopilot", "autopilot-done"])]
    recs = {1: _rec(1, ExecutionStatus.RUNNING)}  # stale running record
    board = build_board(items, recs, CFG)
    assert board["Done"][0].id == 1


def test_pr_url_surfaced_from_record():
    items = [_item(1, ["autopilot"])]
    recs = {1: _rec(1, ExecutionStatus.RUNNING, "https://pr/1")}
    board = build_board(items, recs, CFG)
    assert board["In progress"][0].pr_url == "https://pr/1"


def test_latest_records_keeps_newest_first():
    recs = [
        _rec(1, ExecutionStatus.SUCCESS, "pr-new"),
        _rec(1, ExecutionStatus.FAILED, "pr-old"),
    ]
    latest = latest_records(recs)
    assert latest[1].pr_url == "pr-new"


def test_persisted_state_takes_priority():
    items = [_item(1, ["autopilot", "autopilot-done"])]  # tag says Done
    recs = {1: _rec(1, ExecutionStatus.RUNNING)}
    board = build_board(items, recs, CFG, states_by_id={1: "Needs human"})
    assert board["Needs human"][0].id == 1  # persisted state wins over tag/record


def test_unknown_persisted_state_falls_back_to_derived():
    items = [_item(1, ["autopilot", "autopilot-done"])]
    board = build_board(items, {}, CFG, states_by_id={1: "Bogus"})
    assert board["Done"][0].id == 1  # invalid persisted value ignored → derived


def test_resolved_state_and_escalation_tag_defaults():
    assert CFG.resolved_state == "Resolved"
    assert CFG.escalation_tag == "autopilot-hold"
