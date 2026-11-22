"""Environments (what runs where), production incidents, and hotfix-first priority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_autopilot import environments as env
from ai_autopilot.config import Settings
from ai_autopilot.models import TaskCategory, WorkItemInfo
from ai_autopilot.process_health import HealthItem
from ai_autopilot.routing import priority, scheduler

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)
CATS = {"Active": "InProgress", "Closed": "Completed", "Removed": "Removed", "New": "Proposed"}


def test_parse_environments_and_fallback():
    cfg = Settings(dry_run=True, deploy_environments=[
        "UAT = 45 | release", "Production = 46 | main", "broken line", "X = notanint | b"])
    assert [(e.name, e.pipeline, e.branch) for e in env.parse_environments(cfg)] == [
        ("UAT", 45, "release"), ("Production", 46, "main")]
    cfg = Settings(dry_run=True, deploy_pipeline_id=7, deploy_branch="main")
    assert [(e.name, e.pipeline, e.branch) for e in env.parse_environments(cfg)] == [
        ("Deploy", 7, "main")]


def _build(bid, hours_ago):
    return {"id": bid, "buildNumber": f"2026.{bid}", "sourceVersion": f"c{bid}" * 4,
            "finishTime": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "requestedFor": {"displayName": "Ops"}}


def test_env_view_takes_the_newest_build():
    spec = env.EnvSpec("Production", 46, "main")
    view = env.env_view(spec, [_build(1, 50), _build(3, 2), _build(2, 20)])
    assert view.current.build_id == 3 and [d.build_id for d in view.history] == [3, 2, 1]
    assert env.env_view(spec, []).status == "empty"
    assert env.env_view(spec, None).status == "unknown"


def _hi(i, state, created_days, *, env_="", tags=(), closed_days=None, kind="Bug"):
    return HealthItem(id=i, work_item_type=kind, state=state, title=f"t{i}",
                      tags=tuple(tags), created=NOW - timedelta(days=created_days),
                      found_in_environment=env_,
                      closed=None if closed_days is None else NOW - timedelta(days=closed_days))


def test_incidents_are_prod_defects_or_hotfix_tagged():
    items = [
        _hi(1, "Active", 3, env_="Production"),
        _hi(2, "Closed", 10, env_="Prod", closed_days=8),            # restored in 2 days
        _hi(3, "Active", 1, tags=("hotfix",), kind="Task"),          # tag, any type
        _hi(4, "Active", 2, env_="UAT"),                             # not production
        _hi(5, "Removed", 2, env_="Production"),
    ]
    rep = env.incidents(items, categories=CATS, hotfix_tags=["Hotfix"], days=30, now=NOW)
    assert [i.id for i in rep.open] == [1, 3]
    assert rep.opened == 3 and rep.restore_days == [2.0] and rep.restore_p50_days == 2.0


def _wi(i, tags=(), category=TaskCategory.BACKEND_TASK, prio=3):
    return WorkItemInfo(id=i, title=f"w{i}", work_item_type="Task", state="New",
                        tags=list(tags), category=category, priority=prio)


def test_hotfix_jumps_the_queue_even_over_a_p1_bug():
    items = [_wi(1, category=TaskCategory.BUG, prio=1), _wi(2, tags=("Hotfix",), prio=4)]
    assert [i.id for i in priority.sort_by_priority(items)] == [2, 1]
    assert [i.id for i in priority.sort_by_priority(items, hotfix_tags=[])] == [1, 2]
    plan = scheduler.plan_schedule(items, hotfix_tags=["hotfix"])
    assert [i.id for i in plan.ready][0] == 2


class _Ado:
    async def get_deploy_builds(self, pipeline, branch, since):
        return [dict(_build(9, 3), result="succeeded"), dict(_build(10, 1), result="failed")]

    async def get_build_work_item_ids(self, build_id, top=100):
        assert build_id == 9          # the failed newer build is not what is running
        return [77]

    async def get_work_items_by_ids(self, ids):
        return [WorkItemInfo(id=77, title="Fix invoice", work_item_type="Bug", state="Closed")]


@pytest.mark.asyncio
async def test_gather_envs_shows_the_running_build_and_its_items():
    from types import SimpleNamespace

    from ai_autopilot.dashboard.routes import environments as route
    views = await route.gather_envs(SimpleNamespace(ado=_Ado()),
                                    [env.EnvSpec("Production", 46, "main")], NOW)
    assert views[0].current.build_id == 9 and views[0].items == [(77, "Fix invoice")]


def test_environments_page_renders(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app
    from ai_autopilot.dashboard.routes import environments as route

    async def fake_envs(c, specs, now):
        v = env.env_view(specs[0], [_build(9, 3)])
        v.items = [(77, "Fix invoice")]
        return [v]

    async def fake_inc(c, project, days, now):
        return env.incidents([_hi(1, "Active", 3, env_="Production")], categories=CATS,
                             hotfix_tags=["Hotfix"], days=days, now=NOW), None

    monkeypatch.setattr(route, "gather_envs", fake_envs)
    monkeypatch.setattr(route, "gather_incidents", fake_inc)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'e.db'}",
                   ado_organization="https://dev.azure.com/o", ado_pat="x", ado_project="P",
                   deploy_environments=["Production = 46 | main"])
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/environments").text
    assert "Production" in html and "2026.9" in html and "Fix invoice" in html
    assert "Sự cố production" in html and "t1" in html
