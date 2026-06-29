"""Execution history repository (ported from ``ExecutionRepository``)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select

from ai_autopilot.data.database import Database
from ai_autopilot.data.entities import ExecutionRecord, ExecutionStatus
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

    async def start_execution(self, item: WorkItemInfo, skill: str) -> int:
        async with self._db.session() as session:
            record = ExecutionRecord(
                work_item_id=item.id,
                title=item.title,
                category=str(item.category),
                skill_used=skill,
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

    async def get_recent(self, count: int = 50) -> list[ExecutionRecord]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(ExecutionRecord)
                .order_by(ExecutionRecord.started_at.desc())
                .limit(count)
            )
            return list(rows.scalars().all())

    async def get_by_work_item(self, work_item_id: int) -> list[ExecutionRecord]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(ExecutionRecord)
                .where(ExecutionRecord.work_item_id == work_item_id)
                .order_by(ExecutionRecord.started_at.desc())
            )
            return list(rows.scalars().all())

    async def get_stats(self, since: datetime | None = None) -> ExecutionStats:
        async with self._db.session() as session:
            base = select(func.count()).select_from(ExecutionRecord)
            if since is not None:
                base = base.where(ExecutionRecord.started_at >= since)

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
            avg = (await session.execute(avg_query)).scalar() or 0.0

            return ExecutionStats(
                total=total, success=success, failed=failed, avg_duration=float(avg)
            )
