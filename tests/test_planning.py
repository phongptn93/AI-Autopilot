"""Tests for the Planning service: start_items + the scheduled-run sweeper."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.planning_analyzer import run_due_plans, start_items


class _Ado:
    def __init__(self, state: str = "Closed", tags: list[str] | None = None) -> None:
        self._state, self._tags = state, tags or []
        self.tags: list[tuple[int, str]] = []
        self.states: list[tuple[int, str]] = []

    async def get_work_item(self, i: int) -> WorkItemInfo:
        return WorkItemInfo(id=i, state=self._state, tags=list(self._tags))

    async def add_tag(self, i: int, t: str) -> None:
        self.tags.append((i, t))

    async def update_state(self, i: int, s: str) -> None:
        self.states.append((i, s))


def _c(**over):
    cfg = Settings(
        trigger_tag="autopilot", trigger_states=["Active"], planning_start_state="Active", **over
    )
    return SimpleNamespace(config=cfg, ado=_Ado())


async def test_start_items_tags_and_sets_state():
    c = _c()
    n = await start_items(c, [5])
    assert n == 1
    assert (5, "autopilot") in c.ado.tags
    assert (5, "Active") in c.ado.states


async def test_start_items_skips_state_when_already_trigger():
    c = _c()
    c.ado._state = "Active"
    await start_items(c, [5])
    assert c.ado.states == []                    # already in a trigger state
    assert (5, "autopilot") in c.ado.tags


async def test_start_items_dry_run_writes_nothing():
    c = _c(dry_run=True)
    assert await start_items(c, [5]) == 0
    assert c.ado.tags == [] and c.ado.states == []


async def test_run_due_plans_fires_past_only_and_marks_done():
    now = datetime.now()

    class _Row:
        def __init__(self, rid, ids, run_at):
            self.id, self.item_ids, self.run_at = rid, json.dumps(ids), run_at

    class _Repo:
        def __init__(self, rows):
            self.rows, self.status = rows, {}

        async def due(self, at):
            return [r for r in self.rows if r.run_at <= at]

        def ids_of(self, row):
            return json.loads(row.item_ids)

        async def set_status(self, rid, s):
            self.status[rid] = s

    past = _Row(1, [5], now - timedelta(minutes=1))
    future = _Row(2, [6], now + timedelta(hours=1))
    c = _c()
    c.planned_run_repo = _Repo([past, future])
    started = await run_due_plans(c)
    assert started == 1
    assert c.planned_run_repo.status == {1: "done"}
    assert (5, "autopilot") in c.ado.tags and (6, "autopilot") not in c.ado.tags
