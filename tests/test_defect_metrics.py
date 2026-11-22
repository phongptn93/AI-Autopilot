"""QC defect metrics: escape rate, reopen rate, age, time to close, by module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from ai_autopilot import defect_metrics as dm
from ai_autopilot.process_health import HealthItem
from ai_autopilot.services.process_health_service import to_health_item

NOW = datetime(2026, 11, 22, tzinfo=UTC)
CATS = {"New": "Proposed", "Active": "InProgress", "Resolved": "Resolved",
        "Closed": "Completed", "Removed": "Removed"}


def _bug(i, state, created_days, *, env="", area="Proj\\Orders", closed_days=None,
         kind="Bug", tags=(), severity=""):
    return HealthItem(
        id=i, work_item_type=kind, state=state, title=f"bug {i}", area_path=area,
        tags=tuple(tags), created=NOW - timedelta(days=created_days),
        changed=NOW - timedelta(days=1), found_in_environment=env,
        closed=None if closed_days is None else NOW - timedelta(days=closed_days),
        severity=severity)


@dataclass
class _Change:
    work_item_id: int
    state: str
    entered_at: datetime
    project: str = "Proj"
    category: str = ""


ITEMS = [
    _bug(1, "Active", 5, env="Production", severity="1 - Critical"),
    _bug(2, "Closed", 10, closed_days=6),
    _bug(3, "Closed", 20, closed_days=2, env="UAT"),
    _bug(4, "New", 60, area="Proj\\Billing"),            # older than the window, still open
    _bug(5, "Removed", 3),                                # removed: not a defect any more
    _bug(6, "Active", 2, kind="Requirement", tags=("BUG",), area="Proj\\Billing"),
    HealthItem(id=7, work_item_type="Task", state="Active", created=NOW),
]


def test_counts_rates_and_ages():
    rep = dm.compute(ITEMS, categories=CATS, days=30, now=NOW)
    assert rep.raised == 4                     # 1, 2, 3, 6 (4 is older, 5 removed, 7 a task)
    assert rep.escaped == 3                    # prod, UAT, requirement tagged BUG
    assert rep.escape_rate == pytest.approx(0.75)
    assert rep.closed == 2
    assert rep.close_p50_days == pytest.approx(11.0)   # (10-6)=4 and (20-2)=18
    assert rep.open_count == 3
    assert rep.oldest_open[0].id == 4 and rep.oldest_open[0].age_days == 60
    assert rep.open_age_p50_days == 5
    assert rep.by_severity == {"1 - Critical": 1}


def test_reopen_needs_history_and_is_never_a_silent_zero():
    rep = dm.compute(ITEMS, categories=CATS, days=30, now=NOW)
    assert rep.reopen_measured is False and rep.reopen_rate is None

    changes = [
        _Change(2, "Resolved", NOW - timedelta(days=8)),
        _Change(2, "Active", NOW - timedelta(days=7)),      # reopened
        _Change(2, "Closed", NOW - timedelta(days=6)),
        _Change(3, "Closed", NOW - timedelta(days=2)),
    ]
    rep = dm.compute(ITEMS, categories=CATS, days=30, now=NOW, changes=changes,
                     history_since=NOW - timedelta(days=90))
    assert rep.reopen_measured and rep.reopened == 1
    assert rep.reopen_rate == pytest.approx(0.5)


def test_history_that_starts_inside_the_window_does_not_measure():
    rep = dm.compute(ITEMS, categories=CATS, days=30, now=NOW, changes=[],
                     history_since=NOW - timedelta(days=3))
    assert rep.reopen_rate is None


def test_modules_with_escapes_come_first():
    rep = dm.compute(ITEMS, categories=CATS, days=30, now=NOW)
    assert [m.module for m in rep.modules] == ["Orders", "Billing"]
    orders = rep.modules[0]
    assert (orders.raised, orders.escaped, orders.open) == (3, 2, 1)


def test_health_item_reads_closed_date_and_severity():
    item = to_health_item({
        "System.Id": 9, "System.WorkItemType": "Bug", "System.State": "Closed",
        "Microsoft.VSTS.Common.ClosedDate": "2026-11-20T10:00:00Z",
        "Microsoft.VSTS.Common.Severity": "2 - High"})
    assert item.closed == datetime(2026, 11, 20, 10, tzinfo=UTC)
    assert item.severity == "2 - High"


def test_defects_page_renders(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app
    from ai_autopilot.config import Settings
    from ai_autopilot.dashboard.routes import defects

    async def fake_gather(c, project, days, now):
        return dm.compute(ITEMS, categories=CATS, days=days, now=NOW), None

    monkeypatch.setattr(defects, "gather", fake_gather)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'q.db'}",
                   ado_project="Proj")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/defects").text
    assert "Tỉ lệ lọt" in html and "75.0" in html
    assert "bug 4" in html and "Billing" in html
    assert "chưa đủ lịch sử" in html
