"""Tests for the ROI analytics aggregation (pure function)."""

from __future__ import annotations

from datetime import datetime, timedelta

from ai_autopilot.analytics import compute_analytics
from ai_autopilot.data.entities import ExecutionRecord, ExecutionStatus


def _rec(*, item, status, cat="BackendTask", pr=None, tokens=0, dur=0.0, started=None):
    r = ExecutionRecord()
    r.work_item_id = item
    r.status = status
    r.category = cat
    r.pr_url = pr
    r.cost_tokens = tokens
    r.duration_seconds = dur
    r.started_at = started
    return r


def test_compute_analytics_aggregates():
    now = datetime(2026, 7, 29, 12, 0, 0)
    yst = now - timedelta(days=1)
    recs = [
        _rec(item=1, status=ExecutionStatus.SUCCESS, pr="http://pr/1", tokens=100,
             dur=60, started=now),
        _rec(item=1, status=ExecutionStatus.FAILED, tokens=50, dur=30, started=yst),
        _rec(item=2, status=ExecutionStatus.SUCCESS, pr="http://pr/2", tokens=200, dur=90,
             cat="FrontendTask", started=yst),
    ]
    rep = compute_analytics(recs, days=7, now=now)

    assert rep.total_runs == 3
    assert rep.success == 2 and rep.failed == 1
    assert rep.success_rate == 67                      # 2/3 done
    assert rep.with_pr == 2 and rep.pr_rate == 67
    assert rep.distinct_items == 2 and rep.runs_per_item == 1.5
    assert rep.total_tokens == 350
    assert rep.tokens_per_pr == 175                    # 350 / 2 PRs
    assert rep.avg_duration_seconds == 60.0            # (60+30+90)/3
    # per-day series is dense over the window and ends today
    assert len(rep.per_day) == 7
    assert rep.per_day[-1] == (now.date().isoformat(), 1)
    assert rep.peak_day == 2                            # two runs on now-1d
    cats = {cs.category: cs.runs for cs in rep.by_category}
    assert cats == {"BackendTask": 2, "FrontendTask": 1}


def test_compute_analytics_empty():
    rep = compute_analytics([], days=30, now=datetime(2026, 7, 29))
    assert rep.total_runs == 0 and rep.success_rate == 0 and rep.tokens_per_pr is None
    assert len(rep.per_day) == 30 and rep.peak_day == 0
