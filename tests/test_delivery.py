"""Delivery (PM) analytics: throughput, lead time, ageing, flow.

The numbers on this page drive staffing and expectation-setting conversations, so the
tests below are mostly about the ways a plausible-looking figure can be WRONG: counting
a re-opened item twice, ageing a merge from the wrong clock, reporting "▼100%" against a
window that simply had no data, or inferring "stuck for days" from a field any comment
bumps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ai_autopilot.data.repository import StateChange
from ai_autopilot.delivery import (
    CAT_COMPLETED,
    CAT_IN_PROGRESS,
    CAT_PROPOSED,
    CAT_RESOLVED,
    KIND_BLOCKED_PR,
    KIND_FAILED,
    KIND_MERGE_READY,
    KIND_NEEDS_HUMAN,
    KIND_REVIEW_WAITING,
    KIND_STALE,
    PrView,
    Thresholds,
    build_flow,
    compute_delivery,
    delivered_between,
    percentile,
)
from ai_autopilot.models import WorkItemInfo

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

CATEGORIES = {
    "New": CAT_PROPOSED,
    "Active": CAT_IN_PROGRESS,
    "Resolved": CAT_RESOLVED,
    "Closed": CAT_COMPLETED,
}


def _item(wid: int, state: str = "Active", *, owner: str = "An", project: str = "P1",
          created_days_ago: float = 5, changed_days_ago: float | None = None) -> WorkItemInfo:
    return WorkItemInfo(
        id=wid, title=f"Item {wid}", state=state, project=project, assigned_to=owner,
        created_date=NOW - timedelta(days=created_days_ago),
        changed_date=NOW - timedelta(
            days=changed_days_ago if changed_days_ago is not None else 0.1),
    )


def _change(wid: int, state: str, days_ago: float, *, owner: str = "An",
            project: str = "P1") -> StateChange:
    return StateChange(
        work_item_id=wid, project=project, state=state,
        category=CATEGORIES.get(state, ""), assigned_to=owner, title=f"Item {wid}",
        entered_at=NOW - timedelta(days=days_ago),
    )


def _report(**over):
    base = dict(
        items=[], categories=CATEGORIES, prs=[], changes=[], ai_item_ids=set(),
        days=14, now=NOW,
    )
    base.update(over)
    return compute_delivery(**base)


# ── percentile ───────────────────────────────────────────────────────────────


def test_percentile_returns_an_observed_value_not_an_interpolation():
    # Interpolating would invent a lead time no item actually had.
    values = [1.0, 2.0, 10.0]
    assert percentile(values, 50) in values
    assert percentile(values, 85) in values
    assert percentile(values, 100) == 10.0


def test_percentile_of_nothing_is_zero_not_an_error():
    assert percentile([], 50) == 0.0


# ── delivered_between ────────────────────────────────────────────────────────


def test_delivered_counts_the_first_done_transition_only():
    # Resolved → reopened → resolved again is ONE delivery, not two.
    changes = [
        _change(1, "Active", 9), _change(1, "Closed", 8),
        _change(1, "Active", 5), _change(1, "Closed", 2),
    ]
    delivered = delivered_between(changes, NOW - timedelta(days=14), NOW)
    assert list(delivered) == [1]
    assert delivered[1] == NOW - timedelta(days=8)


def test_item_delivered_before_the_window_is_not_counted_again():
    changes = [_change(1, "Closed", 30), _change(1, "Active", 6), _change(1, "Closed", 3)]
    assert delivered_between(changes, NOW - timedelta(days=14), NOW) == {}


def test_resolved_counts_as_delivered():
    # Many boards ship from Resolved; excluding it reports a shipping team as idle.
    changes = [_change(1, "Resolved", 2)]
    assert 1 in delivered_between(changes, NOW - timedelta(days=14), NOW)


# ── flow ─────────────────────────────────────────────────────────────────────


def test_flow_has_one_column_per_day_oldest_first():
    flow = build_flow([_change(1, "Active", 2)], days=5, now=NOW)
    assert len(flow) == 5
    assert flow[0][0] < flow[-1][0]


def test_flow_carries_state_forward_until_it_changes():
    changes = [_change(1, "Active", 4), _change(1, "Closed", 2)]
    flow = dict(build_flow(changes, days=5, now=NOW))
    days = sorted(flow)
    assert flow[days[1]][CAT_IN_PROGRESS] == 1     # day after it started
    assert flow[days[-1]][CAT_COMPLETED] == 1      # after it closed
    assert flow[days[-1]][CAT_IN_PROGRESS] == 0


def test_flow_ignores_items_before_their_first_recorded_transition():
    # The chart must grow into correctness, not open with a fabricated backlog.
    flow = build_flow([_change(1, "Active", 1)], days=5, now=NOW)
    assert sum(flow[0][1].values()) == 0
    assert sum(flow[-1][1].values()) == 1


# ── PR ageing ────────────────────────────────────────────────────────────────


def _pr(**over) -> PrView:
    base = dict(id=7, repo="Api", title="Add login", author="An", work_item_id=1,
                project="P1", created_at=NOW - timedelta(days=5))
    base.update(over)
    return PrView(**base)


def test_merge_ready_is_aged_from_the_approval_not_the_pr_creation():
    # A PR open for 5 days but approved an hour ago is not a merge backlog.
    pr = _pr(approved=2, approved_at=NOW - timedelta(hours=1))
    report = _report(prs=[pr], thresholds=Thresholds(merge_hours=24))
    assert [a.kind for a in report.actions] == []

    stale_approval = _pr(approved=2, approved_at=NOW - timedelta(days=2))
    report = _report(prs=[stale_approval], thresholds=Thresholds(merge_hours=24))
    assert [a.kind for a in report.actions] == [KIND_MERGE_READY]


def test_merge_ready_falls_back_to_creation_when_the_vote_time_is_unknown():
    report = _report(prs=[_pr(approved=1)], thresholds=Thresholds(merge_hours=24))
    assert [a.kind for a in report.actions] == [KIND_MERGE_READY]


def test_a_rejected_pr_is_reported_as_rejected_not_as_waiting_for_review():
    pr = _pr(approved=1, blocked=1, pending=1)
    report = _report(prs=[pr])
    assert [a.kind for a in report.actions] == [KIND_BLOCKED_PR]


def test_pending_reviewers_are_named_in_the_action():
    pr = _pr(pending=2, pending_reviewers=("Bình", "Chi"))
    report = _report(prs=[pr], thresholds=Thresholds(review_hours=24))
    assert report.actions[0].kind == KIND_REVIEW_WAITING
    assert "Bình" in report.actions[0].detail and "Chi" in report.actions[0].detail


def test_a_fresh_pr_is_not_an_action():
    pr = _pr(pending=1, created_at=NOW - timedelta(hours=2))
    assert _report(prs=[pr], thresholds=Thresholds(review_hours=24)).actions == []


def test_actions_are_ordered_by_urgency_then_by_age():
    prs = [
        _pr(id=1, approved=1, approved_at=NOW - timedelta(days=2)),
        _pr(id=2, work_item_id=2, pending=1, created_at=NOW - timedelta(days=9)),
        _pr(id=3, work_item_id=3, blocked=1),
    ]
    report = _report(prs=prs)
    assert [a.kind for a in report.actions] == [
        KIND_BLOCKED_PR, KIND_MERGE_READY, KIND_REVIEW_WAITING
    ]


def test_held_and_failed_items_become_actions():
    held = [SimpleNamespace(work_item_id=5, title="Bí", detail="cần quyết",
                            updated_at=NOW - timedelta(days=1))]
    failed = [SimpleNamespace(work_item_id=6, title="Hỏng", error="boom",
                              completed_at=NOW - timedelta(hours=3), started_at=None)]
    kinds = [a.kind for a in _report(held=held, failed=failed).actions]
    assert kinds == [KIND_NEEDS_HUMAN, KIND_FAILED]


# ── stale detection ──────────────────────────────────────────────────────────


def test_stale_uses_the_recorded_transition_when_there_is_one():
    item = _item(1, "Active", changed_days_ago=0.1)     # edited an hour ago…
    changes = [_change(1, "Active", 10)]                # …but state unchanged for 10 days
    baseline = NOW - timedelta(days=20)
    report = _report(items=[item], changes=changes, history_since=baseline,
                     thresholds=Thresholds(stale_days=3))
    assert [a.kind for a in report.actions] == [KIND_STALE]
    assert report.actions[0].age_hours > 200


def test_baseline_row_defers_to_an_older_changed_date():
    # The first recorded row means "already in this state when we started looking", not
    # "moved just now" — so an OLDER changed_date (which can only be bumped forward)
    # is the more trustworthy answer.
    baseline = NOW - timedelta(days=1)
    item = _item(1, "Active", changed_days_ago=9)
    changes = [_change(1, "Active", 1)]                  # == the baseline snapshot
    report = _report(items=[item], changes=changes, history_since=baseline,
                     thresholds=Thresholds(stale_days=3))
    assert [a.kind for a in report.actions] == [KIND_STALE]


def test_recently_moved_item_is_not_stale():
    report = _report(
        items=[_item(1, "Active")], changes=[_change(1, "Active", 0.5)],
        history_since=NOW - timedelta(days=20), thresholds=Thresholds(stale_days=3),
    )
    assert report.actions == []


def test_an_item_with_a_pr_is_waiting_to_land_not_stale():
    # Work is finished; the queue is the merge queue, and double-reporting it as
    # "nobody is touching this" would send a PM to the wrong person.
    item = _item(1, "Active")
    report = _report(
        items=[item], prs=[_pr(approved=1, approved_at=NOW - timedelta(hours=1))],
        changes=[_change(1, "Active", 30)], history_since=NOW - timedelta(days=40),
        thresholds=Thresholds(stale_days=3),
    )
    assert [a.kind for a in report.actions] == []
    assert report.people[0].waiting_merge == 1
    assert report.people[0].wip == 0


# ── KPIs ─────────────────────────────────────────────────────────────────────


def test_no_previous_window_shows_no_comparison_rather_than_a_fake_collapse():
    report = _report(changes=[_change(1, "Closed", 2)], items=[_item(1, "Closed")])
    delivered = report.kpis[0]
    assert delivered.value == 1
    assert delivered.previous is None
    assert delivered.delta is None


def test_delivery_kpi_compares_against_the_preceding_window():
    changes = [
        _change(1, "Closed", 20), _change(2, "Closed", 18),   # previous window
        _change(3, "Closed", 3),                              # current window
    ]
    report = _report(changes=changes, days=14, history_since=NOW - timedelta(days=40))
    delivered = report.kpis[0]
    assert (delivered.value, delivered.previous) == (1, 2)
    assert delivered.direction == "down" and delivered.tone == "bad"


def test_comparison_is_withheld_until_history_covers_the_previous_window():
    changes = [_change(1, "Closed", 2)]
    # Recording only started 3 days ago — the previous fortnight is unobserved, not empty.
    report = _report(changes=changes, days=14, history_since=NOW - timedelta(days=3))
    assert report.kpis[0].previous is None


def test_lead_time_falling_reads_as_good():
    kpi = _report().kpis[1]
    assert kpi.higher_is_better is False


def test_ai_share_counts_delivered_items_the_autopilot_touched():
    changes = [_change(1, "Closed", 2), _change(2, "Closed", 3)]
    items = [_item(1, "Closed"), _item(2, "Closed")]
    report = _report(items=items, changes=changes, ai_item_ids={1})
    ai = next(k for k in report.kpis if "AI" in k.label)
    assert ai.value == 50


def test_wip_counts_items_in_progress():
    items = [_item(1, "Active"), _item(2, "Active"), _item(3, "New")]
    report = _report(items=items)
    wip = next(k for k in report.kpis if "WIP" in k.label)
    assert wip.value == 2


def test_wip_includes_work_finished_but_not_yet_merged():
    # Counting only "Active" reported 0 work in flight while the flow chart showed two
    # items moving — the headline and the chart must not contradict each other.
    items = [_item(1, "Active"), _item(2, "Active")]
    report = _report(items=items, prs=[_pr(work_item_id=1, approved=1)])
    wip = next(k for k in report.kpis if "WIP" in k.label)
    assert wip.value == 2
    by_project = {p.project: p for p in report.projects}
    assert (by_project["P1"].wip, by_project["P1"].waiting_merge) == (1, 1)


# ── people & projects ────────────────────────────────────────────────────────


def test_people_are_sorted_by_current_load():
    items = [
        _item(1, "Active", owner="An"), _item(2, "Active", owner="An"),
        _item(3, "Active", owner="Bình"),
    ]
    report = _report(items=items)
    assert [p.name for p in report.people] == ["An", "Bình"]
    assert report.people[0].load == 2


def test_unassigned_work_is_shown_rather_than_dropped():
    report = _report(items=[WorkItemInfo(id=1, title="x", state="Active", project="P1")])
    assert report.people[0].name == "(chưa gán)"


def test_projects_roll_up_delivery_and_ai_share():
    items = [_item(1, "Closed", project="P1"), _item(2, "Closed", project="P2")]
    changes = [_change(1, "Closed", 2, project="P1"), _change(2, "Closed", 2, project="P2")]
    report = _report(items=items, changes=changes, ai_item_ids={1})
    by_name = {p.project: p for p in report.projects}
    assert by_name["P1"].ai_share == 100
    assert by_name["P2"].ai_share == 0


# ── honesty about missing history ────────────────────────────────────────────


def test_flow_is_flagged_partial_when_history_does_not_reach_the_window_start():
    assert _report(history_since=NOW - timedelta(days=2), days=14).flow_is_partial
    assert not _report(history_since=NOW - timedelta(days=30), days=14).flow_is_partial


def test_flow_is_partial_when_nothing_was_ever_recorded():
    assert _report(history_since=None).flow_is_partial


def test_age_label_reads_in_the_right_unit():
    report = _report(prs=[_pr(approved=1, approved_at=NOW - timedelta(days=3))])
    assert report.actions[0].age_label.endswith("ngày")
