"""Tests for the persisted pipeline-state repository (Phase 2)."""

from __future__ import annotations

import pytest

from ai_autopilot.data import Database, PipelineState, StateRepository


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    await db.create_all()
    yield StateRepository(db)
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
