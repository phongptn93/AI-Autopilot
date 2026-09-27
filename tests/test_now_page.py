"""The In-flight page: running work, interactive PR sessions, up next, just finished."""

from __future__ import annotations

import re
import tempfile
from datetime import UTC, datetime, timedelta

from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.data import PipelineState
from ai_autopilot.models import ExecutionResult, WorkItemInfo


def _client():
    d = tempfile.mkdtemp()
    return TestClient(create_app(Settings(database_url=f"sqlite+aiosqlite:///{d}/x.db")))


def test_idle_page_still_shows_queue_recent_and_sessions():
    with _client() as client:
        c = client.app.state.container

        async def seed():
            await c.state_repo.set(9447, PipelineState.QUEUED, title="Sequential push")
            rid = await c.execution_repo.start_execution(
                WorkItemInfo(id=9452, title="Report base query"), "interactive:autopilot-9452")
            await c.execution_repo.complete_execution(
                rid, ExecutionResult.ok(9452, "interactive:autopilot-9452", "done"))
            row, _ = await c.pr_conflict_repo.observe(
                "r1", 2748, repo_name="Micro-Frontend", title="Đồng bộ nhãn",
                source_branch="feature/7438", target_branch="dxfac/development")
            await c.pr_conflict_repo.update(row.id, status="in_session",
                                            session_name="autopilot-conflict-1",
                                            session_started=datetime.now(UTC) - timedelta(hours=1))
            await c.pr_session_repo.create(
                key="pr-4318", repo_id="r1", repo_name="Backend-Fresh", pr_id=4318,
                thread_id=5, work_item_id=9448, branch="feature/9448", instruction="/ai batch it",
                run_dir="C:/nowhere", session_name="autopilot-pr-4318")
        client.portal.call(seed)

        page = client.get("/dashboard/now").text
        assert "Không có lượt chạy nào đang diễn ra" in page
        # Up next and just finished keep an idle page useful.
        assert "#9447" in page and "Sequential push" in page
        assert "#9452" in page and "Report base query" in page
        # Interactive PR sessions are visible, with what to attach to and a way to close.
        assert "autopilot-conflict-1" in page and "!2748" in page
        assert "autopilot-pr-4318" in page and "!4318" in page
        assert re.search(r'action="/dashboard/sessions/\d+/cancel"', page)
        assert 'action="/dashboard/conflicts/' in page


def test_a_long_silent_run_is_flagged_stuck():
    with _client() as client:
        c = client.app.state.container

        async def seed():
            await c.execution_repo.start_execution(
                WorkItemInfo(id=9500, title="quiet one"), "headless")
        client.portal.call(seed)
        import ai_autopilot.dashboard as dash
        orig = dash.activity.last_event
        dash.activity.last_event = lambda ws, key: ("Read src/app.py", 900.0)
        try:
            page = client.get("/dashboard/now").text
        finally:
            dash.activity.last_event = orig
        assert 'class="run stuck"' in page and "15m 00s" in page
        assert "1 im lặng lâu" in page


def test_closing_an_ai_session_from_the_page():
    with _client() as client:
        closed: list[int] = []

        class _Monitor:
            async def cancel_session(self, sid):
                closed.append(sid)
                return True

        client.app.state.pr_monitor = _Monitor()
        r = client.post("/dashboard/sessions/7/cancel", follow_redirects=True)
        assert closed == [7] and "Đã đóng phiên" in r.text
