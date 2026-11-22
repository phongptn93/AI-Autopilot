"""Change requests: register, volatility, and specs changed after customer confirmation."""

from __future__ import annotations

from datetime import UTC, datetime

from ai_autopilot import change_requests as cr

NOW = datetime(2026, 11, 22, tzinfo=UTC)
CATS = {"New": "Proposed", "Active": "InProgress", "Closed": "Completed", "Removed": "Removed"}


def _f(i, state, *, kind="Task", tags="", parent=0, title="", created="2026-11-01T00:00:00Z",
       closed=None):
    f = {"System.Id": i, "System.WorkItemType": kind, "System.State": state,
         "System.Tags": tags, "System.Title": title or f"cr {i}", "System.CreatedDate": created}
    if parent:
        f["System.Parent"] = parent
    if closed:
        f["Microsoft.VSTS.Common.ClosedDate"] = closed
    return f


RAW = [
    _f(1, "Active", tags="Change Request; UI", parent=100),
    _f(2, "Closed", kind="Change Request", parent=100, created="2026-11-01T00:00:00Z",
       closed="2026-11-05T00:00:00Z"),
    _f(3, "New", tags="change request", title="Đổi báo cáo cho #200"),   # ref by title
    _f(4, "Active", tags="Bug", parent=100),                             # not a CR
]
STALE = [{"item_id": 100, "title": "Phiếu lĩnh", "rel": "specs/a.md"},
         {"item_id": 300, "title": "Báo cáo tồn", "rel": "specs/b.md"}]


def test_register_counts_and_decide_time():
    rep = cr.build(RAW, categories=CATS, cr_tags=["Change Request"], stale_specs=STALE,
                   days=90, now=NOW)
    assert sorted(c.id for c in rep.crs) == [1, 2, 3]
    assert {c.id for c in rep.open} == {1, 3}
    assert rep.opened == 3 and rep.decide_p50_days == 4.0
    assert rep.crs[-1].id == 2                       # decided ones sort last


def test_volatility_and_title_references():
    rep = cr.build(RAW, categories=CATS, cr_tags=["Change Request"], stale_specs=[],
                   days=90, now=NOW)
    assert rep.volatile == [(100, 2)]
    assert next(c for c in rep.crs if c.id == 3).refs == {200}


def test_a_confirmed_spec_changed_without_a_cr_is_flagged():
    rep = cr.build(RAW, categories=CATS, cr_tags=["Change Request"], stale_specs=STALE,
                   days=90, now=NOW)
    assert [s.item_id for s in rep.silent] == [300]      # 100 has CRs, 300 does not


def test_no_tags_configured_still_finds_the_type():
    rep = cr.build(RAW, categories=CATS, cr_tags=[], stale_specs=[], days=90, now=NOW)
    assert [c.id for c in rep.crs] == [2]


def test_ba_changes_tab_renders(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app
    from ai_autopilot.config import Settings
    from ai_autopilot.dashboard.routes import ba

    async def fake_changes(c, rows):
        return {"report": cr.build(RAW, categories=CATS, cr_tags=["Change Request"],
                                   stale_specs=STALE, days=90, now=NOW),
                "error": "", "now": NOW, "item_url": lambda i: f"https://x/{i}"}

    monkeypatch.setattr(ba, "_changes", fake_changes)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/ba?tab=changes").text
    assert "Sổ change request" in html and "Báo cáo tồn" in html
    assert "Đổi sau khi khách xác nhận" in html
