"""The learning loop's dashboard surface: see what was learned, prune what is wrong."""

from __future__ import annotations

from datetime import datetime

import pytest
from starlette.testclient import TestClient

from ai_autopilot import lessons
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings

_NOW = datetime(2026, 7, 29)


@pytest.fixture
def learning(tmp_path):
    """A dashboard whose workspace already knows one lesson per repo."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = Settings(
        dry_run=True,
        workspace_directory=str(workspace),
        learning_loop_enabled=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
    )
    lessons.record_lessons(str(workspace), "Backend", ["[High] null check missing"], now=_NOW)
    lessons.record_lessons(str(workspace), "Frontend", ["[Medium] unsubscribe leak"], now=_NOW)
    with TestClient(create_app(settings)) as client:
        yield client, str(workspace)


def test_page_shows_every_stored_lesson(learning):
    client, _ = learning
    body = client.get("/dashboard/learning").text
    assert "null check missing" in body and "unsubscribe leak" in body
    assert "Backend" in body and "Frontend" in body
    assert "2026-07-29" in body                       # when it was learned


def test_delete_prunes_one_lesson(learning):
    client, workspace = learning
    resp = client.post(
        "/dashboard/learning/delete",
        data={"repo": "Backend", "text": "[High] null check missing"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Gone from the store → it can no longer reach any future brief.
    assert lessons.read_lessons(workspace, "Backend") == []
    assert lessons.read_lessons(workspace, "Frontend") == ["[Medium] unsubscribe leak"]


def test_clear_forgets_a_whole_repo(learning):
    client, workspace = learning
    resp = client.post(
        "/dashboard/learning/clear", data={"repo": "Frontend"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert lessons.list_repos(workspace) == ["Backend"]


def test_page_warns_when_the_loop_is_off(tmp_path):
    settings = Settings(
        dry_run=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'off.db'}",
    )
    with TestClient(create_app(settings)) as client:
        body = client.get("/dashboard/learning").text
    assert "Learning loop đang TẮT" in body           # never a silently empty page
