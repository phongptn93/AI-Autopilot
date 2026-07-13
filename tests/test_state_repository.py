"""Tests for the persisted pipeline-state repository (Phase 2)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ai_autopilot.data import (
    AiConflictRepository,
    Database,
    PipelineState,
    PlannedRunRepository,
    SchedulerHistoryRepository,
    StateRepository,
    SyncStateRepository,
)


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    await db.create_all()
    yield StateRepository(db)
    await db.dispose()


async def test_sync_repo_persists_merged_prs(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
    await db.create_all()
    try:
        repo = SyncStateRepository(db)
        assert await repo.seen_merged_prs() == set()
        await repo.mark_merged_pr(7, 42, "Ready to Deploy")
        await repo.mark_merged_pr(9, 43, "Ready to Deploy")
        await repo.mark_merged_pr(7, 42, "Ready to Deploy")  # idempotent upsert
        assert await repo.seen_merged_prs() == {7, 9}
    finally:
        await db.dispose()


async def test_planned_run_repo_create_due_cancel(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'planned.db'}")
    await db.create_all()
    try:
        repo = PlannedRunRepository(db)
        now = datetime.now()
        id1 = await repo.create([1, 2], now - timedelta(minutes=1), note="past")
        id2 = await repo.create([3], now + timedelta(hours=1), note="future")
        assert {r.id for r in await repo.list_active()} == {id1, id2}

        due = await repo.due(now)
        assert [r.id for r in due] == [id1]
        assert PlannedRunRepository.ids_of(due[0]) == [1, 2]

        await repo.set_status(id1, "done")
        assert {r.id for r in await repo.list_active()} == {id2}
    finally:
        await db.dispose()


async def test_scheduler_history_records_and_prunes(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'hist.db'}")
    await db.create_all()
    try:
        repo = SchedulerHistoryRepository(db)
        base = datetime.now()
        for n in range(5):
            await repo.record(
                base + timedelta(minutes=n), candidates=3, ready_ids=[n],
                deferred=[{"id": n + 100, "title": "t", "reason": "wait"}], keep=3,
            )
        rows = await repo.recent(limit=10)
        assert len(rows) == 3                                   # pruned to keep=3
        assert [r["ready"] for r in rows] == [[4], [3], [2]]    # newest first
        assert rows[0]["deferred"][0]["id"] == 104

        # keep=0 → history disabled (no-op).
        await repo.record(base, 1, [9], [{"id": 1, "title": "", "reason": "x"}], keep=0)
        assert len(await repo.recent()) == 3
    finally:
        await db.dispose()


async def test_ai_conflict_repo_upsert_order_and_filter(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'ai.db'}")
    await db.create_all()
    try:
        repo = AiConflictRepository(db)
        # Unordered input normalises to a<b, and re-record upserts (not duplicates).
        await repo.record(9, 4, 80, modules=["svc"], reason="same service")
        await repo.record(4, 9, 85)                 # same pair, new score → upsert
        await repo.record(4, 7, 40)                 # below default feed threshold
        await repo.record(5, 5, 99)                 # self-pair ignored

        # Both endpoints must be in the candidate set.
        edges = await repo.related_edges([4, 9, 7], min_score=60)
        assert edges == {4: {9}, 9: {4}}            # (4,9)=85 kept; (4,7)=40 filtered

        # Raise the bar → nothing qualifies.
        assert await repo.related_edges([4, 9], min_score=90) == {}
        # Missing endpoint → no edge.
        assert await repo.related_edges([9], min_score=60) == {}
    finally:
        await db.dispose()


async def test_set_then_get(repo):
    await repo.set(1, PipelineState.IN_PROGRESS, title="t")
    row = await repo.get(1)
    assert row.state == PipelineState.IN_PROGRESS
    assert row.title == "t"


async def test_upsert_preserves_unset_fields(repo):
    await repo.set(1, PipelineState.IN_REVIEW, pr_url="https://pr")
    await repo.set(1, PipelineState.DONE)  # pr_url not passed → kept
    row = await repo.get(1)
    assert row.state == PipelineState.DONE
    assert row.pr_url == "https://pr"


async def test_requeue_in_progress_resumes(repo):
    await repo.set(1, PipelineState.IN_PROGRESS)
    await repo.set(2, PipelineState.DONE)
    requeued = await repo.requeue_in_progress()
    assert requeued == 1
    assert (await repo.get(1)).state == PipelineState.QUEUED
    assert (await repo.get(2)).state == PipelineState.DONE  # terminal untouched


async def test_all(repo):
    await repo.set(1, PipelineState.QUEUED)
    await repo.set(2, PipelineState.DONE)
    states = {s.work_item_id: s.state for s in await repo.all()}
    assert states == {1: PipelineState.QUEUED, 2: PipelineState.DONE}
