"""Run timeline (🎬 Diễn biến): the pure merge, the "why" panel, and the task room tab."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.data.entities import ExecutionRecord, ExecutionStatus
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.run_timeline import build_timeline, mentions_item, rationale

T0 = datetime(2026, 10, 1, 8, 0, 0)
PR = "https://dev.azure.com/org/P/_git/shop-api/pullrequest/77"


def _run(**kw) -> ExecutionRecord:
    r = ExecutionRecord()
    defaults = dict(
        id=1, work_item_id=42, status=ExecutionStatus.SUCCESS, skill_used="agent",
        started_at=T0, completed_at=T0 + timedelta(minutes=12), pr_url=PR, pr_urls=None,
        files_changed='["a.py", "b.py"]', error=None, output="Đã thêm API tính tồn.",
        duration_seconds=720.0, cost_tokens=12000, cost_usd=None, model_used="opus",
        tests_total=3, tests_failed=0, tests_blocked=0, retry_count=0, profile="dev",
    )
    defaults.update(kw)
    for k, v in defaults.items():
        setattr(r, k, v)
    return r


def _q(kind, at, value=0, **kw):
    return SimpleNamespace(kind=kind, at=at, value=value, work_item_id=kw.pop("wid", 42),
                           actor=kw.pop("actor", "autopilot"), pr_id=kw.pop("pr_id", 0),
                           detail=kw.pop("detail", ""), stage=kw.pop("stage", ""))


def test_events_are_merged_in_time_order_across_sources():
    drift = SimpleNamespace(
        work_item_id=42, where="FR-03", summary="Bỏ làm tròn", pr_url=PR, decision="",
        created_at=T0 + timedelta(minutes=13), resolved_at=T0 + timedelta(hours=2),
        resolved_by="dashboard", decision_note="ok", spec_says="", code_does="",
    )
    audit = [
        SimpleNamespace(at=T0 + timedelta(minutes=1), action="board.run", actor="dashboard",
                        source="dashboard", target="#42", detail=""),
        SimpleNamespace(at=T0 + timedelta(minutes=2), action="x", actor="a", source="b",
                        target="#420", detail=""),            # another item — excluded
    ]
    quality = [
        _q("pr_revision", T0 + timedelta(hours=1), 1, pr_id=77),
        _q("human_feedback", T0 + timedelta(hours=3), -1, detail="Thiếu test", stage="exec:1"),
        _q("test_failed", T0, wid=99),                       # another item — excluded
    ]
    conflict = SimpleNamespace(pr_id=77, work_item_id=0, url=PR, attempts=2, last_error="",
                               first_seen=T0 + timedelta(minutes=30), resolved_at=None,
                               resolved_by="")
    cmd = SimpleNamespace(kind="run_item", args='{"id": 42}', created_at=T0 - timedelta(minutes=1),
                          status="done", created_by="dashboard", worker="vm-2", detail="")
    other_cmd = SimpleNamespace(kind="run_item", args='{"id": 7}', created_at=T0,
                                status="done", created_by="", worker="vm-2", detail="")

    events = build_timeline(
        42, executions=[_run()], quality=quality, drifts=[drift], audit=audit,
        conflicts=[conflict], commands=[cmd, other_cmd], history_url="/dashboard/history?q=42",
    )
    kinds = [e.kind for e in events]
    assert kinds == ["dispatch", "run_start", "audit", "run_end", "drift", "conflict",
                     "quality", "drift_decided", "feedback"]
    end = events[3]
    assert end.tone == "ok" and ("PR shop-api !77", PR) in end.links
    assert "12,000 token" in end.detail and "test 3/3 đạt" in end.detail
    assert "$" not in end.detail                      # unknown cost is omitted, not $0.00
    assert events[6].links == [("PR !77", PR)]        # quality event linked to its PR
    assert events[-1].icon == "👎" and events[-1].detail == "Thiếu test"


def test_failed_run_shows_its_error_and_aware_datetimes_sort_with_naive():
    failed = _run(id=2, status=ExecutionStatus.FAILED, pr_url=None, error="build broke",
                  started_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                  completed_at=datetime(2026, 10, 1, 9, 5, tzinfo=UTC))
    events = build_timeline(42, executions=[_run(), failed])
    assert [e.kind for e in events] == ["run_start", "run_end", "run_start", "run_end"]
    assert events[-1].tone == "bad" and "build broke" in events[-1].detail


def test_mentions_item_matches_whole_tokens_only():
    assert mentions_item("42", 42) and mentions_item("#42", 42)
    assert mentions_item("#41 #42", 42)
    assert not mentions_item("#420", 42) and not mentions_item("142", 42)
    assert not mentions_item("", 42)


def test_rationale_uses_latest_finished_run_drifts_score_and_vote():
    old = _run(id=1, completed_at=T0, output="cũ")
    new = _run(id=2, completed_at=T0 + timedelta(hours=1), output="x" * 50)
    running = _run(id=3, completed_at=None)
    drift = SimpleNamespace(where="AC-2", summary="Đổi mặc định", spec_says="10",
                            code_does="20", decision="", resolved_at=None)
    votes = [_q("human_feedback", T0, 1, stage="exec:2"),
             _q("human_feedback", T0 + timedelta(minutes=5), -1, stage="exec:2", detail="Sai"),
             _q("human_feedback", T0 + timedelta(minutes=9), 1, stage="exec:1")]
    why = rationale([old, new, running], [drift], votes, excerpt_chars=10)
    assert why.run_id == 2 and why.summary == "x" * 10 and why.truncated
    assert why.score is not None and why.grade
    assert why.deviations[0].spec_says == "10" and why.deviations[0].code_does == "20"
    assert why.vote == -1 and why.vote_reason == "Sai"   # latest vote on run 2 wins


def test_rationale_without_finished_runs_is_empty():
    why = rationale([_run(completed_at=None)])
    assert why.empty and why.score is None


# ── the task room tab ────────────────────────────────────────────────────────
def _app(tmp_path):
    return create_app(Settings(dry_run=True,
                               database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"))


def _seed_run(client, app, *, item_id=4242):
    c = app.state.container
    item = WorkItemInfo(id=item_id, title="Tính tồn", state="Active", project="P",
                        work_item_type="Task")
    rid = client.portal.call(c.execution_repo.start_execution, item, "agent")
    result = ExecutionResult.ok(item_id, "agent", "Đã làm xong phần API tồn kho.")
    result.pr_url = PR
    client.portal.call(c.execution_repo.complete_execution, rid, result)
    return rid


def test_task_room_defaults_to_the_story_when_the_item_has_runs(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        rid = _seed_run(client, app)
        page = client.get("/dashboard/task/4242")
        assert page.status_code == 200
        assert "🎬 Diễn biến theo thời gian" in page.text
        assert f"Run #{rid} hoàn tất" in page.text
        assert "Vì sao agent quyết như vậy" in page.text
        assert "Đã làm xong phần API tồn kho." in page.text
        assert f"/dashboard/feedback/{rid}?v=up" in page.text


def test_task_room_without_runs_keeps_the_overview_default(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        page = client.get("/dashboard/task/5151")
        assert page.status_code == 200
        assert "Trạng thái đã đi qua" in page.text
        assert "🎬 Diễn biến theo thời gian" not in page.text
        story = client.get("/dashboard/task/5151?tab=story")
        assert story.status_code == 200 and "Chưa ghi nhận sự kiện nào" in story.text
