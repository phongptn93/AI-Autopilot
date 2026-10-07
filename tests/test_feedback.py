"""1-tap feedback: the page, the vote (latest wins), the lesson, and the notice buttons."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ai_autopilot import lessons
from ai_autopilot.ado.notifier import AdoNotifier
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard.routes.feedback import compose_reason, lesson_text
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.run_timeline import HUMAN_FEEDBACK

PR = "https://dev.azure.com/org/P/_git/shop-api/pullrequest/77"


def test_compose_reason_and_lesson_text():
    got = compose_reason(["Thiếu test", "bogus"], "  thiếu case âm ")
    assert got == "Thiếu test — thiếu case âm"
    assert compose_reason([], "") == ""
    assert lesson_text("Thiếu test", 9) == "Người review: Thiếu test (#9)"
    assert lesson_text("Khác", 9) == ""              # says nothing teachable
    assert lesson_text("", 9) == ""


@pytest.fixture
def app_client(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    settings = Settings(dry_run=True, workspace_directory=str(ws), learning_loop_enabled=True,
                        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    app = create_app(settings)
    with TestClient(app) as client:
        yield app, client, str(ws)


def _seed(client, app, *, item_id=4242, pr=PR):
    c = app.state.container
    item = WorkItemInfo(id=item_id, title="Tính tồn", state="Active", project="P",
                        work_item_type="Task")
    rid = client.portal.call(c.execution_repo.start_execution, item, "agent")
    result = ExecutionResult.ok(item_id, "agent", "done")
    result.pr_url = pr
    client.portal.call(c.execution_repo.complete_execution, rid, result)
    return rid


def _votes(client, app, item_id=4242):
    return client.portal.call(lambda: app.state.container.quality_events.recent(
        kind=HUMAN_FEEDBACK, work_item_id=item_id))


def test_page_renders_with_vote_preselected(app_client):
    app, client, _ = app_client
    rid = _seed(client, app)
    page = client.get(f"/dashboard/feedback/{rid}?v=down")
    assert page.status_code == 200
    assert "Tính tồn" in page.text and PR in page.text
    assert 'value="down" checked' in page.text
    assert "Đụng file không liên quan" in page.text
    assert client.get("/dashboard/feedback/99999").status_code == 404


def test_vote_is_stored_once_per_run_and_latest_wins(app_client):
    app, client, ws = app_client
    rid = _seed(client, app)
    r = client.post(f"/dashboard/feedback/{rid}", data={"v": "up"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post(f"/dashboard/feedback/{rid}",
                    data={"v": "down", "chip": ["Thiếu test"], "reason": "không có case lỗi"},
                    follow_redirects=False)
    assert r.status_code == 303
    votes = _votes(client, app)
    assert len(votes) == 1
    assert votes[0].value == -1 and votes[0].pr_id == 77
    assert votes[0].detail == "Thiếu test — không có case lỗi"
    assert votes[0].stage == f"exec:{rid}"
    # Audited, and the 👎 with a reason became a lesson for the PR's repo.
    audit = client.portal.call(app.state.container.audit_repo.recent)
    assert any(a.action == "run.feedback" and a.target == "#4242" for a in audit)
    assert any("Thiếu test" in t for t in lessons.read_lessons(ws, "shop-api"))
    # The page after the redirect shows the flash and the stored vote.
    page = client.get(f"/dashboard/feedback/{rid}")
    assert "Đã ghi nhận" in page.text and "👎 Chưa tốt" in page.text


def test_down_vote_without_repo_goes_to_shared_bucket(app_client):
    app, client, ws = app_client
    rid = _seed(client, app, item_id=5151, pr=None)
    client.post(f"/dashboard/feedback/{rid}", data={"v": "down", "chip": ["Sai yêu cầu"]})
    assert any("Sai yêu cầu" in t for t in lessons.read_lessons(ws, lessons.SHARED_BUCKET))


def test_up_vote_records_no_lesson_and_bad_vote_is_ignored(app_client):
    app, client, ws = app_client
    rid = _seed(client, app)
    client.post(f"/dashboard/feedback/{rid}", data={"v": "sideways"})
    assert _votes(client, app) == []
    client.post(f"/dashboard/feedback/{rid}", data={"v": "up", "reason": "ổn"})
    assert lessons.read_lessons(ws, "shop-api") == []


def test_item_link_resolves_the_run_and_shows_in_the_timeline(app_client):
    app, client, _ = app_client
    rid = _seed(client, app)
    at = int(datetime.now(UTC).timestamp())
    r = client.get(f"/dashboard/feedback/item/4242?at={at}&v=up", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/dashboard/feedback/{rid}?v=up"
    assert client.get("/dashboard/feedback/item/1?v=up").status_code == 404
    client.post(f"/dashboard/feedback/{rid}", data={"v": "up"})
    room = client.get("/dashboard/task/4242")
    assert "Người review đánh giá: tốt" in room.text


def test_latest_finished_for_item_picks_the_run_nearest_the_notice(app_client):
    app, client, _ = app_client
    first = _seed(client, app)
    second = _seed(client, app)
    repo = app.state.container.execution_repo
    newest = client.portal.call(repo.latest_finished_for_item, 4242)
    assert newest.id == second
    long_ago = datetime.now(UTC) - timedelta(days=3)
    picked = client.portal.call(lambda: repo.latest_finished_for_item(4242, near=long_ago))
    assert picked.id in (first, second)


# ── completion notice buttons ────────────────────────────────────────────────
class _Ado:
    def __init__(self):
        self.comments = []

    async def add_comment(self, item_id, html):
        self.comments.append((item_id, html))


def _item():
    return WorkItemInfo(id=7, title="T", state="Active", project="P", work_item_type="Task")


def test_feedback_actions_need_a_public_url():
    result = ExecutionResult.ok(7, "agent", "ok")
    with_url = AdoNotifier(_Ado(), Settings(dashboard_public_url="https://ap.example.com/"), [])
    actions = with_url._feedback_actions(_item(), result)
    assert [a[0] for a in actions] == ["👍 Tốt", "👎 Chưa tốt"]
    assert actions[0][1].startswith("https://ap.example.com/dashboard/feedback/item/7?at=")
    assert actions[0][1].endswith("&v=up") and actions[1][1].endswith("&v=down")
    without = AdoNotifier(_Ado(), Settings(), [])
    assert without._feedback_actions(_item(), result) == []


def test_a_down_vote_writes_no_lesson_when_learning_is_off(tmp_path):
    """Learning switched off means nothing goes into the agent's memory — not even a
    reviewer's reason. The vote itself is still kept."""
    from ai_autopilot import lessons

    ws = tmp_path / "ws"
    ws.mkdir()
    settings = Settings(dry_run=True, workspace_directory=str(ws), learning_loop_enabled=False,
                        database_url=f"sqlite+aiosqlite:///{tmp_path / 'off.sqlite'}")
    app = create_app(settings)
    with TestClient(app) as client:
        rid = _seed(client, app)
        client.post(f"/dashboard/feedback/{rid}",
                    data={"v": "down", "chip": ["Thiếu test"], "reason": "thiếu case âm"})
    assert lessons.all_entries(str(ws)) == []
