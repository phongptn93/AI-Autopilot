"""Execution history repository (ported from ``ExecutionRepository``)."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, false, func, or_, select

from ai_autopilot.data.database import Database
from ai_autopilot.data.entities import (
    AiConflict,
    AlertState,
    AuditEvent,
    ClaudeSession,
    ExecutionRecord,
    ExecutionStatus,
    HandledPrComment,
    HeldNotification,
    MergedPr,
    PipelineState,
    PlannedRun,
    PrCommandState,
    PrReviewBudget,
    PrReviewerState,
    QualityEvent,
    QualityKind,
    SchedulerDecision,
    SdlcLoopState,
    SpecDrift,
    WorkItemState,
    WorkItemStateHistory,
)
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo


@dataclass
class ExecutionStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    avg_duration: float = 0.0


@dataclass
class EfficiencyStats:
    """Effort spent per unit of work — the denominator for "is this worth it"."""

    distinct_items: int = 0     # work items that got at least one run
    total_runs: int = 0
    total_tokens: int = 0

    @property
    def avg_runs_per_item(self) -> float:
        return self.total_runs / self.distinct_items if self.distinct_items else 0.0


def _project_cond(projects: list[str] | None):
    """SQL predicate restricting executions to ``projects``, or ``None`` for no filter.

    ``None`` and ``[]`` are not interchangeable: no workspace selected means every
    project, while a workspace with no project assigned must match nothing."""
    if projects is None:
        return None
    return ExecutionRecord.project.in_(projects) if projects else false()


class ExecutionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def start_execution(
        self, item: WorkItemInfo, skill: str, trigger_tag: str | None = None
    ) -> int:
        async with self._db.session() as session:
            record = ExecutionRecord(
                work_item_id=item.id,
                title=item.title,
                project=item.project or "",
                category=str(item.category),
                skill_used=skill,
                trigger_tag=trigger_tag,
                status=ExecutionStatus.RUNNING,
                started_at=datetime.now(UTC),
            )
            session.add(record)
            await session.commit()
            return record.id

    async def complete_execution(self, record_id: int, result: ExecutionResult) -> None:
        async with self._db.session() as session:
            record = await session.get(ExecutionRecord, record_id)
            if record is None:
                return
            record.status = ExecutionStatus.SUCCESS if result.success else ExecutionStatus.FAILED
            record.branch_name = result.branch_name
            record.pr_url = result.pr_url
            record.pr_urls = json.dumps(result.pr_urls) if result.pr_urls else None
            record.files_changed = (
                json.dumps(result.files_changed) if result.files_changed else None
            )
            record.error = (result.error or "")[:2000] or None
            record.output = (result.output or "")[:5000]
            now = datetime.now(UTC)
            record.completed_at = now
            record.duration_seconds = result.duration_seconds
            # Interactive sessions (and any path that didn't time itself) leave
            # duration at 0 — fall back to elapsed wall-clock (dispatch → finish).
            if not record.duration_seconds and record.started_at is not None:
                start = record.started_at
                if start.tzinfo is None:
                    start = start.replace(tzinfo=UTC)
                record.duration_seconds = max(0.0, (now - start).total_seconds())
                # Write it back onto the result too. Only the DB row was corrected, so
                # History showed a real duration while the Teams card — which reads the
                # result object, and is notified after this call — showed 0:00 for every
                # interactive run. One number, one place it is computed.
                result.duration_seconds = record.duration_seconds
            if result.cost_tokens:
                record.cost_tokens = result.cost_tokens
                # The breakdown is written under the same guard as the total: a run that
                # reported no usage at all must keep NULLs, so History can distinguish
                # "we don't know" from "this was free". ``cost_usd`` stays None when the
                # CLI did not price the run, which is not the same as costing nothing.
                record.model_used = result.model_used or None
                record.cost_usd = result.cost_usd
                record.input_tokens = result.input_tokens
                record.output_tokens = result.output_tokens
                record.cache_read_tokens = result.cache_read_tokens
                record.cache_creation_tokens = result.cache_creation_tokens
            record.lessons_injected = result.lessons_injected
            await session.commit()

    async def mark_retrying(self, work_item_id: int, retry_count: int) -> None:
        """Stamp the attempt number on the item's most recent run.

        Keyed by work item rather than record id: every caller reaches this right after
        ``complete_execution`` on the run that just failed, so the newest row for the
        item *is* that run — and the failure handlers would otherwise have to thread a
        record id through six call sites to say so. Status is left as the handler set
        it (FAILED); only the counter is written, which is what the History page's
        "Retries" column reads. That column was structurally always 0 before this had
        a caller.
        """
        async with self._db.session() as session:
            record = (await session.execute(
                select(ExecutionRecord)
                .where(ExecutionRecord.work_item_id == work_item_id)
                .order_by(ExecutionRecord.started_at.desc())
                .limit(1)
            )).scalars().first()
            if record is None:
                return
            record.retry_count = retry_count
            await session.commit()

    async def update_cost(self, record_id: int, tokens: int) -> None:
        async with self._db.session() as session:
            record = await session.get(ExecutionRecord, record_id)
            if record is None:
                return
            record.cost_tokens = tokens
            await session.commit()

    async def fail_running(self) -> int:
        """Mark orphaned RUNNING executions (left by a crashed process) as failed."""
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ExecutionRecord).where(
                        ExecutionRecord.status == ExecutionStatus.RUNNING
                    )
                )
            ).scalars().all()
            for row in rows:
                row.status = ExecutionStatus.FAILED
                row.error = "Interrupted (process restarted)"
                row.completed_at = datetime.now(UTC)
            await session.commit()
            return len(rows)

    async def for_item(self, work_item_id: int, limit: int = 50) -> list[ExecutionRecord]:
        """Every run of ONE work item, newest first (the task page's history tab)."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(ExecutionRecord)
                .where(ExecutionRecord.work_item_id == work_item_id)
                .order_by(ExecutionRecord.started_at.desc())
                .limit(max(1, limit))
            )
            return list(rows.scalars().all())

    async def search(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        q: str | None = None,
        dfrom: str | None = None,
        dto: str | None = None,
        trigger_tag: str | None = None,
        projects: list[str] | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[ExecutionRecord], int]:
        """Filtered + paginated executions, newest-first, with the total match count.

        Filters: status, category, free-text ``q`` (work-item id or title), started
        date range (``dfrom``/``dto`` as ``YYYY-MM-DD``), trigger tag, and ADO
        project.

        ``projects`` is applied in SQL rather than by the caller because this query
        is PAGINATED: filtering the page afterwards would hand back three rows out
        of twenty-five, with a total count that disagrees with them.
        """
        conds = []
        if status:
            try:
                conds.append(ExecutionRecord.status == ExecutionStatus(status))
            except ValueError:
                pass
        if category:
            conds.append(ExecutionRecord.category == category)
        if trigger_tag:
            conds.append(ExecutionRecord.trigger_tag == trigger_tag)
        project_cond = _project_cond(projects)
        if project_cond is not None:
            conds.append(project_cond)
        if q:
            s = q.strip()
            if s.isdigit():
                conds.append(ExecutionRecord.work_item_id == int(s))
            else:
                conds.append(ExecutionRecord.title.ilike(f"%{s}%"))
        if dfrom:
            conds.append(func.date(ExecutionRecord.started_at) >= dfrom)
        if dto:
            conds.append(func.date(ExecutionRecord.started_at) <= dto)
        async with self._db.session() as session:
            total = (
                await session.execute(
                    select(func.count()).select_from(ExecutionRecord).where(*conds)
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    select(ExecutionRecord)
                    .where(*conds)
                    .order_by(ExecutionRecord.started_at.desc())
                    .offset(max(0, offset))
                    .limit(max(1, limit))
                )
            ).scalars().all()
        return list(rows), int(total)

    async def get_recent(
        self, count: int = 50, trigger_tag: str | None = None,
        projects: list[str] | None = None,
    ) -> list[ExecutionRecord]:
        async with self._db.session() as session:
            query = select(ExecutionRecord)
            if trigger_tag:
                query = query.where(ExecutionRecord.trigger_tag == trigger_tag)
            project_cond = _project_cond(projects)
            if project_cond is not None:
                query = query.where(project_cond)
            rows = await session.execute(
                query.order_by(ExecutionRecord.started_at.desc()).limit(count)
            )
            return list(rows.scalars().all())

    async def delete(self, record_id: int) -> bool:
        """Delete a single execution record. Returns True if a row was removed."""
        async with self._db.session() as session:
            record = await session.get(ExecutionRecord, record_id)
            if record is None:
                return False
            await session.delete(record)
            await session.commit()
            return True

    async def clear_all(self) -> int:
        """Delete all execution records. Returns the number of rows removed."""
        async with self._db.session() as session:
            result = await session.execute(delete(ExecutionRecord))
            await session.commit()
            return int(result.rowcount or 0)

    async def get_by_work_item(self, work_item_id: int) -> list[ExecutionRecord]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(ExecutionRecord)
                .where(ExecutionRecord.work_item_id == work_item_id)
                .order_by(ExecutionRecord.started_at.desc())
            )
            return list(rows.scalars().all())

    async def get_stats(
        self, since: datetime | None = None, trigger_tag: str | None = None,
        projects: list[str] | None = None,
    ) -> ExecutionStats:
        project_cond = _project_cond(projects)
        async with self._db.session() as session:
            base = select(func.count()).select_from(ExecutionRecord)
            if since is not None:
                base = base.where(ExecutionRecord.started_at >= since)
            if trigger_tag:
                base = base.where(ExecutionRecord.trigger_tag == trigger_tag)
            if project_cond is not None:
                base = base.where(project_cond)

            total = (await session.execute(base)).scalar_one()
            success = (
                await session.execute(
                    base.where(ExecutionRecord.status == ExecutionStatus.SUCCESS)
                )
            ).scalar_one()
            failed = (
                await session.execute(
                    base.where(ExecutionRecord.status == ExecutionStatus.FAILED)
                )
            ).scalar_one()

            avg_query = select(func.avg(ExecutionRecord.duration_seconds))
            if since is not None:
                avg_query = avg_query.where(ExecutionRecord.started_at >= since)
            if trigger_tag:
                avg_query = avg_query.where(ExecutionRecord.trigger_tag == trigger_tag)
            if project_cond is not None:
                avg_query = avg_query.where(project_cond)
            avg = (await session.execute(avg_query)).scalar() or 0.0

            return ExecutionStats(
                total=total, success=success, failed=failed, avg_duration=float(avg)
            )

    async def work_item_ids(self) -> set[int]:
        """Every work item this autopilot has actually run.

        The authoritative answer to "is that PR ours?". Branch names are not: the prefix
        list matches what humans name their branches too, and the agent sometimes picks a
        prefix that isn't on the list at all.
        """
        async with self._db.session() as session:
            rows = await session.execute(
                select(func.distinct(ExecutionRecord.work_item_id))
            )
            return {int(r[0]) for r in rows if r[0] is not None}

    async def get_efficiency(
        self, trigger_tag: str | None = None, projects: list[str] | None = None
    ) -> EfficiencyStats:
        """Aggregate effort figures for the Overview's efficiency cards."""
        conds = []
        if trigger_tag:
            conds.append(ExecutionRecord.trigger_tag == trigger_tag)
        project_cond = _project_cond(projects)
        if project_cond is not None:
            conds.append(project_cond)
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(
                        func.count(func.distinct(ExecutionRecord.work_item_id)),
                        func.count(),
                        func.coalesce(func.sum(ExecutionRecord.cost_tokens), 0),
                    ).where(*conds)
                )
            ).one()
            return EfficiencyStats(
                distinct_items=int(row[0]), total_runs=int(row[1]), total_tokens=int(row[2])
            )


