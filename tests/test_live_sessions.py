"""Live interactive sessions survive a restart: DB is the source of truth for _live."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_autopilot.data import Database, ExecutionRepository, LiveSessionRepository
from ai_autopilot.data.entities import ExecutionRecord, ExecutionStatus
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.poller import AdoPollerService


@pytest.fixture
async def db(tmp_path: Path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
    await database.create_all()
    yield database
    await database.dispose()


def _item(i: int) -> WorkItemInfo:
    return WorkItemInfo(id=i, title=f"item {i}", work_item_type="Task", state="Active")


async def test_repository_round_trip(db):
    repo = LiveSessionRepository(db)
    await repo.put(7, 41, "C:/scratch/r7", "qc")
    await repo.put(7, 42, "C:/scratch/r7b", "dev")          # same item: replaced, not added
    await repo.put(8, 43)
    rows = {r.work_item_id: r for r in await repo.all()}
    assert set(rows) == {7, 8}
    assert (rows[7].record_id, rows[7].run_dir, rows[7].profile) == (42, "C:/scratch/r7b", "dev")
    await repo.delete(7)
    assert [r.work_item_id for r in await repo.all()] == [8]


async def test_fail_running_keeps_the_runs_of_surviving_sessions(db):
    runs = ExecutionRepository(db)
    live_run = await runs.start_execution(_item(1), "interactive:abc")
    dead_run = await runs.start_execution(_item(2), "headless")
    assert await runs.fail_running(keep={live_run}) == 1
    async with db.session() as s:
        status = {r.id: r.status for r in
                  (await s.execute(ExecutionRecord.__table__.select())).fetchall()}
    assert status[live_run] == ExecutionStatus.RUNNING
    assert status[dead_run] == ExecutionStatus.FAILED


def _poller(container):
    log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    return SimpleNamespace(_c=container, _log=log, _live={}, _live_profiles={}, _live_dirs={})


async def test_track_restore_untrack_across_a_restart(db):
    container = SimpleNamespace(live_session_repo=LiveSessionRepository(db))
    before = _poller(container)
    await AdoPollerService._track_live(before, 5, 99, "C:/scratch/r5", "qc")
    assert before._live == {5: 99} and before._live_profiles == {5: "qc"}

    after = _poller(container)                       # a new process
    kept = await AdoPollerService._restore_live_sessions(after)
    assert kept == {99}
    assert after._live == {5: 99}
    assert after._live_dirs == {5: "C:/scratch/r5"} and after._live_profiles == {5: "qc"}

    await AdoPollerService._untrack_live(after, 5)
    assert after._live == {} and after._live_profiles == {}
    assert await container.live_session_repo.all() == []


async def test_a_broken_store_never_stops_the_poller():
    class Broken:
        async def put(self, *a, **k):
            raise RuntimeError("db locked")

        async def delete(self, *a, **k):
            raise RuntimeError("db locked")

        async def all(self):
            raise RuntimeError("db locked")

    p = _poller(SimpleNamespace(live_session_repo=Broken()))
    await AdoPollerService._track_live(p, 1, 2, "", "")
    assert p._live == {1: 2}                         # memory still holds it, as before
    await AdoPollerService._untrack_live(p, 1)
    assert p._live == {}
    assert await AdoPollerService._restore_live_sessions(p) == set()


async def test_no_store_is_the_old_in_memory_behaviour():
    p = _poller(SimpleNamespace())
    await AdoPollerService._track_live(p, 3, 4, "", "dev")
    assert p._live == {3: 4}
    assert await AdoPollerService._restore_live_sessions(p) == set()
