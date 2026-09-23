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


def test_over_the_cap_the_page_marks_the_lines_the_brief_really_carries(tmp_path):
    """The syringe markers must come from `lessons.recent()`, not from "newest N".

    The two rules only agree while the file is under the cap. Over it they diverged in
    the worst direction: `recent()` puts a human-typed rule FIRST and keeps it forever,
    while "newest N" dropped it for being old. Measured on a 13-line file, the page told
    the operator that a standing rule was NOT reaching the agent (it was) and that a
    machine line WAS (it was not) — on the one page whose whole promise is that it shows
    what the agent gets told.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cap = 8
    # One rule a human typed, older than everything else.
    lessons.add(str(workspace), "Repo", "RULE: validate every DTO before mapping")
    for i in range(12):                                   # then bury it under the cap
        lessons.record_lessons(
            str(workspace), "Repo", [f"learned line {i}"],
            now=datetime(2026, 9, 10 + (i // 5), 12, i % 60),
        )
    settings = Settings(
        dry_run=True,
        workspace_directory=str(workspace),
        learning_loop_enabled=True,
        lessons_max_injected=cap,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cap.db'}",
    )
    with TestClient(create_app(settings)) as client:
        body = client.get("/dashboard/learning").text

    injected = lessons.recent(str(workspace), ["Repo"], limit=cap)
    assert "RULE: validate every DTO before mapping" in injected   # the rule is carried

    # Every row the page dims as "ngoài suất" must be one the brief really leaves out,
    # and nothing the brief carries may be dimmed. Rows are rendered newest-first, so
    # slicing the rendered order is enough to tell which row got which class.
    rows = body.split('class="krow')[1:]
    # Guard the guard: if the page ever stops rendering rows (a redirect to login, a
    # renamed class) the loop below iterates nothing and this test passes while proving
    # nothing. It did exactly that once.
    assert len(rows) == 13, f"expected 13 rendered rows, got {len(rows)}"
    classes = [r.split(">", 1)[0].strip().strip('"') for r in rows]
    assert any(c.endswith("off") for c in classes), "no row is over the cap — test is vacuous"
    assert any(c.endswith("live") for c in classes), "no row is injected — test is vacuous"

    for row in rows:
        dimmed = row.split(">", 1)[0].strip().strip('"').endswith("off")
        carried = any(line in row for line in injected)
        assert dimmed != carried, (
            "a row is dimmed as 'ngoài suất' while the brief carries it, or vice versa"
        )