class StateRepository:
    """Persisted per-work-item pipeline state — the autopilot's resumable memory."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def set(
        self,
        work_item_id: int,
        state: PipelineState,
        *,
        title: str = "",
        detail: str | None = None,
        pr_url: str | None = None,
    ) -> None:
        """Upsert the state for a work item (only overwrites fields provided)."""
        async with self._db.session() as session:
            row = await session.get(WorkItemState, work_item_id)
            if row is None:
                row = WorkItemState(work_item_id=work_item_id)
                session.add(row)
            row.state = state
            if title:
                row.title = title
            if detail is not None:
                row.detail = detail
            if pr_url is not None:
                row.pr_url = pr_url
            row.updated_at = datetime.now(UTC)
            await session.commit()

    async def get(self, work_item_id: int) -> WorkItemState | None:
        async with self._db.session() as session:
            return await session.get(WorkItemState, work_item_id)

    async def all(self) -> list[WorkItemState]:
        async with self._db.session() as session:
            rows = await session.execute(select(WorkItemState))
            return list(rows.scalars().all())

    async def requeue_in_progress(self) -> int:
        """On startup, re-queue runs that were interrupted mid-flight (resume)."""
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(WorkItemState).where(WorkItemState.state == PipelineState.IN_PROGRESS)
                )
            ).scalars().all()
            for row in rows:
                row.state = PipelineState.QUEUED
                row.updated_at = datetime.now(UTC)
            await session.commit()
            return len(rows)


@dataclass(frozen=True)
class StateChange:
    """One recorded ADO state transition (detached from the session)."""

    work_item_id: int
    project: str
    state: str
    category: str
    assigned_to: str
    title: str
    entered_at: datetime


class StateHistoryRepository:
    """Append-only log of ADO state transitions — see ``WorkItemStateHistory``.

    Deliberately append-only: a transition is a fact about a moment, so nothing here
    updates a previous row. That is what lets the flow chart be re-derived for any past
    window without the numbers changing under it.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self, items: list[WorkItemInfo], categories: dict[str, str], *,
        now: datetime | None = None,
    ) -> int:
        """Record a row for each item whose state differs from its last recorded one.

        Idempotent by construction: calling it twice with unchanged items writes
        nothing. Returns the number of transitions written.

        ``categories`` maps state name → ADO state category; a state missing from it is
        recorded with an empty category rather than skipped — losing the transition
        entirely would put a permanent hole in the item's timeline, while an unknown
        category only weakens one bar of one chart.
        """
        if not items:
            return 0
        stamp = now or datetime.now(UTC)
        latest = await self.latest_states([i.id for i in items])
        written = 0
        async with self._db.session() as session:
            for item in items:
                state = (item.state or "").strip()
                if not state or latest.get(item.id) == state:
                    continue
                session.add(WorkItemStateHistory(
                    work_item_id=item.id,
                    project=item.project or "",
                    state=state,
                    category=categories.get(state, ""),
                    assigned_to=item.assigned_to or "",
                    title=item.title or "",
                    entered_at=stamp,
                ))
                written += 1
            if written:
                await session.commit()
        return written

    async def timeline(self, work_item_id: int, limit: int = 100) -> list[WorkItemStateHistory]:
        """Every recorded transition of ONE item, oldest first — how it got to here."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(WorkItemStateHistory)
                .where(WorkItemStateHistory.work_item_id == work_item_id)
                .order_by(WorkItemStateHistory.entered_at.asc())
                .limit(max(1, limit))
            )
            return list(rows.scalars().all())

    async def latest_states(self, ids: list[int]) -> dict[int, str]:
        """``{work_item_id: most recently recorded state}`` for ``ids``."""
        if not ids:
            return {}
        async with self._db.session() as session:
            # The newest row per item = the row whose id is the max for that item
            # (ids are monotonic within a table, and ties on entered_at are common
            # because a whole scan shares one timestamp).
            newest = (
                select(func.max(WorkItemStateHistory.id))
                .where(WorkItemStateHistory.work_item_id.in_(ids))
                .group_by(WorkItemStateHistory.work_item_id)
            )
            rows = await session.execute(
                select(WorkItemStateHistory).where(WorkItemStateHistory.id.in_(newest))
            )
            return {r.work_item_id: r.state for r in rows.scalars().all()}

    async def known_projects(self, ids: list[int] | None = None) -> dict[int, str]:
        """``{work_item_id: project}`` from the recorded history.

        The pipeline tables (``work_item_states``, ``execution_records``) key on an id
        alone, so a page built from them cannot tell which workspace a row belongs to.
        This answers that from data already on disk rather than re-fetching the items
        from Azure DevOps just to read one field."""
        async with self._db.session() as session:
            stmt = select(
                WorkItemStateHistory.work_item_id, WorkItemStateHistory.project
            ).where(WorkItemStateHistory.project != "")
            if ids:
                stmt = stmt.where(WorkItemStateHistory.work_item_id.in_(ids))
            rows = await session.execute(stmt.distinct())
            return {wid: project for wid, project in rows.all()}

    async def changes_since(self, since: datetime | None = None) -> list[StateChange]:
        """Every recorded transition at/after ``since`` (all of them when ``None``),
        oldest first — the input the flow chart and lead-time maths both read."""
        async with self._db.session() as session:
            stmt = select(WorkItemStateHistory)
            if since is not None:
                stmt = stmt.where(WorkItemStateHistory.entered_at >= since)
            rows = await session.execute(stmt.order_by(WorkItemStateHistory.entered_at))
            return [
                StateChange(
                    work_item_id=r.work_item_id, project=r.project, state=r.state,
                    category=r.category, assigned_to=r.assigned_to, title=r.title,
                    entered_at=r.entered_at,
                )
                for r in rows.scalars().all()
            ]

    async def first_seen(self) -> datetime | None:
        """When recording started — the point before which the Delivery page has no
        history and must say so instead of drawing an empty chart as though it were
        a quiet week."""
        async with self._db.session() as session:
            return (
                await session.execute(select(func.min(WorkItemStateHistory.entered_at)))
            ).scalar_one_or_none()

    async def prune(self, before: datetime) -> int:
        """Drop transitions older than ``before`` (retention)."""
        async with self._db.session() as session:
            result = await session.execute(
                delete(WorkItemStateHistory).where(WorkItemStateHistory.entered_at < before)
            )
            await session.commit()
            return result.rowcount or 0


class SdlcLoopStateRepository:
    """Resumable per-item progress for the closed-loop SDLC engine."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self, work_item_id: int) -> SdlcLoopState | None:
        async with self._db.session() as session:
            return await session.get(SdlcLoopState, work_item_id)

    async def save(
        self,
        work_item_id: int,
        *,
        profile: str,
        stage_index: int,
        iterations: int,
        branch: str,
        signals_json: str,
    ) -> None:
        """Upsert the loop progress (called after every stage step → crash-safe)."""
        async with self._db.session() as session:
            row = await session.get(SdlcLoopState, work_item_id)
            if row is None:
                row = SdlcLoopState(work_item_id=work_item_id)
                session.add(row)
            row.profile = profile
            row.stage_index = stage_index
            row.iterations = iterations
            row.branch = branch
            row.signals_json = signals_json
            row.updated_at = datetime.now(UTC)
            await session.commit()

    async def clear(self, work_item_id: int) -> None:
        async with self._db.session() as session:
            row = await session.get(SdlcLoopState, work_item_id)
            if row is not None:
                await session.delete(row)
                await session.commit()


