"""UX wave: feedback after saves, error pages, quick find, ADO links, readable schedules."""

from __future__ import annotations

import asyncio
import re

import pytest
from starlette.testclient import TestClient

from ai_autopilot import cron_text
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard.routes.search import norm
from ai_autopilot.data import PipelineState
from ai_autopilot.inbox import InboxEntry


@pytest.mark.parametrize("expr,text", [
    ("7 18 * * 1-5", "Thứ 2–6 lúc 18:07"),
    ("13 7 * * 1", "Thứ 2 lúc 07:13"),
    ("37 6 * * *", "Mỗi ngày lúc 06:37"),
    ("*/15 * * * *", "Mỗi ngày, mỗi 15 phút"),
    ("5 */2 * * *", "Mỗi ngày, mỗi 2 giờ (phút 05)"),
    ("0 9,14 * * 1-5", "Thứ 2–6 lúc 09:00, 14:00"),
    ("30 8 1 * *", "Ngày 1 hằng tháng lúc 08:30"),
    ("0 0 * 1 *", None),            # month-specific: shown raw
    ("garbage", None),
])
def test_cron_in_words(expr, text):
    assert cron_text.describe(expr) == text


def test_search_is_accent_insensitive():
    assert norm("Báo cáo tồn kho Đà Nẵng") == "bao cao ton kho da nang"


def test_inbox_rows_know_their_work_item():
    assert InboxEntry("s", "red", "!", "#8624 — BomUsage…").work_item_id == 8624
    assert InboxEntry("s", "red", "!", "Máy trạm vm-1 offline").work_item_id == 0


def _app(tmp_path, monkeypatch, **cfg):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "c.yaml"))
    base = dict(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}",
                ado_organization="https://dev.azure.com/acme", ado_project="P")
    base.update(cfg)
    return create_app(Settings(**base))


def test_quick_find_returns_work_items_and_reviews(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        c = app.state.container
        asyncio.run(c.state_repo.set(10079, PipelineState.DONE,
                                     title="Phụ tùng / Vật tư: xoá phụ tùng đang gắn"))
        asyncio.run(c.review_repo.create(kind="spec", target="x.md", title="BRD Quản lý kho"))
        hits = client.get("/dashboard/api/search?q=phu tung").json()["results"]
        assert hits and hits[0]["href"] == "/dashboard/task/10079"
        assert hits[0]["group"] == "Work item"
        assert any(h["href"].startswith("/dashboard/review/")
                   for h in client.get("/dashboard/api/search?q=quan ly kho").json()["results"])
        assert client.get("/dashboard/api/search?q=x").json() == {"results": []}   # too short


def test_errors_are_pages_not_json(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.get("/dashboard/no-such-page")
        assert r.status_code == 404 and "Không tìm thấy trang này" in r.text and "<html" in r.text
        api = client.get("/dashboard/no-such.json")
        assert api.status_code == 404 and api.headers["content-type"].startswith("application/json")


def test_a_save_without_its_own_message_gets_a_toast(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.post("/dashboard/workspace/select",
                        data={"workspace": "all", "back": "/dashboard"}, follow_redirects=False)
        assert r.status_code in (302, 303) and "ap_toast=saved" in r.headers.get("set-cookie", "")
        # a page with its own wording keeps it — no generic toast on top
        r = client.post("/dashboard/review/start", data={"kind": "spec", "target": "../x"},
                        follow_redirects=False)
        assert "autopilot_flash=" in r.headers.get("set-cookie", "")
        assert "ap_toast=" not in r.headers.get("set-cookie", "")
        page = client.get("/dashboard").text
        assert 'id="ap-toast"' in page and "data-busy" in page


def test_task_room_puts_the_actions_first(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        asyncio.run(app.state.container.state_repo.set(
            6749, PipelineState.NEEDS_HUMAN, title="TLCL.33",
            detail="Chờ BA chốt quy tắc chia đều"))
        page = client.get("/dashboard/task/6749").text
        assert "https://dev.azure.com/acme/_workitems/edit/6749" in page and "Mở trên ADO" in page
        assert "Cần người xử lý" in page and "Chờ BA chốt quy tắc chia đều" in page
        assert "/dashboard/queue/resume" in page and "/dashboard/trigger-check?id=6749" in page


def test_sibling_pages_are_one_click_away_and_health_is_on_the_overview(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        flow = client.get("/dashboard/flow").text
        assert 'class="subnav"' in flow and 'href="/dashboard/roles"' in flow
        assert 'aria-current="page"' in flow
        home = client.get("/dashboard").text
        assert "Sức khoẻ hệ thống" in home and "/dashboard/setup?step=done" in home


def test_keyboard_help_and_security_first_visit(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        page = client.get("/dashboard").text
        assert 'id="kbd-help"' in page and "/dashboard/inbox" in page
        sec = client.get("/dashboard/security").text
        assert "Chưa quét repo nào" in sec


def test_pr_cards_say_not_connected_instead_of_an_outage(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)            # no PAT → no tracker
    with TestClient(app) as client:
        home = client.get("/dashboard").text
        assert "chưa kết nối tracker" in home and "chưa đọc được Azure DevOps" not in home


def test_one_button_system_and_no_bespoke_button_classes_left():
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[1] / "ai_autopilot" / "dashboard" / "templates"
    base = (tpl / "base.html").read_text(encoding="utf-8")
    for cls in (".btn-primary", ".btn-secondary", ".btn-soft", ".btn-success", ".btn-danger",
                ".btn-quiet", ".btn-sm", ".btn-lg", ".actions-sm"):
        assert cls in base, cls
    retired = ("fl-btn", "ib-btn", "s-btn", "sp-btn", "own-btn", "danger-btn", "promote-btn",
               "btn-go", "tr-act", "wz-next", "fl-sync", "rs-go", "fl-forget")
    for f in tpl.glob("*.html"):
        text = f.read_text(encoding="utf-8")
        tokens = {t for attr in re.findall(r'class="([^"]*)"', text) for t in attr.split()}
        tokens |= set(re.findall(r"\.([\w-]+)\s*[{:]", text))
        assert not tokens & set(retired), (f.name, tokens & set(retired))


def test_loading_states_progress_bar_and_skeleton(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        page = client.get("/dashboard").text
        assert 'id="ap-progress"' in page and "window.apProgress" in page
        assert ".skel {" in page and ".ap-stamp.stale" in page


def test_login_styles_every_field_type():
    from pathlib import Path
    tpl = Path(__file__).resolve().parents[1] / "ai_autopilot" / "dashboard" / "templates"
    login = (tpl / "login.html").read_text(encoding="utf-8")
    assert "input[type=email]" in login and "-webkit-autofill" in login
