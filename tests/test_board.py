"""Tests for board column derivation (pure, no I/O)."""

from __future__ import annotations

from ai_autopilot.board import board_columns, build_board, latest_records, parse_drop_map
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


def test_multiple_pr_urls_surfaced():
    import json
    items = [_item(1, ["autopilot"])]
    rec = _rec(1, ExecutionStatus.SUCCESS, "https://pr/1")
    rec.pr_urls = json.dumps(["https://pr/1", "https://pr/2"])
    board = build_board(items, {1: rec}, CFG)
    assert board["Done"][0].pr_urls == ["https://pr/1", "https://pr/2"]


def test_pr_urls_falls_back_to_single():
    items = [_item(1, ["autopilot"])]
    board = build_board(items, {1: _rec(1, ExecutionStatus.SUCCESS, "https://pr/only")}, CFG)
    assert board["Done"][0].pr_urls == ["https://pr/only"]


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


def test_parse_drop_map():
    assert parse_drop_map(
        ["In review => autopilot-review", "Done => @Closed", "Ready => tag2", "bad-entry"]
    ) == {
        "in review": ("tag", "autopilot-review"),
        "done": ("state", "Closed"),
        "ready": ("tag", "tag2"),
    }


def test_done_states_map_to_done_column():
    cfg = Settings(done_states=["Ready to Testing", "Closed"])
    items = [
        _item(1, ["autopilot"], state="Ready to Testing"),                     # human moved → Done
        _item(2, ["autopilot", "autopilot-review"], state="Ready to Testing"),  # beats the review tag
        _item(3, ["autopilot"], state="Active"),                               # normal → Queued
    ]
    board = build_board(items, {}, cfg)
    done_ids = {c.id for c in board["Done"]}
    assert done_ids == {1, 2}
    assert board["Queued"][0].id == 3


def test_extra_columns_hidden_by_default():
    # No board_review_state / board_deploy_state configured → base 6 columns.
    assert board_columns(CFG) == [
        "Queued", "In progress", "In review", "Needs human", "Done", "Failed"
    ]


def test_extra_columns_appear_and_map_by_ado_state():
    cfg = Settings(board_review_state="Ready to Review", board_deploy_state="Ready to Deploy")
    cols = board_columns(cfg)
    # inserted right after "In review", grouped together
    assert cols == [
        "Queued", "In progress", "In review", "Ready for review", "Ready to deploy",
        "Needs human", "Done", "Failed",
    ]
    items = [
        _item(1, ["autopilot"], state="Ready to Review"),
        _item(2, ["autopilot", "autopilot-done"], state="Ready to Deploy"),  # ADO state wins over tag
        _item(3, ["autopilot"], state="Active"),
    ]
    board = build_board(items, {}, cfg)
    assert board["Ready for review"][0].id == 1
    assert board["Ready to deploy"][0].id == 2   # beats the Done tag
    assert board["Queued"][0].id == 3