class PlannedRunRepository:
    """Scheduled Planning-workbench runs (fire a batch of items at a future time)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, item_ids: list[int], run_at: datetime, note: str = "") -> int:
        async with self._db.session() as session:
            row = PlannedRun(
                item_ids=json.dumps(item_ids), run_at=run_at, status="pending",
                note=note, created_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            return row.id

    async def list_active(self) -> list[PlannedRun]:
        """Pending runs, soonest first (for the workbench panel)."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(PlannedRun).where(PlannedRun.status == "pending").order_by(PlannedRun.run_at)
            )
            return list(rows.scalars().all())

    async def due(self, now: datetime) -> list[PlannedRun]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(PlannedRun).where(
                    PlannedRun.status == "pending", PlannedRun.run_at <= now
                )
            )
            return list(rows.scalars().all())

    async def set_status(self, run_id: int, status: str) -> None:
        async with self._db.session() as session:
            row = await session.get(PlannedRun, run_id)
            if row is not None:
                row.status = status
                await session.commit()

    @staticmethod
    def ids_of(row: PlannedRun) -> list[int]:
        try:
            return [int(x) for x in json.loads(row.item_ids or "[]")]
        except (ValueError, TypeError):
            return []


class SchedulerHistoryRepository:
    """Recent dependency-scheduler decisions (bounded ring, persisted for the UI)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self, at: datetime, candidates: int, ready_ids: list[int],
        deferred: list[dict], *, keep: int,
    ) -> None:
        """Append one decision and prune to the ``keep`` newest rows. ``keep <= 0``
        is a no-op (history disabled)."""
        if keep <= 0:
            return
        async with self._db.session() as session:
            session.add(SchedulerDecision(
                at=at, candidates=candidates,
                ready_ids=json.dumps(list(ready_ids)),
                deferred_json=json.dumps(deferred),
            ))
            await session.commit()
            stale = (
                await session.execute(
                    select(SchedulerDecision.id)
                    .order_by(SchedulerDecision.at.desc())
                    .offset(keep)
                )
            ).scalars().all()
            if stale:
                await session.execute(
                    delete(SchedulerDecision).where(SchedulerDecision.id.in_(stale))
                )
                await session.commit()

    async def recent(self, limit: int = 20) -> list[dict]:
        """Newest-first decisions, decoded for the template."""
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(SchedulerDecision)
                    .order_by(SchedulerDecision.at.desc())
                    .limit(max(1, limit))
                )
            ).scalars().all()
        out: list[dict] = []
        for r in rows:
            with contextlib.suppress(ValueError, TypeError):
                out.append({
                    "at": r.at,
                    "candidates": r.candidates,
                    "ready": json.loads(r.ready_ids or "[]"),
                    "deferred": json.loads(r.deferred_json or "[]"),
                })
        return out


class AuditRepository:
    """Append-only audit trail of consequential actions (who did what, from where).

    ``record`` never raises into the caller — a broken audit write must not block
    the action being audited (the action itself is the priority; the log is not)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self, *, actor: str, source: str, action: str, target: str = "", detail: str = ""
    ) -> None:
        try:
            async with self._db.session() as session:
                session.add(AuditEvent(
                    at=datetime.now(UTC).replace(tzinfo=None),
                    actor=(actor or "")[:200], source=(source or "")[:50],
                    action=(action or "")[:100], target=(target or "")[:300],
                    detail=(detail or "")[:2000],
                ))
                await session.commit()
        except Exception:  # noqa: BLE001 — auditing must never break the audited action
            get_logger("data.audit").warning("audit write failed", action=action)

    async def for_target(self, target: str, limit: int = 50) -> list[AuditEvent]:
        """Events about one thing (a work-item id, a PR) — newest first."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(AuditEvent)
                .where(AuditEvent.target == str(target))
                .order_by(AuditEvent.at.desc())
                .limit(max(1, limit))
            )
            return list(rows.scalars().all())

    async def recent(self, limit: int = 100, action: str = "") -> list[AuditEvent]:
        """Newest-first events, optionally filtered by action prefix (e.g. "config.")."""
        async with self._db.session() as session:
            q = select(AuditEvent).order_by(AuditEvent.at.desc()).limit(max(1, limit))
            if action:
                q = q.where(AuditEvent.action.startswith(action))
            return list((await session.execute(q)).scalars().all())


@dataclass
class ReworkRow:
    """One work item's durable quality tally, for the Quality page."""

    work_item_id: int = 0
    retries: int = 0
    pr_revisions: int = 0
    sdlc_iterations: int = 0
    reopens: int = 0
    findings: int = 0        # auto-review findings recorded
    test_failures: int = 0
    rejections: int = 0      # human votes < 0 (rejected / waiting for author)
    approvals: int = 0       # human votes > 0
    worst_vote: int = 0      # lowest vote ever seen (-10 = rejected)
    last_at: datetime | None = None

    @property
    def rework(self) -> int:
        """Times this item had to be redone — the headline number."""
        return self.retries + self.pr_revisions + self.sdlc_iterations + self.reopens


