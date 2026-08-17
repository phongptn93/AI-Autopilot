"""Derive the live board: group tagged work items into pipeline columns.

Pure functions (no I/O) so they can be unit-tested. The caller supplies the
tagged work items (from ADO) and the latest execution record per item (from the
DB); each item's column is derived from its ADO tags + that record. State/tag
names come from config — nothing here is hardcoded to a particular board.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ai_autopilot.config import Settings
from ai_autopilot.data.entities import ExecutionRecord, ExecutionStatus
from ai_autopilot.models import WorkItemInfo

# Pipeline columns the autopilot manages, left → right.
COLUMNS: list[str] = ["Queued", "In progress", "In review", "Needs human", "Done", "Failed"]

# Optional hand-off columns driven purely by the item's ADO state (a human moves
# the item there; the autopilot doesn't). Shown only when configured, grouped
# right after "In review".
COL_READY_REVIEW = "Ready for review"
COL_READY_DEPLOY = "Ready to deploy"


def board_columns(cfg: Settings) -> list[str]:
    """The active board columns for this config: the base pipeline plus any
    configured ADO-state hand-off columns, inserted right after 'In review'."""
    cols = ["Queued", "In progress", "In review"]
    if (cfg.board_review_state or "").strip():
        cols.append(COL_READY_REVIEW)
    if (cfg.board_deploy_state or "").strip():
        cols.append(COL_READY_DEPLOY)
    cols += ["Needs human", "Done", "Failed"]
    return cols


@dataclass
class BoardCard:
    id: int
    title: str
    ado_state: str
    column: str
    # The item's own ADO project. With several projects on one board, the link to
    # Azure DevOps has to be built per card — a single board-wide project would send
    # every card from the other projects to a URL that does not resolve.
    project: str = ""
    url: str = ""  # filled by the dashboard (needs the org URL, which board.py has no I/O for)
    category: str = ""  # BackendTask / FrontendTask / ... for the colour badge
    assigned_to: str | None = None
    pr_url: str | None = None
    pr_urls: list[str] = field(default_factory=list)  # every PR the task opened


def parse_drop_map(entries: list[str]) -> dict[str, tuple[str, str]]:
    """Parse board drag-drop rules into ``{column_lower: (kind, value)}``.

    Each entry is ``"Column => value"``; ``value`` is a tag by default, or an ADO
    state when prefixed with ``@`` (e.g. ``"Done => @Closed"``). ``=`` also works
    as the separator.
    """
    out: dict[str, tuple[str, str]] = {}
    for entry in entries:
        sep = "=>" if "=>" in entry else ("=" if "=" in entry else "")
        if not sep:
            continue
        col, val = entry.split(sep, 1)
        col, val = col.strip(), val.strip()
        if not col or not val:
            continue
        if val.startswith("@"):
            out[col.lower()] = ("state", val[1:].strip())
        else:
            out[col.lower()] = ("tag", val)
    return out


def _record_pr_urls(record: ExecutionRecord | None) -> list[str]:
    """All PR URLs for a record: the JSON ``pr_urls`` list, else the single pr_url."""
    if record is None:
        return []
    raw = getattr(record, "pr_urls", None)
    if raw:
        try:
            urls = json.loads(raw)
            if isinstance(urls, list):
                return [u for u in urls if u]
        except (ValueError, TypeError):
            pass
    return [record.pr_url] if record.pr_url else []


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
    # ADO-state hand-off columns win: they reflect where a human moved the item
    # (e.g. "Ready to Deploy"), which the autopilot no longer manages.
    state = (item.state or "").strip().lower()
    if state and state in {s.strip().lower() for s in cfg.done_states if s.strip()}:
        return "Done"
    if (cfg.board_review_state or "").strip() and state == cfg.board_review_state.strip().lower():
        return COL_READY_REVIEW
    if (cfg.board_deploy_state or "").strip() and state == cfg.board_deploy_state.strip().lower():
        return COL_READY_DEPLOY
    # The autopilot's own persisted pipeline state wins next.
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
    board: dict[str, list[BoardCard]] = {col: [] for col in board_columns(cfg)}
    for item in items:
        record = records_by_id.get(item.id)
        column = _column_for(item, record, cfg, states_by_id.get(item.id))
        if column not in board:  # configured column absent → safe fallback
            column = "Queued"
        board[column].append(
            BoardCard(
                id=item.id,
                title=item.title,
                ado_state=item.state,
                column=column,
                project=item.project,
                category=_category(item.title),
                assigned_to=item.assigned_to,
                pr_url=record.pr_url if record else None,
                pr_urls=_record_pr_urls(record),
            )
        )
    return board
