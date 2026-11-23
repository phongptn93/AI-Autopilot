"""North-star KPIs: the pure aggregation and the Analytics section."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.data.entities import ExecutionRecord, ExecutionStatus, PipelineState
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.north_star import compute_north_star

T0 = datetime(2026, 10, 1, 8, 0, 0)


def _pr(n: int) -> str:
    return f"https://dev.azure.com/org/P/_git/repo/pullrequest/{n}"


def _run(item, *, ok=True, pr=None, start=T0, minutes=30, retry=0, done=True):
    r = ExecutionRecord()
    r.work_item_id = item
    r.status = ExecutionStatus.SUCCESS if ok else ExecutionStatus.FAILED
    r.pr_url = pr
    r.started_at = start
    r.completed_at = start + timedelta(minutes=minutes) if done else None
    r.retry_count = retry
    return r


def _q(item, kind, value=0):
    return SimpleNamespace(work_item_id=item, kind=kind, value=value)


def test_autonomy_counts_only_clean_deliveries():
    runs = [
        _run(1, pr=_pr(1)),                                   # clean ✓
        _run(2, pr=_pr(2)),                                   # revised on PR ✗
        _run(3, ok=False), _run(3, pr=_pr(3), start=T0 + timedelta(hours=1)),  # failed first ✗
        _run(4, pr=_pr(4), retry=1),                          # retried ✗
        _run(5),                                              # success, no PR ✗
        _run(6, pr=_pr(6)),                                   # held for a human ✗
        _run(7, done=False),                                  # still running — not counted
    ]
    ns = compute_north_star(runs, [_q(2, "pr_revision", 1)], needs_human_items={6})
    assert (ns.autonomous_items, ns.finished_items) == (1, 6)
    assert ns.autonomy_rate == 17
    assert ns.needs_human_runs == 1 and ns.finished_runs == 7
    kpi = ns.kpis()[0]
    assert kpi.value == "17%" and kpi.sub == "1/6 item" and kpi.tone == "bad"
    assert "chưa theo dõi merge" in kpi.hint


def test_merge_tracking_requires_the_pr_to_have_merged():
    runs = [_run(1, pr=_pr(1)), _run(2, pr=_pr(2))]
    ns = compute_north_star(runs, merged_pr_ids={1})
    assert ns.autonomous_items == 1 and "PR đã merge" in ns.kpis()[0].hint


def test_time_to_first_pr_is_a_median_per_item():
    runs = [
        _run(1, ok=False, minutes=10),
        _run(1, pr=_pr(1), start=T0 + timedelta(minutes=20), minutes=40),   # 60 min
        _run(2, pr=_pr(2), minutes=20),                                      # 20 min
        _run(3, pr=_pr(3), minutes=300),                                     # 300 min
    ]
    ns = compute_north_star(runs)
    assert ns.median_first_pr_seconds == 3600
    kpi = ns.kpis()[1]
    assert kpi.value == "1 giờ" and kpi.tone == "good"


def test_feedback_share_and_honest_dashes_without_data():
    ns = compute_north_star([], [_q(1, "human_feedback", 1), _q(1, "human_feedback", 1),
                                 _q(2, "human_feedback", -1)])
    assert ns.approval_rate == 67
    kpis = {k.key: k for k in ns.kpis()}
    assert kpis["feedback"].value == "67%" and kpis["feedback"].tone == "warn"
    for key in ("autonomy", "first_pr", "needs_human", "human_minutes"):
        assert kpis[key].value == "—" and kpis[key].tone == "" and not kpis[key].has_data
    assert "Chưa đo được" in kpis["human_minutes"].hint


def test_down_vote_disqualifies_autonomy():
    ns = compute_north_star([_run(1, pr=_pr(1))], [_q(1, "human_feedback", -1)])
    assert ns.autonomous_items == 0 and ns.finished_items == 1


# ── the Analytics page ───────────────────────────────────────────────────────
def test_analytics_page_shows_the_north_star_section(tmp_path):
    settings = Settings(dry_run=True,
                        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    app = create_app(settings)
    with TestClient(app) as client:
        empty = client.get("/dashboard/analytics")
        assert empty.status_code == 200
        assert "⭐ Chỉ số đích" in empty.text and "Phút công người / item" in empty.text
        assert "chưa có dữ liệu trong khoảng này" in empty.text

        c = app.state.container
        item = WorkItemInfo(id=11, title="A", state="Active", project="P",
                            work_item_type="Task")
        rid = client.portal.call(c.execution_repo.start_execution, item, "agent")
        result = ExecutionResult.ok(11, "agent", "ok")
        result.pr_url = _pr(5)
        client.portal.call(c.execution_repo.complete_execution, rid, result)
        held = WorkItemInfo(id=12, title="B", state="Active", project="P",
                            work_item_type="Task")
        rid2 = client.portal.call(c.execution_repo.start_execution, held, "agent")
        client.portal.call(c.execution_repo.complete_execution, rid2,
                           ExecutionResult.fail(12, "agent", "needs human input"))
        client.portal.call(c.state_repo.set, 12, PipelineState.NEEDS_HUMAN)

        page = client.get("/dashboard/analytics")
        assert page.status_code == 200
        assert "1/2 item" in page.text          # autonomy: #11 clean, #12 held
        assert "1/2 run" in page.text           # needs-human share


# ── reliability: first try, cost, wasted spend, failure causes ─────────────────────────


def _costed(item, usd, **kw):
    r = _run(item, **kw)
    r.cost_usd = usd
    return r


def test_cost_per_delivered_counts_failed_attempts_of_delivered_items():
    runs = [
        _costed(1, 1.0, ok=False),
        _costed(1, 2.0, pr=_pr(1), start=T0 + timedelta(hours=1)),   # delivered: 3.0 total
        _costed(2, 5.0, pr=_pr(2)),                                  # delivered: 5.0
        _costed(3, 4.0, ok=False),                                   # never delivered: waste
    ]
    ns = compute_north_star(runs)
    assert ns.delivered_items == 2
    assert ns.cost_per_delivered == 4.0
    assert ns.wasted_share == 33          # 4 / 12
    by_key = {k.key: k for k in ns.kpis()}
    assert by_key["cost_per_pr"].value == "$4.00"
    assert by_key["wasted"].value == "33%"


def test_unknown_cost_is_not_treated_as_free():
    ns = compute_north_star([_run(1, pr=_pr(1))])        # cost_usd is None
    assert ns.cost_per_delivered is None and ns.wasted_share is None
    by_key = {k.key: k for k in ns.kpis()}
    assert not by_key["cost_per_pr"].has_data and not by_key["wasted"].has_data


def test_first_try_needs_pr_from_first_run_without_retry():
    runs = [
        _run(1, pr=_pr(1)),                                           # first try ✓
        _run(2, ok=False), _run(2, pr=_pr(2), start=T0 + timedelta(hours=1)),  # ✗
        _run(3, pr=_pr(3), retry=1),                                  # ✗
    ]
    ns = compute_north_star(runs)
    assert (ns.first_try_items, ns.finished_items) == (1, 3)
    assert ns.first_try_rate == 33


def test_failure_reasons_are_classified_and_unknown_is_honest():
    from ai_autopilot.north_star import classify_failure

    assert classify_failure("Claude timed out after 3600s") == "hết thời gian"
    assert classify_failure("HTTP 429 rate limit") == "hết quota / giới hạn"
    assert classify_failure("401 Unauthorized") == "xác thực"
    assert classify_failure("test gate: 3 tests failed") == "test gate"
    assert classify_failure("merge conflict in a.py") == "merge conflict"
    assert classify_failure("") == "không ghi lý do"
    assert classify_failure("something odd") == "khác"
    f1, f2, f3 = (_run(i, ok=False) for i in (1, 2, 3))
    f1.error = f2.error = "timed out"
    f3.error = "weird"
    ns = compute_north_star([f1, f2, f3])
    top = {k.key: k for k in ns.kpis()}["fail_reason"]
    assert top.value == "hết thời gian" and "khác 1" in top.sub
    assert "Tách item" in top.sub and "Tách item" in top.hint      # the lever, on the card


def test_every_classifiable_cause_has_a_remedy_and_unknowns_have_none():
    from ai_autopilot.north_star import _FAIL_RULES, failure_remedy

    assert all(failure_remedy(label) for label, _ in _FAIL_RULES)   # no rule without a lever
    assert failure_remedy("khác") == "" and failure_remedy("không ghi lý do") == ""
    f = _run(1, ok=False)
    f.error = "weird"
    top = {k.key: k for k in compute_north_star([f]).kpis()}["fail_reason"]
    assert top.value == "khác" and "Gợi ý" not in top.sub          # honest: no tip invented