class QualityRepository:
    """Append-only log of rework / review-quality events (:class:`QualityEvent`).

    ``record`` never raises into the caller: a broken analytics write must not break
    the run it is measuring — same contract as :class:`AuditRepository`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self, *, work_item_id: int, kind: str, value: int = 0, stage: str = "",
        actor: str = "", pr_id: int = 0, detail: str = "",
    ) -> None:
        try:
            async with self._db.session() as session:
                session.add(QualityEvent(
                    at=datetime.now(UTC).replace(tzinfo=None),
                    work_item_id=int(work_item_id or 0), kind=(kind or "")[:64],
                    stage=(stage or "")[:64], value=int(value or 0),
                    actor=(actor or "")[:200], pr_id=int(pr_id or 0),
                    detail=(detail or "")[:2000],
                ))
                await session.commit()
        except Exception:  # noqa: BLE001 — measurement must never break the measured run
            get_logger("data.quality").warning("quality write failed", kind=kind)

    async def recent(
        self, limit: int = 200, kind: str = "", work_item_id: int = 0, since: datetime | None = None
    ) -> list[QualityEvent]:
        """Newest-first events, optionally filtered."""
        async with self._db.session() as session:
            q = select(QualityEvent).order_by(QualityEvent.at.desc()).limit(max(1, limit))
            if kind:
                q = q.where(QualityEvent.kind == kind)
            if work_item_id:
                q = q.where(QualityEvent.work_item_id == work_item_id)
            if since is not None:
                q = q.where(QualityEvent.at >= since)
            return list((await session.execute(q)).scalars().all())

    async def rework_rows(self, since: datetime | None = None) -> list[ReworkRow]:
        """Per-item tallies, worst rework first.

        Aggregated in Python rather than SQL: the row count is one per event and the
        window is a handful of days, so the join-free read stays simple and the
        conditional-sum SQL (which differs across backends) is avoided.
        """
        async with self._db.session() as session:
            q = select(QualityEvent)
            if since is not None:
                q = q.where(QualityEvent.at >= since)
            events = list((await session.execute(q)).scalars().all())
        rows: dict[int, ReworkRow] = {}
        for e in events:
            row = rows.setdefault(e.work_item_id, ReworkRow(work_item_id=e.work_item_id))
            if e.kind == QualityKind.EXECUTION_RETRY:
                row.retries += 1
            elif e.kind == QualityKind.PR_REVISION:
                row.pr_revisions += 1
            elif e.kind == QualityKind.SDLC_ITERATION:
                row.sdlc_iterations += 1
            elif e.kind == QualityKind.REOPENED:
                row.reopens += 1
            elif e.kind == QualityKind.REVIEW_FINDING:
                row.findings += max(1, e.value)
            elif e.kind == QualityKind.TEST_FAILED:
                row.test_failures += 1
            elif e.kind == QualityKind.REVIEW_VOTE:
                if e.value < 0:
                    row.rejections += 1
                elif e.value > 0:
                    row.approvals += 1
                row.worst_vote = min(row.worst_vote, e.value)
            if row.last_at is None or e.at > row.last_at:
                row.last_at = e.at
        return sorted(rows.values(), key=lambda r: (-r.rework, -r.rejections, r.work_item_id))

    async def kind_totals(self, since: datetime | None = None) -> dict[str, int]:
        """``{kind: count}`` over the window — the page's headline tiles."""
        async with self._db.session() as session:
            q = select(QualityEvent.kind, func.count()).group_by(QualityEvent.kind)
            if since is not None:
                q = q.where(QualityEvent.at >= since)
            return {k: n for k, n in (await session.execute(q)).all()}


