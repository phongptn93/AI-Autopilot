"""The current sprint: committed vs done, on track, added, hours, estimates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_autopilot import sprint as sp
from ai_autopilot.dashboard.routes import sprint as sprint_route

START = datetime(2026, 11, 16, tzinfo=UTC)
FINISH = datetime(2026, 11, 30, tzinfo=UTC)
CATS = {"New": "Proposed", "Active": "InProgress", "Resolved": "Resolved",
        "Closed": "Completed", "Removed": "Removed"}
ITER = {"name": "Sprint 42", "path": "Proj\\Sprint 42",
        "start": START.isoformat(), "finish": FINISH.isoformat()}


def _f(i, kind, state, *, who="An", points=None, created="2026-11-10T00:00:00Z", **extra):
    out = {"System.Id": i, "System.Title": f"item {i}", "System.WorkItemType": kind,
           "System.State": state, "System.AssignedTo": {"displayName": who},
           "System.CreatedDate": created, **extra}
    if points is not None:
        out["Microsoft.VSTS.Scheduling.StoryPoints"] = points
    return out


RAW = [
    _f(1, "User Story", "Closed", points=5),
    _f(2, "User Story", "Active", points=3, who="Binh"),
    _f(3, "Bug", "New", points=2, who="Binh", created="2026-11-18T00:00:00Z"),
    _f(4, "User Story", "Removed", points=8),
    _f(10, "Task", "Active", who="Binh", **{"Microsoft.VSTS.Scheduling.RemainingWork": 6}),
    _f(11, "Task", "Closed", **{"Microsoft.VSTS.Scheduling.RemainingWork": 0,
                                "Microsoft.VSTS.Scheduling.OriginalEstimate": 4,
                                "Microsoft.VSTS.Scheduling.CompletedWork": 6}),
]


def test_committed_done_points_hours_and_estimates():
    now = datetime(2026, 11, 23, tzinfo=UTC)           # half way
    rep = sp.compute(ITER, RAW, categories=CATS, ai_item_ids={1}, now=now)
    assert rep.status == "ok" and rep.name == "Sprint 42"
    assert (rep.committed, rep.done, rep.in_progress, rep.not_started) == (3, 1, 1, 1)
    assert (rep.points_committed, rep.points_done) == (10, 5)
    assert rep.done_pct == 50.0 and rep.elapsed_pct == 50.0
    assert rep.verdict == "on_track"
    assert [i.id for i in rep.added_after_start] == [3]
    assert rep.remaining_hours == 6
    assert rep.estimate_accuracy == 1.5
    assert rep.ai_done == 1
    assert rep.people[0].name == "Binh" and rep.people[0].remaining_hours == 6
    assert rep.at_risk == []                         # not yet 2/3 through


def test_late_in_the_sprint_unstarted_work_is_at_risk_and_behind():
    now = datetime(2026, 11, 28, tzinfo=UTC)           # ~86% through
    rep = sp.compute(ITER, RAW, categories=CATS, now=now)
    assert [i.id for i in rep.at_risk] == [3]
    assert rep.verdict == "behind"


def test_without_points_progress_counts_items():
    raw = [_f(1, "User Story", "Closed"), _f(2, "User Story", "New")]
    rep = sp.compute(ITER, raw, categories=CATS, now=datetime(2026, 11, 23, tzinfo=UTC))
    assert rep.points_committed is None and rep.done_pct == 50.0
    assert rep.remaining_hours is None and rep.estimate_accuracy is None


def test_no_sprint_and_unreadable_are_different_answers():
    assert sp.compute({}, [], categories=CATS).status == "none"
    assert sp.compute(None, None, categories=CATS).status == "unknown"


class _Ado:
    def __init__(self, iteration, raw):
        self.iteration, self.raw = iteration, raw

    async def get_current_iteration(self, project):
        return self.iteration

    async def get_iteration_items(self, project, path):
        assert path == ITER["path"]
        return self.raw

    async def get_state_categories(self):
        return CATS


class _Container:
    def __init__(self, ado):
        self.ado = ado


@pytest.mark.asyncio
async def test_gather_reads_the_current_iteration_items():
    rep = await sprint_route.gather(_Container(_Ado(ITER, RAW)), "Proj",
                                    datetime(2026, 11, 23, tzinfo=UTC))
    assert rep.committed == 3


@pytest.mark.asyncio
async def test_gather_with_no_current_sprint():
    rep = await sprint_route.gather(_Container(_Ado({}, None)), "Proj", START)
    assert rep.status == "none"


def test_sprint_page_renders(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app
    from ai_autopilot.config import Settings

    async def fake_gather(c, project, now):
        return sp.compute(ITER, RAW, categories=CATS, now=datetime(2026, 11, 28, tzinfo=UTC))

    monkeypatch.setattr(sprint_route, "gather", fake_gather)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}",
                   ado_project="Proj")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/sprint").text
    assert "Sprint 42" in html and "Chậm hơn lịch" in html and "item 3" in html
