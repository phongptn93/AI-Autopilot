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
    AuditEvent,
    ClaudeSession,
    ExecutionRecord,
    ExecutionStatus,
    HandledPrComment,
    MergedPr,
    PipelineState,
    PlannedRun,
    PrCommandState,
    PrReviewBudget,
    PrReviewerState,
    SchedulerDecision,
    SdlcLoopState,
    WorkItemState,
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

    async def search(
        self,
        *,
        status: str | None = None,
        category: str | None = None,
        q: str | None = None,
        dfrom: str | None = None,
        dto: str | None = None,
        trigger_tag: str | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> tuple[list[ExecutionRecord], int]:
        """Filtered + paginated executions, newest-first, with the total match count.

        Filters: status, category, free-text ``q`` (work-item id or title), started
        date range (``dfrom``/``dto`` as ``YYYY-MM-DD``), and trigger tag.
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

    async def get_efficiency(self, trigger_tag: str | None = None) -> EfficiencyStats:
        """Aggregate effort figures for the Overview's efficiency cards."""
        conds = []
        if trigger_tag:
            conds.append(ExecutionRecord.trigger_tag == trigger_tag)
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

    async def recent(self, limit: int = 100, action: str = "") -> list[AuditEvent]:
        """Newest-first events, optionally filtered by action prefix (e.g. "config.")."""
        async with self._db.session() as session:
            q = select(AuditEvent).order_by(AuditEvent.at.desc()).limit(max(1, limit))
            if action:
                q = q.where(AuditEvent.action.startswith(action))
            return list((await session.execute(q)).scalars().all())


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

    async def handled_comments(self, pr_id: int) -> set[int]:
        async with self._db.session() as session:
            rows = await session.execute(
                select(HandledPrComment.comment_id).where(HandledPrComment.pr_id == pr_id)
            )
            return set(rows.scalars().all())

    async def mark_handled(self, pr_id: int, comment_id: int) -> None:
        async with self._db.session() as session:
            if await session.get(HandledPrComment, (pr_id, comment_id)) is None:
                session.add(HandledPrComment(
                    pr_id=pr_id, comment_id=comment_id, created_at=datetime.now(UTC)
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