class AiConflictRepository:
    """AI-confirmed hidden conflicts (Planning Analyze → poller scheduling feedback)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _order(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a <= b else (b, a)

    async def record(
        self, a: int, b: int, score: int, modules: list[str] | None = None, reason: str = ""
    ) -> None:
        """Upsert one confirmed conflict pair (normalised so a<b, recorded once)."""
        if a == b:
            return
        lo, hi = self._order(a, b)
        async with self._db.session() as session:
            row = await session.get(AiConflict, (lo, hi))
            if row is None:
                row = AiConflict(a_id=lo, b_id=hi)
                session.add(row)
            row.score = max(0, min(100, int(score)))
            row.modules = json.dumps(modules or [])
            row.reason = (reason or "")[:500]
            row.created_at = datetime.now(UTC)
            await session.commit()

    async def related_edges(
        self, ids: list[int], min_score: int
    ) -> dict[int, set[int]]:
        """Symmetric ``id → {conflicting ids}`` for stored pairs where BOTH endpoints
        are in ``ids`` and ``score >= min_score``. Empty ``ids`` → ``{}``."""
        edges: dict[int, set[int]] = {}
        idset = set(ids)
        if not idset:
            return edges
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(AiConflict).where(AiConflict.score >= min_score)
                )
            ).scalars().all()
        for row in rows:
            if row.a_id in idset and row.b_id in idset:
                edges.setdefault(row.a_id, set()).add(row.b_id)
                edges.setdefault(row.b_id, set()).add(row.a_id)
        return edges


class NotificationHoldRepository:
    """Notices waiting for the notification window to open.

    Deliberately a queue, not a log: it is drained when the window opens and stays
    empty the rest of the time.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def hold(
        self, kind: str, title: str, body: str, work_item_id: int = 0, *, cap: int = 200
    ) -> int:
        """Queue one notice; returns how many older ones had to be dropped.

        Dropping oldest-first (rather than refusing the newest) keeps the summary
        useful: after a long weekend the recent notices are the ones somebody will act
        on, and the count of what was dropped is reported rather than hidden.
        """
        async with self._db.session() as session:
            session.add(HeldNotification(
                kind=kind or "", title=(title or "")[:500], body=body or "",
                work_item_id=work_item_id, at=datetime.now(UTC),
            ))
            await session.commit()
            total = int((await session.execute(
                select(func.count()).select_from(HeldNotification)
            )).scalar() or 0)
            if total <= cap:
                return 0
            excess = total - cap
            old = (await session.execute(
                select(HeldNotification.id).order_by(HeldNotification.id.asc()).limit(excess)
            )).scalars().all()
            await session.execute(
                delete(HeldNotification).where(HeldNotification.id.in_(list(old)))
            )
            await session.commit()
            return excess

    async def pending(self) -> list[HeldNotification]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(HeldNotification).order_by(HeldNotification.at.asc())
            )
            return list(rows.scalars().all())

    async def count(self) -> int:
        async with self._db.session() as session:
            return int((await session.execute(
                select(func.count()).select_from(HeldNotification)
            )).scalar() or 0)

    async def drain(self) -> list[HeldNotification]:
        """Take everything held and clear the queue in ONE transaction.

        Read-then-delete separately would re-send the batch if the process died between
        the two — the summary is a notification, and a duplicate wall of them at 08:00
        is the noise this feature exists to prevent.
        """
        async with self._db.session() as session:
            rows = list((await session.execute(
                select(HeldNotification).order_by(HeldNotification.at.asc())
            )).scalars().all())
            if rows:
                session.expunge_all()
                await session.execute(delete(HeldNotification))
                await session.commit()
            return rows


@dataclass(frozen=True)
class AlertDecision:
    """Whether one alert should be spoken about now, and why.

    ``reason`` is carried so the digest can SAY why a line reappeared ("tang tu 3 gio")
    instead of silently repeating itself, which is what made repetition read as a
    malfunction rather than as an escalation."""

    send: bool
    reason: str = ""
    escalated: bool = False
    first_time: bool = False


