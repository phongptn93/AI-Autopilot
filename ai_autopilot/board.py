"""Derive the live board: group tagged work items into pipeline columns.

Pure functions (no I/O) so they can be unit-tested. The caller supplies the
tagged work items (from ADO) and the latest execution record per item (from the
DB); each item's column is derived from its ADO tags + that record. State/tag
names come from config — nothing here is hardcoded to a particular board.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_autopilot.config import Settings
from ai_autopilot.data.entities import ExecutionRecord, ExecutionStatus
from ai_autopilot.models import WorkItemInfo

# Pipeline columns, left → right.
COLUMNS: list[str] = ["Queued", "In progress", "In review", "Needs human", "Done", "Failed"]


@dataclass
class BoardCard:
    id: int
    title: str
    ado_state: str
    column: str
    category: str = ""  # BackendTask / FrontendTask / ... for the colour badge
    assigned_to: str | None = None
    pr_url: str | None = None


def _category(title: str) -> str:
    t = title.upper()
    if t.startswith("[BE]"):
        return "BackendTask"
    if t.startswith("[FE]"):
        return "FrontendTask"
    if t.startswith("[DB]"):
        return "DatabaseTask"
    if t.startswith("[QC]") or t.startswith("[TEST]"):
        return "TestTask"
    return ""


def _column_for(
    item: WorkItemInfo, record: ExecutionRecord | None, cfg: Settings, persisted: str | None = None
) -> str:
    # The autopilot's own persisted pipeline state wins when present.
    if persisted in COLUMNS:
        return persisted
    tags = {t.lower() for t in item.tags}
    # ADO tags are the cross-restart source of truth, checked first.
    if cfg.escalation_tag.lower() in tags:
        return "Needs human"
    if cfg.review_tag.lower() in tags:
        return "In review"
    if cfg.processed_tag.lower() in tags:
        return "Done"
    # Otherwise use the autopilot's own latest record.
    if record is not None:
        if record.status == ExecutionStatus.RUNNING:
            return "In progress"
        if record.status in (ExecutionStatus.FAILED, ExecutionStatus.RETRYING):
            return "Failed"
        if record.status == ExecutionStatus.SUCCESS:
            return "Done"
    return "Queued"


def latest_records(records: list[ExecutionRecord]) -> dict[int, ExecutionRecord]:
    """Most recent record per work item (records must be sorted newest-first)."""
    out: dict[int, ExecutionRecord] = {}
    for r in records:
        out.setdefault(r.work_item_id, r)
    return out


def build_board(
    items: list[WorkItemInfo],
    records_by_id: dict[int, ExecutionRecord],
    cfg: Settings,
    states_by_id: dict[int, str] | None = None,
) -> dict[str, list[BoardCard]]:
    """Group tagged work items into pipeline columns.

    ``states_by_id`` (work_item_id → persisted pipeline state name) takes priority
    when present; otherwise the column is derived from ADO tags + the last record.
    """
    states_by_id = states_by_id or {}
    board: dict[str, list[BoardCard]] = {col: [] for col in COLUMNS}
    for item in items:
        record = records_by_id.get(item.id)
        column = _column_for(item, record, cfg, states_by_id.get(item.id))
        board[column].append(
            BoardCard(
                id=item.id,
                title=item.title,
                ado_state=item.state,
                column=column,
                category=_category(item.title),
                assigned_to=item.assigned_to,
                pr_url=record.pr_url if record else None,
            )
        )
    return board
