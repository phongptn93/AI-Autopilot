"""Execution history repository (ported from ``ExecutionRepository``)."""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select

from ai_autopilot.data.database import Database
from ai_autopilot.data.entities import (
    AiConflict,
    ExecutionRecord,
    ExecutionStatus,
    MergedPr,
    PipelineState,
    PlannedRun,
    SchedulerDecision,
    SdlcLoopState,
    WorkItemState,
)
from ai_autopilot.models import ExecutionResult, WorkItemInfo


@dataclass
class ExecutionStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    avg_duration: float = 0.0


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
            record.duration_seconds = result.duration_seconds
            record.completed_at = datetime.now(UTC)
            if result.cost_tokens:
                record.cost_tokens = result.cost_tokens
            await session.commit()

    async def mark_retrying(self, record_id: int, retry_count: int) -> None:
        async with self._db.session() as session:
            record = await session.get(ExecutionRecord, record_id)
            if record is None:
                return
            record.status = ExecutionStatus.RETRYING
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

    async def get_recent(
        self, count: int = 50, trigger_tag: str | None = None
    ) -> list[ExecutionRecord]:
        async with self._db.session() as session:
            query = select(ExecutionRecord)
            if trigger_tag:
                query = query.where(ExecutionRecord.trigger_tag == trigger_tag)
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
        self, since: datetime | None = None, trigger_tag: str | None = None
    ) -> ExecutionStats:
        async with self._db.session() as session:
            base = select(func.count()).select_from(ExecutionRecord)
            if since is not None:
                base = base.where(ExecutionRecord.started_at >= since)
            if trigger_tag:
                base = base.where(ExecutionRecord.trigger_tag == trigger_tag)

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
            avg = (await session.execute(avg_query)).scalar() or 0.0

            return ExecutionStats(
                total=total, success=success, failed=failed, avg_duration=float(avg)
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