class AlertStateRepository:
    """The memory that stops an alert being reported in every single digest.

    One row per (kind, work item). The policy lives here rather than in the digest
    renderer because the same question is asked by chat, e-mail and anything else that
    reports actions: a rule implemented per channel is a rule that drifts.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._log = get_logger("data.alerts")

    async def decide(
        self, kind: str, work_item_id: int, *, age_hours: float, title: str = "",
        repeat_hours: int = 24, now: datetime | None = None,
    ) -> AlertDecision:
        """Should this alert be included in the message being built right now?

        Three ways the answer is yes, in the order they are checked:

        * **never reported** - always news;
        * **escalated** - the wait has at least DOUBLED since we last said so, which is
          the difference between "still waiting" and "now seriously stuck";
        * **overdue** - ``repeat_hours`` has passed and nobody acted.

        Snoozed and acknowledged alerts answer no, and an ack is deliberately NOT
        time-limited: someone said they were handling it, and asking again in an hour is
        how a tool teaches people to ignore it.
        """
        now = now or datetime.now(UTC)
        async with self._db.session() as session:
            row = (await session.execute(
                select(AlertState).where(
                    AlertState.kind == kind, AlertState.work_item_id == work_item_id
                )
            )).scalar_one_or_none()

            if row is None:
                session.add(AlertState(
                    kind=kind, work_item_id=work_item_id, title=(title or "")[:500],
                    first_seen_at=_naive(now), last_notified_at=_naive(now),
                    last_age_hours=age_hours, notify_count=1,
                ))
                await session.commit()
                return AlertDecision(send=True, first_time=True, reason="moi")

            # A row left over from a previous occurrence: the condition had cleared, so
            # this is a fresh problem that happens to reuse the key.
            if row.resolved_at is not None:
                row.resolved_at = None
                row.first_seen_at = _naive(now)
                row.last_notified_at = _naive(now)
                row.last_age_hours = age_hours
                row.notify_count = 1
                row.acked_at, row.acked_by, row.snoozed_until = None, "", None
                row.title = (title or row.title)[:500]
                await session.commit()
                return AlertDecision(send=True, first_time=True, reason="tai phat")

            if row.snoozed_until is not None and row.snoozed_until > _naive(now):
                return AlertDecision(send=False, reason="dang snooze")
            if row.acked_at is not None:
                return AlertDecision(send=False, reason="da ack")

            baseline = row.last_age_hours or 0.0
            escalated = baseline > 0 and age_hours >= baseline * 2
            due = False
            if repeat_hours > 0 and row.last_notified_at is not None:
                elapsed = (_naive(now) - row.last_notified_at).total_seconds() / 3600
                due = elapsed >= repeat_hours

            if not (escalated or due):
                return AlertDecision(send=False, reason="da bao")

            reason = f"tang tu {_hours_label(baseline)}" if escalated else "van chua xu ly"
            # A snooze that has run out is SPENT, not merely inactive. Leaving the stale
            # timestamp behind kept the row looking muted to ``muted_count``, so the
            # dashboard reported alerts as silenced while they were in fact being sent —
            # exactly the kind of unexplainable silence this table exists to remove.
            row.snoozed_until = None
            row.last_notified_at = _naive(now)
            row.last_age_hours = age_hours
            row.notify_count = int(row.notify_count or 0) + 1
            row.title = (title or row.title)[:500]
            await session.commit()
            return AlertDecision(send=True, escalated=escalated, reason=reason)

    async def clear(
        self, kind: str, keep_ids: set[int], *, now: datetime | None = None
    ) -> int:
        """Mark every alert of ``kind`` that is no longer active as resolved.

        Called with the ids the current report DID raise, so anything else has gone
        away. Resolving rather than deleting keeps ``notify_count`` and the first-seen
        time, and makes a recurrence visibly a recurrence.
        """
        now = now or datetime.now(UTC)
        async with self._db.session() as session:
            rows = (await session.execute(
                select(AlertState).where(
                    AlertState.kind == kind, AlertState.resolved_at.is_(None)
                )
            )).scalars().all()
            cleared = 0
            for row in rows:
                if row.work_item_id in keep_ids:
                    continue
                row.resolved_at = _naive(now)
                row.acked_at, row.acked_by, row.snoozed_until = None, "", None
                cleared += 1
            if cleared:
                await session.commit()
            return cleared

    async def ack(self, work_item_id: int, by: str = "", kind: str = "") -> int:
        """Acknowledge every open alert on an item (or one ``kind``). Returns how many."""
        return await self._mark(
            work_item_id, kind, acked_at=datetime.now(UTC), acked_by=(by or "")[:200],
        )

    async def snooze(self, work_item_id: int, days: int, kind: str = "") -> int:
        """Hide an item's alerts for ``days``. Returns how many were snoozed."""
        until = datetime.now(UTC) + timedelta(days=max(1, days))
        return await self._mark(work_item_id, kind, snoozed_until=until)

    async def unack(self, work_item_id: int, kind: str = "") -> int:
        """Undo an ack/snooze - the alert becomes reportable on the next digest."""
        return await self._mark(
            work_item_id, kind, acked_at=None, acked_by="", snoozed_until=None
        )

    async def _mark(self, work_item_id: int, kind: str, **fields) -> int:
        async with self._db.session() as session:
            stmt = select(AlertState).where(
                AlertState.work_item_id == work_item_id, AlertState.resolved_at.is_(None)
            )
            if kind:
                stmt = stmt.where(AlertState.kind == kind)
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                for key, value in fields.items():
                    setattr(
                        row, key,
                        _naive(value) if isinstance(value, datetime) else value,
                    )
            if rows:
                await session.commit()
            return len(rows)

    async def open_alerts(self) -> list[AlertState]:
        """Every alert currently live, oldest problem first - what the dashboard lists."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(AlertState)
                .where(AlertState.resolved_at.is_(None))
                .order_by(AlertState.first_seen_at.asc())
            )
            return list(rows.scalars().all())

    async def muted_count(self, now: datetime | None = None) -> int:
        """How many live alerts are acked or snoozed - shown so silence is explainable."""
        cutoff = _naive(now or datetime.now(UTC))
        async with self._db.session() as session:
            return int((await session.execute(
                select(func.count()).select_from(AlertState).where(
                    AlertState.resolved_at.is_(None),
                    or_(
                        AlertState.acked_at.is_not(None),
                        AlertState.snoozed_until > cutoff,
                    ),
                )
            )).scalar() or 0)


def _naive(value: datetime | None) -> datetime | None:
    """Drop tzinfo for comparison with DateTime columns, which SQLite stores naive.

    Mixing an aware ``now`` with a naive column raises at comparison time, which would
    turn "is this snoozed?" into a crashed digest."""
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _hours_label(hours: float) -> str:
    """A wait as Vietnamese prose - the same vocabulary the Delivery page uses."""
    if hours < 1:
        return f"{int(hours * 60)} phut"
    if hours < 48:
        return f"{int(hours)} gio"
    return f"{int(hours // 24)} ngay"


class SpecDriftRepository:
    """Where the agent's "this is not what the item said" reports are kept.

    Two readers, two shapes: the dashboard wants what is still OUTSTANDING, grouped by
    work item, so a BA can walk the list; the overview wants a count. Both come from
    here rather than from re-reading ADO comments, which cannot be filtered or ticked
    off.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, item: WorkItemInfo, pr_url: str, deviations: list) -> int:
        """Record this run's deviations for ``item``; returns how many were stored."""
        if not deviations:
            return 0
        now = datetime.now(UTC)
        async with self._db.session() as session:
            for dev in deviations:
                session.add(SpecDrift(
                    work_item_id=item.id, project=item.project or "",
                    title=(item.title or "")[:500], pr_url=pr_url or "",
                    kind=dev.kind, summary=dev.summary, detail=dev.detail,
                    where=dev.where, created_at=now,
                ))
            await session.commit()
        return len(deviations)

    async def open_drifts(self, limit: int = 500) -> list[SpecDrift]:
        """Everything not yet ticked off, newest first."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(SpecDrift)
                .where(SpecDrift.resolved_at.is_(None))
                .order_by(SpecDrift.created_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def for_item(self, work_item_id: int) -> list[SpecDrift]:
        """Every drift ever reported on one item, open or ticked off."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(SpecDrift)
                .where(SpecDrift.work_item_id == work_item_id)
                .order_by(SpecDrift.created_at.desc())
            )
            return list(rows.scalars().all())

    async def recent_resolved(self, limit: int = 50) -> list[SpecDrift]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(SpecDrift)
                .where(SpecDrift.resolved_at.is_not(None))
                .order_by(SpecDrift.resolved_at.desc())
                .limit(limit)
            )
            return list(rows.scalars().all())

    async def open_count(self) -> int:
        async with self._db.session() as session:
            rows = await session.execute(
                select(func.count())
                .select_from(SpecDrift)
                .where(SpecDrift.resolved_at.is_(None))
            )
            return int(rows.scalar() or 0)

    async def open_item_ids(self) -> set[int]:
        """Work items with at least one outstanding drift — for board/queue badges."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(SpecDrift.work_item_id).where(SpecDrift.resolved_at.is_(None))
            )
            return {int(r) for (r,) in rows.all()}

    async def resolve(self, work_item_id: int, by: str = "") -> int:
        """Tick off every outstanding drift on one item; returns how many were closed."""
        now = datetime.now(UTC)
        async with self._db.session() as session:
            rows = await session.execute(
                select(SpecDrift).where(
                    SpecDrift.work_item_id == work_item_id, SpecDrift.resolved_at.is_(None)
                )
            )
            found = list(rows.scalars().all())
            for row in found:
                row.resolved_at, row.resolved_by = now, (by or "")[:200]
            if found:
                await session.commit()
        return len(found)


class PrCommandRepository:
    """Restart-proof memory for the PR babysitter: revision budget spent per work
    item, and PR comment ids already dispatched."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def revision_count(self, work_item_id: int) -> int:
        async with self._db.session() as session:
            row = await session.get(PrCommandState, work_item_id)
            return row.revisions if row is not None else 0

    async def set_revision_count(self, work_item_id: int, revisions: int) -> None:
        async with self._db.session() as session:
            row = await session.get(PrCommandState, work_item_id)
            if row is None:
                row = PrCommandState(work_item_id=work_item_id)
                session.add(row)
            row.revisions = revisions
            row.updated_at = datetime.now(UTC)
            await session.commit()

    async def all_revision_counts(self) -> dict[int, int]:
        """Every work item with a spent revision budget → how much it spent.

        Needed because the budget is otherwise write-only: nothing ever cleared it, so an
        item that used its three revisions stayed capped for the life of the database.
        """
        async with self._db.session() as session:
            rows = await session.execute(
                select(PrCommandState.work_item_id, PrCommandState.revisions)
            )
            return {wid: n for wid, n in rows.all() if n}

    async def reset_revision_count(self, work_item_id: int) -> None:
        """Give the item its full revision budget back (its PR is no longer open)."""
        async with self._db.session() as session:
            row = await session.get(PrCommandState, work_item_id)
            if row is not None and row.revisions:
                row.revisions = 0
                row.updated_at = datetime.now(UTC)
                await session.commit()

    async def review_budget(self, pr_id: int) -> tuple[str, int, int]:
        """``(commit_id, advisory_runs, auto_reviews)`` for ``pr_id``; zeros if unseen."""
        async with self._db.session() as session:
            row = await session.get(PrReviewBudget, pr_id)
            if row is None:
                return "", 0, 0
            return row.commit_id, row.advisory_runs, row.auto_reviews

    async def record_advisory_run(self, pr_id: int, commit_id: str) -> int:
        """Count one advisory review of ``pr_id`` at ``commit_id``; returns the new count.

        A different commit resets the count to 1 — the point of the cap is to stop repeat
        reviews of *unchanged* code, not to ration reviews across a PR's life.
        """
        async with self._db.session() as session:
            row = await session.get(PrReviewBudget, pr_id)
            if row is None:
                # Set the counters explicitly: `default=0` on the column applies at
                # FLUSH time, so reading the attribute before then yields None.
                row = PrReviewBudget(
                    pr_id=pr_id, commit_id="", advisory_runs=0, auto_reviews=0
                )
                session.add(row)
            if row.commit_id != commit_id:
                row.commit_id, row.advisory_runs = commit_id, 0
            row.advisory_runs += 1
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return row.advisory_runs

    async def record_auto_review(self, pr_id: int) -> int:
        """Count one auto-review of ``pr_id`` (whole-life counter); returns the new count."""
        async with self._db.session() as session:
            row = await session.get(PrReviewBudget, pr_id)
            if row is None:
                # Set the counters explicitly: `default=0` on the column applies at
                # FLUSH time, so reading the attribute before then yields None.
                row = PrReviewBudget(
                    pr_id=pr_id, commit_id="", advisory_runs=0, auto_reviews=0
                )
                session.add(row)
            row.auto_reviews += 1
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return row.auto_reviews

    async def forget_pr_budget(self, pr_id: int) -> None:
        async with self._db.session() as session:
            row = await session.get(PrReviewBudget, pr_id)
            if row is not None:
                await session.delete(row)
                await session.commit()

    async def handled_comments(self, pr_id: int) -> set[tuple[int, int]]:
        """``(thread_id, comment_id)`` pairs already dispatched on this PR.

        Pairs, not bare comment ids: ADO numbers comments per THREAD, so a bare id is
        ambiguous across a PR (see ``HandledPrComment``)."""
        async with self._db.session() as session:
            rows = await session.execute(
                select(HandledPrComment.thread_id, HandledPrComment.comment_id).where(
                    HandledPrComment.pr_id == pr_id
                )
            )
            return {(t, c) for t, c in rows.all()}

    async def mark_handled(self, pr_id: int, thread_id: int, comment_id: int) -> None:
        async with self._db.session() as session:
            if await session.get(HandledPrComment, (pr_id, thread_id, comment_id)) is None:
                session.add(HandledPrComment(
                    pr_id=pr_id, thread_id=thread_id, comment_id=comment_id,
                    created_at=datetime.now(UTC),
                ))
                await session.commit()


