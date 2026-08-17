"""The state-transition log behind the Delivery page's clock.

Two properties matter more than anything else here: recording the same unchanged items
repeatedly must write NOTHING (the recorder runs every few minutes forever), and a row
once written must never be rewritten (a chart re-drawn next month has to show the same
numbers it showed today).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_autopilot.data import Database, StateHistoryRepository
from ai_autopilot.models import WorkItemInfo

CATEGORIES = {"New": "Proposed", "Active": "InProgress", "Closed": "Completed"}


@pytest.fixture
async def repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    await db.create_all()
    yield StateHistoryRepository(db)
    await db.dispose()


def _item(wid: int, state: str, **over) -> WorkItemInfo:
    return WorkItemInfo(
        id=wid, title=f"Item {wid}", state=state,
        project=over.get("project", "P1"), assigned_to=over.get("owner", "An"),
    )


async def test_first_recording_captures_a_baseline_for_every_item(repo):
    written = await repo.record([_item(1, "New"), _item(2, "Active")], CATEGORIES)
    assert written == 2
    assert await repo.latest_states([1, 2]) == {1: "New", 2: "Active"}


async def test_recording_unchanged_items_writes_nothing(repo):
    items = [_item(1, "Active")]
    await repo.record(items, CATEGORIES)
    assert await repo.record(items, CATEGORIES) == 0
    assert await repo.record(items, CATEGORIES) == 0
    assert len(await repo.changes_since()) == 1


async def test_only_the_moved_item_is_recorded(repo):
    await repo.record([_item(1, "New"), _item(2, "New")], CATEGORIES)
    written = await repo.record([_item(1, "Active"), _item(2, "New")], CATEGORIES)
    assert written == 1
    assert await repo.latest_states([1, 2]) == {1: "Active", 2: "New"}


async def test_a_transition_keeps_the_earlier_rows(repo):
    # Append-only: the timeline is the point, so nothing overwrites.
    await repo.record([_item(1, "New")], CATEGORIES)
    await repo.record([_item(1, "Active")], CATEGORIES)
    await repo.record([_item(1, "Closed")], CATEGORIES)
    assert [c.state for c in await repo.changes_since()] == ["New", "Active", "Closed"]


async def test_category_is_captured_alongside_the_state(repo):
    await repo.record([_item(1, "Active")], CATEGORIES)
    assert (await repo.changes_since())[0].category == "InProgress"


async def test_an_unknown_state_is_still_recorded(repo):
    # Losing the transition would put a permanent hole in the item's timeline; losing
    # only the category weakens one chart.
    await repo.record([_item(1, "Cần duyệt")], CATEGORIES)
    row = (await repo.changes_since())[0]
    assert (row.state, row.category) == ("Cần duyệt", "")


async def test_blank_states_are_skipped(repo):
    assert await repo.record([_item(1, "")], CATEGORIES) == 0


async def test_changes_since_filters_by_time(repo):
    now = datetime.now(UTC)
    await repo.record([_item(1, "New")], CATEGORIES, now=now - timedelta(days=10))
    await repo.record([_item(1, "Active")], CATEGORIES, now=now - timedelta(days=1))
    recent = await repo.changes_since(now - timedelta(days=3))
    assert [c.state for c in recent] == ["Active"]


async def test_first_seen_reports_when_recording_began(repo):
    assert await repo.first_seen() is None
    stamp = datetime.now(UTC) - timedelta(days=5)
    await repo.record([_item(1, "New")], CATEGORIES, now=stamp)
    seen = await repo.first_seen()
    assert seen is not None and abs((seen.replace(tzinfo=UTC) - stamp).total_seconds()) < 2


async def test_prune_drops_only_old_rows(repo):
    now = datetime.now(UTC)
    await repo.record([_item(1, "New")], CATEGORIES, now=now - timedelta(days=200))
    await repo.record([_item(1, "Active")], CATEGORIES, now=now - timedelta(days=2))
    assert await repo.prune(now - timedelta(days=180)) == 1
    assert [c.state for c in await repo.changes_since()] == ["Active"]


async def test_latest_states_of_nothing_is_empty(repo):
    assert await repo.latest_states([]) == {}
    assert await repo.record([], CATEGORIES) == 0
