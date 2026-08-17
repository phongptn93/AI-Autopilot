"""The Workspaces page and the view scoping it drives, through the real app.

``test_workspaces.py`` covers the pure logic. These go through HTTP because the parts
that broke in review are the wiring: a save that does not reach ``config.yaml``, a
selector cookie nothing reads, or a scoped page that quietly shows everything.
"""

from __future__ import annotations

import pytest
import yaml
from starlette.testclient import TestClient

from ai_autopilot import dashboard
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dashboard, "config_file_path", lambda: config_file)
    settings = Settings(
        dry_run=True,
        ado_organization="https://dev.azure.com/o",
        ado_project="Khatoco",
        workspace_directory=str(tmp_path / "ws"),
        base_branch="development",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
    )
    with TestClient(create_app(settings)) as c:
        c.config_file = config_file          # handed to the tests that assert on YAML
        yield c


def _save(client: TestClient, rows: list[dict]):
    payload: dict = {"ws_count": str(len(rows))}
    for index, row in enumerate(rows):
        for key, value in row.items():
            payload[f"ws{index}_{key}"] = value
    return client.post("/dashboard/workspaces", data=payload, follow_redirects=False)


def test_page_renders_with_the_default_workspace(client: TestClient):
    resp = client.get("/dashboard/workspaces")
    assert resp.status_code == 200
    assert "Workspaces" in resp.text
    assert "Mặc định" in resp.text          # the default row is labelled as such


def test_saving_persists_to_yaml_and_applies_live(client: TestClient):
    resp = _save(client, [
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws",
         "base_branch": "development"},
        {"is_default": "0", "name": "CMMS", "projects": "CMMS", "directory": "D:/cmms",
         "base_branch": "main", "enabled": "on"},
    ])
    assert resp.status_code == 303

    saved = yaml.safe_load(client.config_file.read_text(encoding="utf-8"))
    assert saved["workspaces"][0]["ado_projects"] == ["CMMS"]
    assert saved["workspaces"][0]["base_branch"] == "main"

    # …and the running process sees it without a restart.
    cfg = client.app.state.container.config
    assert cfg.effective_ado_projects == ["Khatoco", "CMMS"]
    assert cfg.scoped_for_project("CMMS").base_branch == "main"


def test_an_invalid_save_is_refused_and_keeps_what_was_typed(client: TestClient):
    resp = _save(client, [
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0", "name": "Trùng", "projects": "Khatoco", "directory": "D:/x"},
    ])
    assert resp.status_code == 303
    saved = yaml.safe_load(client.config_file.read_text(encoding="utf-8"))
    assert "workspaces" not in saved          # nothing was written

    page = client.get("/dashboard/workspaces")
    assert "Chưa lưu" in page.text
    assert "Trùng" in page.text               # their work came back


def test_the_selector_appears_only_once_there_is_a_choice(client: TestClient):
    assert 'id="ws-picker"' not in client.get("/dashboard").text
    _save(client, [
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0", "name": "CMMS", "projects": "CMMS", "directory": "D:/cmms",
         "enabled": "on"},
    ])
    assert 'id="ws-picker"' in client.get("/dashboard").text


def test_selecting_a_workspace_sticks_across_pages(client: TestClient):
    _save(client, [
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0", "name": "CMMS", "projects": "CMMS", "directory": "D:/cmms",
         "enabled": "on"},
    ])
    resp = client.post("/dashboard/workspace/select",
                       data={"workspace": "cmms", "back": "/dashboard/board"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/board"
    assert client.cookies.get("autopilot_workspace") == "cmms"
    # The choice is reflected on an unrelated page, not just the one we came from.
    assert 'value="cmms" selected' in client.get("/dashboard/delivery").text


def test_the_selector_never_redirects_off_site(client: TestClient):
    resp = client.post("/dashboard/workspace/select",
                       data={"workspace": "all", "back": "https://evil.example/"},
                       follow_redirects=False)
    assert resp.headers["location"] == "/dashboard"


def test_a_stale_selection_falls_back_to_all_rather_than_stranding_the_view(client: TestClient):
    client.cookies.set("autopilot_workspace", "deleted-workspace", path="/dashboard")
    page = client.get("/dashboard/board")
    assert page.status_code == 200


@pytest.mark.parametrize("path", [
    "/dashboard", "/dashboard/board", "/dashboard/delivery", "/dashboard/history",
    "/dashboard/queue", "/dashboard/analytics", "/dashboard/planning",
])
def test_scoped_pages_still_render(client: TestClient, path: str):
    """Every page the selector narrows must survive being narrowed — including to a
    workspace whose projects have no data at all."""
    _save(client, [
        {"is_default": "1", "name": "Chính", "projects": "Khatoco", "directory": "C:/ws"},
        {"is_default": "0", "name": "CMMS", "projects": "CMMS", "directory": "D:/cmms",
         "enabled": "on"},
    ])
    client.cookies.set("autopilot_workspace", "cmms", path="/dashboard")
    resp = client.get(path)
    assert resp.status_code == 200
    assert "AI Autopilot" in resp.text
