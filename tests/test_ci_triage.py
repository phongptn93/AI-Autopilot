"""CI/CD triage: red builds sorted into causes, with a next step."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_autopilot import ci_triage as ct
from ai_autopilot.dashboard.routes import ci as ci_route

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


@pytest.mark.parametrize(("step", "errors", "log", "expected"), [
    ("Build", ["error CS0103: The name 'x' does not exist"], "", "build"),
    ("Run tests", [], "FAILED tests/test_a.py::test_x - assert 1 == 2", "test"),
    ("Restore", ["NU1101: Unable to find package Foo"], "", "restore"),
    ("npm ci", [], "npm ERR! code ERESOLVE", "restore"),
    ("Deploy", ["Request failed with status code 401"], "", "auth"),
    ("Checkout", [], "##[error]No space left on device", "agent"),
    ("Job", ["The job running on agent X has exceeded the maximum execution time of 60 minutes"],
     "", "timeout"),
    ("gitleaks scan", ["leaks found: 2"], "", "security"),
    ("Push image", [], "denied: requested access to the resource is denied", "docker"),
    ("Docker build", ["error CS1002: ; expected"], "", "build"),   # the code, not docker
    ("macro step", [], "something odd", "unknown"),
])
def test_classify(step, errors, log, expected):
    cat, hint, evidence = ct.classify(step, errors, log)
    assert cat == expected
    assert hint
    assert (evidence != "") == (expected != "unknown")


def test_failing_record_takes_the_first_failed_task_and_its_errors():
    timeline = {"records": [
        {"type": "Stage", "result": "failed", "name": "CI"},
        {"type": "Task", "result": "succeeded", "name": "Checkout", "startTime": "1"},
        {"type": "Task", "result": "failed", "name": "Test", "startTime": "3",
         "log": {"id": 9}, "issues": [{"type": "warning", "message": "w"}]},
        {"type": "Task", "result": "failed", "name": "Build", "startTime": "2",
         "log": {"id": 7}, "issues": [{"type": "error", "message": "error CS1 x"}]},
    ]}
    assert ct.failing_record(timeline) == ("Build", ["error CS1 x"], 7)
    assert ct.failing_record(None) == ("", [], None)


def _build(bid, result, hours_ago, commit="abc", pipeline="api-ci"):
    return {"id": bid, "result": result, "buildNumber": f"2026.{bid}", "sourceVersion": commit,
            "sourceBranch": "refs/heads/main", "definition": {"name": pipeline},
            "requestedFor": {"displayName": "Bob"},
            "finishTime": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "_links": {"web": {"href": f"https://x/{bid}"}}}


def test_a_failure_the_same_commit_later_fixed_is_flaky():
    failed = [ct.from_build(_build(1, "failed", 5)), ct.from_build(_build(2, "failed", 5, "def"))]
    ct.mark_flaky(failed, [_build(3, "succeeded", 4), _build(4, "succeeded", 9, "def")])
    assert [f.flaky for f in failed] == [True, False]   # def's green build came BEFORE


class _Ado:
    def __init__(self, builds):
        self.builds = builds
        self.timelines = 0

    async def get_builds_since(self, since, top=300):
        return self.builds

    async def get_build_timeline(self, build_id):
        self.timelines += 1
        return {"records": [{"type": "Task", "result": "failed", "name": "Restore",
                             "log": {"id": 1}, "issues": []}]}

    async def get_build_log_tail(self, build_id, log_id, lines=120):
        return "npm ERR! 404 Not Found - GET https://registry/x"


@pytest.mark.asyncio
async def test_gather_triages_red_builds_and_counts_all():
    ado = _Ado([_build(1, "failed", 2), _build(2, "succeeded", 3), _build(3, "canceled", 4)])
    rep = await ci_route.gather(ado, 7, NOW)
    assert rep.status == "ok" and rep.total_builds == 3
    assert [f.category for f in rep.failed] == ["restore"]
    assert rep.failed[0].url == "https://x/1"


@pytest.mark.asyncio
async def test_gather_bounds_how_many_builds_it_opens():
    ado = _Ado([_build(i, "failed", i) for i in range(1, 60)])
    rep = await ci_route.gather(ado, 7, NOW)
    assert len(rep.failed) == ci_route.MAX_TRIAGED == ado.timelines
    assert "25" in rep.reason


@pytest.mark.asyncio
async def test_unreadable_builds_are_unknown_never_green():
    rep = await ci_route.gather(_Ado(None), 7, NOW)
    assert rep.status == "unknown" and rep.failed == []


def test_ci_page_renders(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app
    from ai_autopilot.config import Settings

    async def fake_gather(ado, days, now):
        rep = ct.CiReport(days=days, total_builds=4)
        f = ct.from_build(_build(1, "failed", 2))
        f.category, f.hint, f.evidence = ct.classify("Build", ["error CS0103: x"], "")
        f.flaky = True
        rep.failed = [f]
        return rep

    monkeypatch.setattr(ci_route, "gather", fake_gather)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'ci.db'}",
                   ado_organization="https://dev.azure.com/org", ado_pat="x")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/ci").text
    assert "Lỗi biên dịch" in html and "chập chờn" in html and "error CS0103" in html
