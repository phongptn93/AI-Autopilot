"""DORA metrics from the deploy pipeline's build history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_autopilot import dora
from ai_autopilot.services import delivery_report

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def _b(bid: int, result: str, hours_ago: float) -> dict:
    return {"id": bid, "result": result,
            "finishTime": (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")}


def test_frequency_failure_rate_and_restore():
    builds = [
        _b(1, "succeeded", 24 * 13),
        _b(2, "failed", 24 * 10),          # incident opens
        _b(3, "failed", 24 * 10 - 2),      # same incident, not a second one
        _b(4, "succeeded", 24 * 10 - 6),   # restored after 6h
        _b(5, "canceled", 24 * 5),         # neither ok nor failed
        _b(6, "partiallySucceeded", 24 * 2),
        _b(7, "succeeded", 24 * 2 - 1),    # restored after 1h
        _b(8, "succeeded", 3),
    ]
    rep = dora.compute(dora.deploys_from_builds(builds, "7|main"), days=14, now=NOW,
                       lead_time_days=3.5)
    assert rep.status == "ok"
    assert rep.deploys_ok == 4 and rep.deploys_failed == 3
    assert rep.per_week == 2.0
    assert rep.change_failure_rate == pytest.approx(3 / 7)
    assert rep.restore_hours == [6.0, 1.0]
    assert rep.restore_p50_hours == 3.5
    assert rep.broken_since is None
    assert rep.levels == {"frequency": "high", "cfr": "low", "restore": "high", "lead": "high"}


def test_a_failure_not_yet_restored_is_reported_as_broken_not_averaged():
    builds = [_b(1, "succeeded", 48), _b(2, "failed", 5)]
    rep = dora.compute(dora.deploys_from_builds(builds, "0|main"), days=7, now=NOW)
    assert rep.restore_hours == []
    assert rep.broken_since == NOW - timedelta(hours=5)


def test_failures_restore_within_their_own_scope_only():
    a = dora.deploys_from_builds([_b(1, "failed", 10)], "1|main")
    b = dora.deploys_from_builds([_b(2, "succeeded", 5)], "2|main")
    rep = dora.compute(a + b, days=7, now=NOW)
    assert rep.restore_hours == [] and rep.broken_since is not None


def test_no_deploys_has_no_rate_and_no_band():
    rep = dora.compute([], days=30, now=NOW)
    assert rep.change_failure_rate is None
    assert rep.levels["frequency"] == "" and rep.levels["cfr"] == ""


def test_unknown_carries_no_numbers():
    rep = dora.unknown(30, "ADO lỗi")
    assert rep.status == "unknown" and all(v == "" for v in rep.levels.values())


class _Cfg:
    deploy_branch = ""
    base_branch = "development"
    deploy_pipeline_id = 12
    workspaces: list = []


class _Ado:
    def __init__(self, builds):
        self.builds = builds
        self.calls: list = []

    async def get_deploy_builds(self, pipeline, branch, since):
        self.calls.append((pipeline, branch))
        return self.builds


class _Container:
    def __init__(self, builds):
        self.config = _Cfg()
        self.ado = _Ado(builds)


@pytest.mark.asyncio
async def test_gather_reads_the_configured_pipeline_and_branch():
    c = _Container([_b(1, "succeeded", 2)])
    rep = await delivery_report.gather_dora(c, days=7, now=NOW)
    assert c.ado.calls == [(12, "development")]
    assert rep.deploys_ok == 1


@pytest.mark.asyncio
async def test_an_unreadable_pipeline_is_unknown_never_zero():
    rep = await delivery_report.gather_dora(_Container(None), days=7, now=NOW)
    assert rep.status == "unknown" and "development" in rep.reason


def test_delivery_page_shows_the_four_metrics(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from ai_autopilot import delivery
    from ai_autopilot.app import create_app
    from ai_autopilot.config import Settings

    report = delivery.DeliveryReport(window_days=14, generated_at=NOW,
                                     thresholds=delivery.Thresholds())
    report.dora = dora.compute(
        dora.deploys_from_builds([_b(1, "succeeded", 30), _b(2, "failed", 5)], "0|main"),
        days=14, now=NOW)

    async def fake_gather(container, **_):
        return report, None

    monkeypatch.setattr(delivery_report, "gather", fake_gather)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/delivery").text
    assert "DORA — 4 chỉ số giao hàng" in html
    assert "Tần suất deploy" in html and "Thời gian khôi phục" in html
    assert "đang hỏng" in html