@dataclass
class ReviewerSnapshot:
    """Detached view of one ``PrReviewerState`` row (safe to use outside a session)."""

    pr_id: int
    reviewer_id: str
    repo_id: str = ""
    display_name: str = ""
    unique_name: str = ""
    is_bot: bool = False
    vote: int = 0
    added_at: datetime | None = None
    last_vote_at: datetime | None = None
    reminded_at: datetime | None = None
    reviewed_commit: str = ""


def _snapshot(row: PrReviewerState) -> ReviewerSnapshot:
    return ReviewerSnapshot(
        pr_id=row.pr_id,
        reviewer_id=row.reviewer_id,
        repo_id=row.repo_id,
        display_name=row.display_name,
        unique_name=row.unique_name,
        is_bot=row.is_bot,
        vote=row.vote,
        added_at=row.added_at,
        last_vote_at=row.last_vote_at,
        reminded_at=row.reminded_at,
        reviewed_commit=row.reviewed_commit,
    )


class PrReviewerRepository:
    """Restart-proof memory for the reviewer tracker: who reviews which PR, their
    last-seen vote, and which reminders / auto-reviews already happened."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def reviewers_for_pr(self, pr_id: int) -> dict[str, ReviewerSnapshot]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(PrReviewerState).where(PrReviewerState.pr_id == pr_id)
            )
            return {r.reviewer_id: _snapshot(r) for r in rows.scalars().all()}

    async def all_reviewers(self) -> list[ReviewerSnapshot]:
        async with self._db.session() as session:
            rows = await session.execute(select(PrReviewerState))
            return [_snapshot(r) for r in rows.scalars().all()]

    async def upsert(
        self, pr_id: int, reviewer_id: str, *, repo_id: str = "", display_name: str = "",
        unique_name: str = "", is_bot: bool = False, vote: int = 0,
    ) -> ReviewerSnapshot:
        """Insert a newly-seen reviewer, or refresh vote/name fields of a known one.
        ``added_at`` is set only on insert; ``last_vote_at`` moves when the vote does."""
        now = datetime.now(UTC)
        async with self._db.session() as session:
            row = await session.get(PrReviewerState, (pr_id, reviewer_id))
            if row is None:
                row = PrReviewerState(
                    pr_id=pr_id, reviewer_id=reviewer_id, added_at=now
                )
                session.add(row)
            if vote != row.vote:
                row.last_vote_at = now
            row.repo_id = repo_id or row.repo_id
            row.display_name = display_name or row.display_name
            row.unique_name = unique_name or row.unique_name
            row.is_bot = is_bot
            row.vote = vote
            row.updated_at = now
            await session.commit()
            return _snapshot(row)

    async def mark_reminded(self, pr_id: int, reviewer_id: str) -> None:
        async with self._db.session() as session:
            row = await session.get(PrReviewerState, (pr_id, reviewer_id))
            if row is not None:
                row.reminded_at = datetime.now(UTC)
                row.updated_at = row.reminded_at
                await session.commit()

    async def set_reviewed_commit(self, pr_id: int, reviewer_id: str, commit: str) -> None:
        async with self._db.session() as session:
            row = await session.get(PrReviewerState, (pr_id, reviewer_id))
            if row is not None:
                now = datetime.now(UTC)
                row.reviewed_commit = commit
                row.reviewed_at = now
                row.updated_at = now
                await session.commit()

    async def count_reviewed_since(self, cutoff: datetime) -> int:
        """How many auto-review attempts (success or failure) completed since
        ``cutoff`` — for the Teams digest's activity stats."""
        async with self._db.session() as session:
            result = await session.execute(
                select(func.count()).select_from(PrReviewerState)
                .where(PrReviewerState.reviewed_at.is_not(None))
                .where(PrReviewerState.reviewed_at >= cutoff)
            )
            return result.scalar_one()

    async def count_reminded_since(self, cutoff: datetime) -> int:
        """How many reviewer reminders were sent since ``cutoff``."""
        async with self._db.session() as session:
            result = await session.execute(
                select(func.count()).select_from(PrReviewerState)
                .where(PrReviewerState.reminded_at.is_not(None))
                .where(PrReviewerState.reminded_at >= cutoff)
            )
            return result.scalar_one()

    async def remove_absent(self, pr_id: int, keep_reviewer_ids: set[str]) -> None:
        """Drop rows for reviewers no longer on the PR (removed by a human)."""
        async with self._db.session() as session:
            stmt = delete(PrReviewerState).where(PrReviewerState.pr_id == pr_id)
            if keep_reviewer_ids:
                stmt = stmt.where(PrReviewerState.reviewer_id.not_in(keep_reviewer_ids))
            await session.execute(stmt)
            await session.commit()

    async def delete_pr(self, pr_id: int) -> None:
        """Forget a PR entirely (it completed / was abandoned)."""
        async with self._db.session() as session:
            await session.execute(delete(PrReviewerState).where(PrReviewerState.pr_id == pr_id))
            await session.commit()


