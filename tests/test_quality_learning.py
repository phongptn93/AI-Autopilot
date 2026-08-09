"""The durable quality log and the learning funnel it feeds.

Covers the gap that made the learning loop inert: every execution path READ lessons
but only the replaced legacy path ever WROTE one, so the loop drew from a well
nothing filled. Recording and learning now happen in one call — these tests pin that
they cannot drift apart again.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ai_autopilot import lessons
from ai_autopilot.config import Settings
from ai_autopilot.data import QualityKind, QualityRepository
from ai_autopilot.data.database import Database
from ai_autopilot.learning import QualityLog, lesson_text


@pytest.fixture
async def repo(tmp_path: Path) -> QualityRepository:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'q.db'}")
    await db.create_all()
    return QualityRepository(db)


def _log(repo, tmp_path, **over) -> QualityLog:
    cfg = Settings(
        learning_loop_enabled=True, workspace_directory=str(tmp_path / "ws"), **over
    )
    return QualityLog(repo, cfg)


# ── the append-only record ────────────────────────────────────────────────────


async def test_events_survive_and_aggregate(repo: QualityRepository):
    for kind in (QualityKind.EXECUTION_RETRY, QualityKind.PR_REVISION, QualityKind.REOPENED):
        await repo.record(work_item_id=7, kind=kind, value=1)
    await repo.record(work_item_id=7, kind=QualityKind.REVIEW_VOTE, value=-10, actor="Human")
    await repo.record(work_item_id=9, kind=QualityKind.TEST_FAILED, detail="3 failed")

    rows = await repo.rework_rows()
    by_id = {r.work_item_id: r for r in rows}
    assert by_id[7].rework == 3          # retry + revision + reopen
    assert by_id[7].rejections == 1
    assert by_id[7].worst_vote == -10
    assert by_id[9].test_failures == 1
    assert by_id[9].rework == 0
    assert rows[0].work_item_id == 7     # worst rework first

    assert (await repo.kind_totals())[QualityKind.EXECUTION_RETRY] == 1


async def test_since_window_filters(repo: QualityRepository):
    await repo.record(work_item_id=1, kind=QualityKind.EXECUTION_RETRY)
    future = datetime.now() + timedelta(days=1)
    assert await repo.rework_rows(since=future) == []
    assert await repo.kind_totals(since=future) == {}


async def test_record_never_raises_into_the_caller(tmp_path: Path):
    # A broken analytics write must not break the run it is measuring.
    broken = QualityRepository(Database(f"sqlite+aiosqlite:///{tmp_path / 'missing'}/x.db"))
    await broken.record(work_item_id=1, kind=QualityKind.EXECUTION_RETRY)  # must not raise


# ── the lesson mapping ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind,teaches",
    [
        (QualityKind.REVIEW_FINDING, True),
        (QualityKind.TEST_FAILED, True),
        (QualityKind.REVIEW_VOTE, True),
        (QualityKind.REOPENED, True),
        (QualityKind.EXECUTION_RETRY, False),   # an infra flake teaches no rule
        (QualityKind.PR_REVISION, False),       # the review comment is the lesson
    ],
)
def test_lesson_mapping(kind: str, teaches: bool):
    assert bool(lesson_text(kind, "detail here", "Reviewer")) is teaches


# ── the funnel: record → learn ────────────────────────────────────────────────


async def test_blocking_vote_becomes_a_lesson(repo: QualityRepository, tmp_path: Path):
    log = _log(repo, tmp_path)
    await log.record(
        work_item_id=42, kind=QualityKind.REVIEW_VOTE, value=-10,
        actor="Phong Pham", detail="Rejected",
    )
    carried = lessons.recent(str(tmp_path / "ws"), [], limit=8)
    assert any("Phong Pham" in line for line in carried)
    assert (await repo.recent())[0].value == -10   # and it is still measurable


async def test_approval_is_recorded_but_teaches_nothing(repo: QualityRepository, tmp_path: Path):
    log = _log(repo, tmp_path)
    await log.record(work_item_id=42, kind=QualityKind.REVIEW_VOTE, value=10, actor="Ana")
    assert lessons.recent(str(tmp_path / "ws"), [], limit=8) == []
    assert len(await repo.recent()) == 1


async def test_lessons_are_carried_by_a_brief_for_any_repo(repo: QualityRepository, tmp_path: Path):
    # The shared bucket is what makes a work-item-level signal (a rejection spanning
    # repos, a reopen) reachable from a brief built for a NAMED repo.
    log = _log(repo, tmp_path)
    await log.record(work_item_id=1, kind=QualityKind.TEST_FAILED, detail="2 failed")
    brief = lessons.lessons_brief(str(tmp_path / "ws"), ["some-repo"], limit=8)
    assert "2 failed" in brief


async def test_learning_off_records_but_does_not_write_lessons(
    repo: QualityRepository, tmp_path: Path
):
    log = QualityLog(
        repo,
        Settings(learning_loop_enabled=False, workspace_directory=str(tmp_path / "ws")),
    )
    await log.record(work_item_id=1, kind=QualityKind.TEST_FAILED, detail="boom")
    assert len(await repo.recent()) == 1
    assert not (tmp_path / "ws" / ".autopilot").exists()


async def test_empty_workspace_does_not_silently_pretend_to_learn(
    repo: QualityRepository, caplog
):
    # The failure mode that made this look enabled while doing nothing: learning on,
    # workspace unset. It must record, skip the lesson, and SAY so.
    log = QualityLog(repo, Settings(learning_loop_enabled=True, workspace_directory=""))
    await log.record(work_item_id=1, kind=QualityKind.TEST_FAILED, detail="boom")
    assert len(await repo.recent()) == 1


async def test_single_repo_lesson_also_filed_under_that_repo(
    repo: QualityRepository, tmp_path: Path
):
    ws = str(tmp_path / "ws")
    log = QualityLog(
        repo, Settings(learning_loop_enabled=True, workspace_directory=ws),
        repos_provider=lambda: ["only-repo"],
    )
    await log.record(work_item_id=1, kind=QualityKind.TEST_FAILED, detail="boom")
    assert any("boom" in line for line in lessons.read_lessons(ws, "only-repo", limit=8))


async def test_quality_page_renders_with_data(tmp_path: Path):
    """The populated branch of the template — the smoke test only ever hits the empty
    state, where none of the tallies, pills or `sum(attribute=...)` are evaluated."""
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app

    settings = Settings(
        dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    )
    with TestClient(create_app(settings)) as client:
        q = client.app.state.container.quality_events
        await q.record(work_item_id=7812, kind=QualityKind.EXECUTION_RETRY, value=2,
                       detail="build failed")
        await q.record(work_item_id=7812, kind=QualityKind.PR_REVISION, value=1)
        await q.record(work_item_id=7812, kind=QualityKind.REOPENED, actor="human")
        await q.record(work_item_id=7812, kind=QualityKind.REVIEW_VOTE, value=-10,
                       actor="Reviewer", detail="Rejected")
        await q.record(work_item_id=99, kind=QualityKind.TEST_FAILED, detail="3 failed")
        await q.record(work_item_id=99, kind=QualityKind.REVIEW_VOTE, value=10, actor="Ana")

        body = client.get("/dashboard/quality").text
        assert "#7812" in body
        assert "blocked (-10)" in body        # worst vote pill
        assert "approved" in body             # the other item's pill
        assert "3 failed" in body             # event-log detail
        # 3 rework events on 7812 + 0 on 99, over 2 items → the headline ratio.
        assert "1.50" in body

        # The kind filter narrows the event log without emptying the table.
        filtered = client.get("/dashboard/quality?kind=test_failed").text
        assert "3 failed" in filtered
        assert "build failed" not in filtered


async def test_learning_survives_a_broken_repos_provider(
    repo: QualityRepository, tmp_path: Path
):
    ws = str(tmp_path / "ws")

    def explode():
        raise RuntimeError("workspace scan failed")

    log = QualityLog(
        repo, Settings(learning_loop_enabled=True, workspace_directory=ws),
        repos_provider=explode,
    )
    await log.record(work_item_id=1, kind=QualityKind.TEST_FAILED, detail="boom")
    assert lessons.recent(ws, [], limit=8)   # shared bucket still got it
