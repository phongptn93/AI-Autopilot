"""Tests for the PR-command persistence layer and efficiency aggregates."""

from __future__ import annotations

import pytest

from ai_autopilot.data import Database, ExecutionRepository, PrCommandRepository
from ai_autopilot.models import ExecutionResult, WorkItemInfo


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.create_all()
    yield database
    await database.dispose()


async def test_pr_command_repo_roundtrip(db: Database):
    repo = PrCommandRepository(db)

    assert await repo.revision_count(42) == 0          # unknown item → 0
    await repo.set_revision_count(42, 2)
    assert await repo.revision_count(42) == 2
    await repo.set_revision_count(42, 3)               # upsert, not insert
    assert await repo.revision_count(42) == 3

    assert await repo.handled_comments(7) == set()
    await repo.mark_handled(7, 100)
    await repo.mark_handled(7, 101)
    await repo.mark_handled(7, 100)                    # idempotent
    assert await repo.handled_comments(7) == {100, 101}
    assert await repo.handled_comments(8) == set()     # per-PR isolation


async def test_efficiency_stats_aggregate(db: Database):
    repo = ExecutionRepository(db)
    for item_id, tokens in ((1, 100), (1, 50), (2, 25)):
        rid = await repo.start_execution(WorkItemInfo(id=item_id, title="t"), "/x")
        result = ExecutionResult.ok(item_id, "/x", "out")
        result.cost_tokens = tokens
        await repo.complete_execution(rid, result)

    eff = await repo.get_efficiency()
    assert eff.distinct_items == 2
    assert eff.total_runs == 3
    assert eff.total_tokens == 175
    assert eff.avg_runs_per_item == 1.5


async def test_efficiency_stats_empty(db: Database):
    eff = await ExecutionRepository(db).get_efficiency()
    assert eff.distinct_items == 0 and eff.avg_runs_per_item == 0.0
