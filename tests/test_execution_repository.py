"""Tests for ExecutionRepository — trigger-tag filtering (dashboard Overview)."""

from __future__ import annotations

import pytest

from ai_autopilot.data import Database
from ai_autopilot.data.repository import ExecutionRepository
from ai_autopilot.models import ExecutionResult, WorkItemInfo


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'exec.db'}")
    await db.create_all()
    yield ExecutionRepository(db)
    await db.dispose()


async def _record(repo, work_item_id: int, tag: str | None, *, success: bool) -> None:
    item = WorkItemInfo(id=work_item_id, title=f"t{work_item_id}")
    rid = await repo.start_execution(item, "agent", trigger_tag=tag)
    result = (
        ExecutionResult.ok(work_item_id, "agent", "done")
        if success
        else ExecutionResult.fail(work_item_id, "agent", "boom")
    )
    await repo.complete_execution(rid, result)


async def test_pr_urls_persisted(repo):
    import json

    item = WorkItemInfo(id=1, title="t")
    rid = await repo.start_execution(item, "agent")
    result = ExecutionResult.ok(1, "agent", "done")
    result.pr_urls = ["https://pr/1", "https://pr/2"]
    await repo.complete_execution(rid, result)
    rec = (await repo.get_recent())[0]
    assert json.loads(rec.pr_urls) == ["https://pr/1", "https://pr/2"]


async def test_stats_and_recent_filter_by_trigger_tag(repo):
    await _record(repo, 1, "squad-a", success=True)
    await _record(repo, 2, "squad-a", success=False)
    await _record(repo, 3, "squad-b", success=True)
    await _record(repo, 4, None, success=True)  # legacy row (no tag)

    all_stats = await repo.get_stats()
    assert all_stats.total == 4

    a = await repo.get_stats(trigger_tag="squad-a")
    assert (a.total, a.success, a.failed) == (2, 1, 1)

    b_recent = await repo.get_recent(trigger_tag="squad-b")
    assert [r.work_item_id for r in b_recent] == [3]

    # None / "all" → everything, including the untagged legacy row.
    assert len(await repo.get_recent()) == 4


async def test_a_wallclock_duration_is_written_back_onto_the_result(repo):
    """The interactive path never times itself, so ExecutionResult.duration_seconds stays
    0.0. The DB row was corrected from started_at — but only the row, so History showed a
    real duration while the Teams card, which reads the result object and is notified after
    this call, showed 0:00 for every interactive run."""
    item = WorkItemInfo(id=7, title="t")
    rid = await repo.start_execution(item, "interactive:autopilot-7")
    result = ExecutionResult.ok(7, "agent", "done")
    assert result.duration_seconds == 0.0            # nothing measured it

    await repo.complete_execution(rid, result)

    assert result.duration_seconds > 0.0             # the result now agrees with the row
    recent = await repo.get_recent(1)
    assert recent[0].duration_seconds == pytest.approx(result.duration_seconds)


async def test_a_measured_duration_is_left_alone(repo):
    """A path that timed itself must not have its number replaced by wall-clock, which
    includes queueing and would silently inflate it."""
    item = WorkItemInfo(id=8, title="t")
    rid = await repo.start_execution(item, "agent")
    result = ExecutionResult.ok(8, "agent", "done")
    result.duration_seconds = 12.5
    await repo.complete_execution(rid, result)
    assert result.duration_seconds == 12.5
    assert (await repo.get_recent(1))[0].duration_seconds == pytest.approx(12.5)
