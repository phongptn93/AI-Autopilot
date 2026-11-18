"""'Cần người xử lý' queue: triage by reason, and the Done / Skip actions next to Resume."""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard.routes.runs import _branch_in, _hold_kind
from ai_autopilot.data import PipelineState

_PR_REASON = ("Cannot open the PR: ado MCP server failed to connect (CONNECT_TIMEOUT), `az` CLI "
              "is not installed. Branch feature/11371-goods-receipt-approve is pushed; please "
              "create a DRAFT PR to dxfac/development linked to #11371.")


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "c.yaml"))
    return create_app(Settings(dry_run=True, ado_organization="https://dev.azure.com/acme",
                               ado_project="P",
                               database_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}"))


def test_reasons_are_sorted_into_actionable_kinds():
    assert _hold_kind(_PR_REASON) == "manual_pr"
    assert _hold_kind("ADO MCP servers failed to connect (CONNECT_TIMEOUT)") == "infra"
    assert _hold_kind("Chờ BA chốt quy tắc chia đều") == "decision"
    assert _hold_kind("Merge conflict on src/app.ts") == "conflict"
    assert _hold_kind("something odd") == "other"
    assert _branch_in(_PR_REASON) == "feature/11371-goods-receipt-approve"
    assert _branch_in("no branch here") == ""


def test_page_shows_triage_and_every_action(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        repo = app.state.container.state_repo
        asyncio.run(repo.set(11371, PipelineState.NEEDS_HUMAN, title="Nút Gửi phê duyệt",
                             detail=_PR_REASON))
        page = client.get("/dashboard/queue").text
        assert "Cần tạo PR tay" in page and "feature/11371-goods-receipt-approve" in page
        for action in ("/dashboard/queue/resume", "/dashboard/queue/done", "/dashboard/queue/skip"):
            assert action in page
        assert "/dashboard/task/11371" in page


def test_done_and_skip_take_items_out_of_the_queue(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        repo = app.state.container.state_repo
        asyncio.run(repo.set(1, PipelineState.NEEDS_HUMAN, title="a", detail="x"))
        asyncio.run(repo.set(2, PipelineState.NEEDS_HUMAN, title="b", detail="Chờ BA"))
        r = client.post("/dashboard/queue/done", data={"ids": ["1"]}, follow_redirects=False)
        assert r.status_code == 303 and "autopilot_flash=queue_done" in r.headers["set-cookie"]
        r = client.post("/dashboard/queue/skip", data={"ids": ["2"]}, follow_redirects=False)
        assert "autopilot_flash=queue_skipped" in r.headers["set-cookie"]
        one, two = asyncio.run(repo.get(1)), asyncio.run(repo.get(2))
        assert one.state == PipelineState.DONE
        assert two.state == PipelineState.FAILED and "Chờ BA" in two.detail
        assert "Không có item nào đang chờ" in client.get("/dashboard/queue").text


def test_empty_post_is_a_no_op(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        for path in ("/dashboard/queue/done", "/dashboard/queue/skip"):
            r = client.post(path, data={}, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/dashboard/queue"