class ClaudeSessionRepository:
    """Per-branch Claude session ids for resume, with TTL-bounded reads."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, repo: str, branch: str, ttl_hours: int) -> str | None:
        """The stored session id for ``(repo, branch)`` if still within ``ttl_hours``;
        otherwise None (too old / unknown → the caller starts a fresh session)."""
        async with self._db.session() as session:
            row = await session.get(ClaudeSession, (repo, branch))
            if row is None or not row.session_id:
                return None
            updated = row.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if ttl_hours and (datetime.now(UTC) - updated).total_seconds() > ttl_hours * 3600:
                return None
            return row.session_id

    async def save(self, repo: str, branch: str, session_id: str) -> None:
        if not session_id:
            return
        async with self._db.session() as session:
            row = await session.get(ClaudeSession, (repo, branch))
            if row is None:
                row = ClaudeSession(repo=repo, branch=branch)
                session.add(row)
            row.session_id = session_id
            row.updated_at = datetime.now(UTC)
            await session.commit()


class SyncStateRepository:
    """Persisted memory for the state-sync service — which merged PRs it has already
    handled, so a restart never re-transitions items that have moved on."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def seen_merged_prs(self) -> set[int]:
        async with self._db.session() as session:
            rows = await session.execute(select(MergedPr.pr_id))
            return set(rows.scalars().all())

    async def mark_merged_pr(self, pr_id: int, work_item_id: int, state: str) -> None:
        async with self._db.session() as session:
            row = await session.get(MergedPr, pr_id)
            if row is None:
                row = MergedPr(pr_id=pr_id)
                session.add(row)
            row.work_item_id = work_item_id
            row.state = state
            row.created_at = datetime.now(UTC)
            await session.commit()

    async def prune_merged_prs(self, keep: int = 5000) -> int:
        """Keep only the most-recent ``keep`` merged-PR rows (highest pr_id = newest),
        deleting older ones so the table can't grow unbounded over the project's life.
        Safe: ADO only re-surfaces recent completed PRs, far fewer than ``keep``, so a
        pruned id is never re-seen. Returns the number of rows deleted."""
        async with self._db.session() as session:
            stale = (
                await session.execute(
                    select(MergedPr.pr_id).order_by(MergedPr.pr_id.desc()).offset(keep)
                )
            ).scalars().all()
            if not stale:
                return 0
            await session.execute(delete(MergedPr).where(MergedPr.pr_id.in_(stale)))
            await session.commit()
            return len(stale)
