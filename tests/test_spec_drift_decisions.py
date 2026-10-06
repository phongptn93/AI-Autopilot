"""Spec drift, professional edition: the four-part notice, one decision per point, ageing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ai_autopilot import spec_drift
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.execution.result_contract import Deviation, _parse_deviations


def test_the_contract_carries_what_the_spec_says_and_what_the_code_does():
    devs = _parse_deviations([{
        "kind": "logic_differs", "summary": "x", "where": "AC-3",
        "spec_says": "Tồn tính theo kho", "code_does": "Tồn tính theo lô",
        "needs_decision": "true",
    }])
    assert devs[0].spec_says == "Tồn tính theo kho" and devs[0].code_does == "Tồn tính theo lô"
    assert devs[0].needs_decision is True


def test_the_notice_has_the_four_parts_the_team_rule_asks_for():
    html = spec_drift.render_comment([
        Deviation(kind="logic_differs", summary="theo lô", where="AC-3",
                  spec_says="Tồn tính theo <kho>", code_does="Tồn tính theo lô"),
        Deviation(kind="out_of_scope", summary="Sửa luôn báo cáo nhập", where="ImportReport"),
        Deviation(kind="assumption", summary="Làm tròn 2 số lẻ", where="AC-5"),
    ]).html
    assert spec_drift.DRIFT_PREFIX in html
    for part in ("1. Quyết định", "2. Mục spec bị ảnh hưởng", "3. Phát sinh ngoài phạm vi",
                 "4. Cần chốt lại"):
        assert part in html
    assert "Hiện tại (spec)" in html and "Điều chỉnh (code)" in html
    assert "Tồn tính theo &lt;kho&gt;" in html                      # quoted, escaped
    # An assumption is a spec point (table) AND a question for the customer (part 4).
    assert html.rindex("Làm tròn") > html.index("4. Cần chốt lại")


def test_parts_with_nothing_in_them_are_left_out():
    html = spec_drift.render_comment([Deviation(kind="logic_differs", summary="x")]).html
    assert "3. Phát sinh" not in html and "4. Cần chốt" not in html


def test_the_closing_note_lists_each_decision_and_flags_code_to_fix():
    rows = [SimpleNamespace(where="AC-3", summary="theo lô", decision="update_spec",
                            decision_note=""),
            SimpleNamespace(where="AC-5", summary="làm tròn", decision="fix_code",
                            decision_note="khách muốn 0 số lẻ")]
    html = spec_drift.render_decisions_comment(rows, by="dashboard")
    assert spec_drift.RESOLVED_PREFIX in html and "1 điểm cần sửa code" in html
    assert "khách muốn 0 số lẻ" in html


@pytest.fixture
def client(tmp_path):
    cfg = Settings(dry_run=False, database_url=f"sqlite+aiosqlite:///{tmp_path / 'd.db'}",
                   ado_organization="https://dev.azure.com/o", spec_drift_tag="spec-update-needed")
    with TestClient(create_app(cfg)) as c:
        calls: list = []

        async def rec(name, *a):
            calls.append((name, *a))
            return True

        c.app.state.container.ado = SimpleNamespace(
            add_comment=lambda i, t: rec("comment", i, t),
            remove_tag=lambda i, t: rec("remove_tag", i, t),
            refresh=lambda: None,
        )
        c.calls = calls
        yield c


async def _seed(client, item_id=8530, days_ago=0):
    repo = client.app.state.container.spec_drift_repo
    item = SimpleNamespace(id=item_id, project="P", title="Tồn kho")
    await repo.add(item, "", [
        Deviation(kind="logic_differs", summary="theo lô", where="AC-3",
                  spec_says="theo kho", code_does="theo lô"),
        Deviation(kind="assumption", summary="làm tròn", where="AC-5"),
    ])
    if days_ago:
        from sqlalchemy import update

        from ai_autopilot.data.entities import SpecDrift
        async with client.app.state.container.database.session() as s:
            await s.execute(update(SpecDrift).values(
                created_at=datetime.now(UTC) - timedelta(days=days_ago)))
            await s.commit()
    return await repo.for_item(item_id)


def test_the_empty_page_does_not_celebrate_before_anything_was_ever_recorded(client):
    html = client.get("/dashboard/specs").text
    assert "Chưa có run nào báo lệch spec" in html and "🎉" not in html


def test_the_page_shows_the_table_ages_and_asks(client):
    client.portal.call(_seed, client, 8530, 10)
    html = client.get("/dashboard/specs").text
    assert "Hiện tại (spec)" in html and "theo kho" in html
    assert "tồn 10 ngày" in html and "❗ 1 cần chốt" in html
    assert 'class="s-card stale' in html                       # past the 7-day SLA
    only_asks = client.get("/dashboard/specs?kind=ask").text
    assert "AC-5" in only_asks and "AC-3" not in only_asks


def test_deciding_every_point_closes_the_item_once(client):
    rows = client.portal.call(_seed, client)
    client.post("/dashboard/specs/decide", data={"row_id": rows[0].id, "decision": "update_spec"},
                follow_redirects=False)
    assert not [c for c in client.calls if c[0] == "comment"]          # one left — no comment
    client.post("/dashboard/specs/decide",
                data={"row_id": rows[1].id, "decision": "fix_code", "note": "0 số lẻ"},
                follow_redirects=False)
    comments = [c[2] for c in client.calls if c[0] == "comment"]
    assert len(comments) == 1 and "1 điểm cần sửa code" in comments[0]
    assert ("remove_tag", 8530, "spec-update-needed") in client.calls
    html = client.get("/dashboard/specs").text
    assert "Không còn điểm lệch nào chờ quyết" in html


def test_an_unknown_decision_changes_nothing(client):
    rows = client.portal.call(_seed, client)
    client.post("/dashboard/specs/decide", data={"row_id": rows[0].id, "decision": "yolo"},
                follow_redirects=False)
    assert all(r.resolved_at is None
               for r in client.portal.call(client.app.state.container.spec_drift_repo.for_item,
                                           8530))


def test_the_export_is_a_markdown_table(client):
    client.portal.call(_seed, client)
    md = client.get("/dashboard/specs/export.md").text
    assert "| Mục | Hiện tại (spec) | Điều chỉnh (code) | Cần chốt |" in md
    assert "| AC-3 | theo kho | theo lô |  |" in md and "| AC-5 |" in md
