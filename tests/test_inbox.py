"""The decision inbox: one page for every decision waiting on a person.

What must not regress: the tier a source lands in (red means the same thing wherever
it came from), one broken source never blanking the page, the empty state only when
there really is nothing, and the sidebar badge's numbers agreeing with the page.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ai_autopilot import fleet, inbox
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard import nav
from ai_autopilot.data.entities import PipelineState
from ai_autopilot.execution.result_contract import Deviation


@pytest.fixture
def client(tmp_path):
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'i.db'}",
                   fleet_role="central", fleet_token="t")
    with TestClient(create_app(cfg)) as c:
        yield c


async def _seed(client) -> None:
    c = client.app.state.container
    # 1) an item held for a human
    await c.state_repo.set(4242, PipelineState.NEEDS_HUMAN, title="Báo cáo tồn kho",
                           detail="Thiếu AC cho trường hợp kho âm")
    # 2) one drift that needs a business decision, one that does not
    await c.spec_drift_repo.add(SimpleNamespace(id=8530, project="P", title="Tồn kho"), "", [
        Deviation(kind="logic_differs", summary="theo lô", where="AC-3",
                  spec_says="theo kho", code_does="Tồn tính theo lô", needs_decision=True),
        Deviation(kind="logic_differs", summary="đổi tên cột", where="AC-4",
                  code_does="Cột đổi tên"),
    ])
    # 3) an escalated PR conflict
    row, _ = await c.pr_conflict_repo.observe(
        "repo-1", 77, repo_name="shop", title="Thêm báo cáo", url="https://ado/pr/77",
        source_branch="feature/x", target_branch="main",
        first_seen=datetime.now(UTC) - timedelta(hours=2),
    )
    await c.pr_conflict_repo.update(row.id, status="escalated", last_error="markers left")
    # 4) a fleet worker that stopped reporting
    await c.fleet_repo.upsert(
        fleet.WorkerReport(name="dev-01", hostname="dev-01", version="2.60.0", profile="dev"),
        now=datetime.now(UTC) - timedelta(hours=3),
    )


def test_every_source_lands_in_its_tier(client):
    client.portal.call(_seed, client)
    entries = client.portal.call(inbox.collect, client.app.state.container)
    by = {(e.source, e.tier) for e in entries}
    assert ("held", inbox.RED) in by
    assert ("conflict", inbox.RED) in by
    assert ("fleet", inbox.RED) in by
    spec = [e for e in entries if e.source == "spec"]
    assert {e.tier for e in spec} == {inbox.RED, inbox.YELLOW}
    assert [e for e in spec if e.tier == inbox.RED][0].title.startswith("#8530 — AC-3")
    # Red before yellow, and every entry can be acted on in place.
    tiers = [inbox.TIER_ORDER.index(e.tier) for e in entries]
    assert tiers == sorted(tiers)
    assert all(e.actions for e in entries)
    held = next(e for e in entries if e.source == "held")
    assert held.actions[0].href == "/dashboard/queue/resume"
    assert held.actions[0].fields == {"ids": 4242}


def test_fleet_is_silent_off_a_central(tmp_path):
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    with TestClient(create_app(cfg)) as c:
        c.portal.call(_seed, c)
        entries = c.portal.call(inbox.collect, c.app.state.container)
    assert not [e for e in entries if e.source == "fleet"]


def test_a_failing_source_does_not_blank_the_page(client, monkeypatch):
    client.portal.call(_seed, client)

    async def boom(_c):
        raise RuntimeError("table gone")

    monkeypatch.setattr(inbox, "_src_spec", boom)
    box = client.portal.call(inbox.gather, client.app.state.container)
    assert box.failed == ["Lệch spec"]
    assert any(e.source == "held" for e in box.entries)
    html = client.get("/dashboard/inbox").text
    assert "Không đọc được nguồn Lệch spec" in html
    assert "#4242" in html


def test_the_page_lists_entries_with_inline_actions(client):
    client.portal.call(_seed, client)
    resp = client.get("/dashboard/inbox")
    assert resp.status_code == 200
    html = resp.text
    assert "Cần chốt hôm nay" in html and "Trong tuần" in html
    assert "#4242 — Báo cáo tồn kho" in html
    assert 'action="/dashboard/specs/decide"' in html
    assert 'action="/dashboard/queue/resume"' in html
    assert "PR !77" in html and "Máy dev-01 mất liên lạc" in html
    assert "giờ trước" in html
    assert "Không có gì chờ bạn quyết" not in html
    only = client.get("/dashboard/inbox?src=conflict").text
    assert "PR !77" in only and "#4242 — " not in only


def test_the_empty_state(client):
    html = client.get("/dashboard/inbox").text
    assert "✅" in html and "Không có gì chờ bạn quyết." in html
    assert 'href="/dashboard"' in html


def test_count_json_matches_the_page(client):
    assert client.get("/dashboard/inbox/count.json").json() == {"red": 0, "total": 0}
    client.portal.call(_seed, client)
    client.app.state.inbox_counts = None          # drop the badge cache
    data = client.get("/dashboard/inbox/count.json").json()
    entries = client.portal.call(inbox.collect, client.app.state.container)
    assert data == {"red": sum(1 for e in entries if e.tier == inbox.RED),
                    "total": len(entries)}
    assert data["red"] == 4                        # held, spec ask, conflict, offline


def test_the_inbox_opens_the_menu():
    first = nav.GROUPS[0].items[0]
    assert (first.key, first.href, first.label) == ("inbox", "/dashboard/inbox", "Hộp quyết định")
    assert nav.page_title("inbox") == "🎯 Hộp quyết định"


def test_every_page_carries_the_palette_and_the_badge(client):
    html = client.get("/dashboard").text
    assert 'id="cmdk"' in html and 'role="dialog"' in html and 'aria-modal="true"' in html
    assert 'id="cmdk-pages"' in html and 'id="inbox-badge"' in html
    assert "/dashboard/inbox/count.json" in html
    assert 'id="nav-toggle"' in html                 # the phone menu toggle
    raw = html.split('id="cmdk-pages">', 1)[1].split("</script>", 1)[0]
    pages = json.loads(raw)
    assert pages[0]["href"] == "/dashboard/inbox"
    assert {p["href"] for p in pages} >= {"/dashboard/settings", "/dashboard/fleet"}


def test_knowledge_and_security_land_in_their_tiers(tmp_path):
    from ai_autopilot import lessons
    from ai_autopilot.reports import Finding

    ws = tmp_path / "ws"
    ws.mkdir()
    for _ in range(lessons.RECURRING_AT):
        lessons.record_lessons(str(ws), "shop", ["Luôn chạy migration trước test"],
                               now=datetime.now(UTC))
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'k.db'}",
                   fleet_role="central", fleet_token="t", workspace_directory=str(ws))

    async def seed(c):
        await c.fleet_knowledge_repo.contribute(
            [{"key": "dung utc", "text": "Dùng UTC khi lưu ngày", "repo": "shop", "count": 1}],
            origin="dev-01",
        )
        await c.security_repo.upsert_scan("C:/src/shop", "P", 1, [
            Finding(severity="critical", title="SQL injection", file="a.py", line=3,
                    tool="builtin", rule_id="sqli", fingerprint="f1"),
            Finding(severity="high", title="Weak hash", file="b.py", line=9,
                    tool="builtin", rule_id="md5", fingerprint="f2"),
            Finding(severity="medium", title="Noise", file="c.py", tool="builtin",
                    rule_id="x", fingerprint="f3"),
        ])

    with TestClient(create_app(cfg)) as c:
        c.portal.call(seed, c.app.state.container)
        entries = c.portal.call(inbox.collect, c.app.state.container)
        html = c.get("/dashboard/inbox?src=knowledge").text
    sec = {e.title: e.tier for e in entries if e.source == "security"}
    assert sec == {"[CRITICAL] SQL injection": inbox.RED, "[HIGH] Weak hash": inbox.YELLOW}
    know = [e for e in entries if e.source == "knowledge"]
    assert {e.tier for e in know if "chờ duyệt" in e.title} == {inbox.GREEN}   # one machine
    recurring = [e for e in know if e.title.startswith("Bài học lặp lại")]
    assert recurring and recurring[0].tier == inbox.YELLOW
    assert recurring[0].actions[0].href == "/dashboard/learning/promote"
    assert 'action="/dashboard/learning/pool/approved"' in html
    assert 'action="/dashboard/learning/promote"' in html
