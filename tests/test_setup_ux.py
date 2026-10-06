"""Setup wizard: presets on the policy step, save-then-check, workspace sanity, next steps."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "c.yaml"))
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}",
                   max_concurrent=3)
    with TestClient(create_app(cfg)) as c:
        yield c


def test_the_policy_step_offers_presets_without_preselecting_one(client):
    html = client.get("/dashboard/setup?step=policy").text
    assert "An toàn" in html and "Tự động hoàn toàn" in html
    # max_concurrent=3 matches no preset → "Tự đặt" is the checked one, nothing is
    # overwritten by a casual save.
    assert 'name="preset" value="" checked' in html


def test_choosing_a_preset_answers_the_policy_step(client):
    client.post("/dashboard/setup", data={
        "step": "policy", "preset": "autonomous", "execution_mode": "interactive",
    }, follow_redirects=False)
    cfg = client.app.state.container.config
    assert cfg.execution_mode == "headless" and cfg.sdlc_loop_enabled is True


def test_custom_keeps_the_typed_values(client):
    client.post("/dashboard/setup", data={
        "step": "policy", "preset": "", "execution_mode": "headless",
        "autonomy_level": "assisted", "max_concurrent": "2",
    }, follow_redirects=False)
    cfg = client.app.state.container.config
    assert cfg.execution_mode == "headless" and cfg.sdlc_loop_enabled is False


def test_save_and_check_saves_without_moving_on(client):
    resp = client.post("/dashboard/setup", data={
        "step": "ado", "ado_organization": "https://dev.azure.com/acme", "stay": "1",
    }, follow_redirects=False)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert client.app.state.container.config.ado_organization == "https://dev.azure.com/acme"


def test_a_missing_workspace_folder_is_said_at_once(client, tmp_path):
    resp = client.post("/dashboard/setup", data={
        "step": "workspace", "workspace_directory": str(tmp_path / "nope"),
        "base_branch": "main",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "setup_ws_missing" in resp.headers.get("set-cookie", "")


def test_a_real_workspace_with_claude_passes_quietly(client, tmp_path):
    (tmp_path / "ws" / ".claude").mkdir(parents=True)
    resp = client.post("/dashboard/setup", data={
        "step": "workspace", "workspace_directory": str(tmp_path / "ws"), "base_branch": "main",
    }, follow_redirects=False)
    assert "setup_ws" not in resp.headers.get("set-cookie", "")


def test_the_last_step_says_how_to_try_one_item(client):
    html = client.get("/dashboard/setup?step=done").text
    assert "Chạy thử với 1 work item" in html
    assert client.app.state.container.config.trigger_tag in html


def test_settings_has_exactly_one_search_box(client):
    # One search only: the page already had "Find a setting"; a second box was noise.
    html = client.get("/dashboard/settings").text
    assert 'id="set-find"' in html and 'id="set-q"' not in html
