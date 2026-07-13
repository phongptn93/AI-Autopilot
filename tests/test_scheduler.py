"""Tests for the dependency-aware scheduler (P1) — pure function ``plan_schedule``."""

from __future__ import annotations

from ai_autopilot.models import TaskCategory, WorkItemInfo
from ai_autopilot.routing.scheduler import plan_schedule
from ai_autopilot.services.poller import _scheduler_snapshot


def _item(
    id: int, *, category: TaskCategory = TaskCategory.BACKEND_TASK, priority: int = 3
) -> WorkItemInfo:
    return WorkItemInfo(id=id, title=f"#{id}", category=category, priority=priority)


def test_no_links_dispatches_all_in_priority_order():
    # Bug (base 60) + P1 boost should outrank a normal backend task.
    a = _item(1, category=TaskCategory.BACKEND_TASK, priority=3)
    b = _item(2, category=TaskCategory.BUG, priority=1)
    plan = plan_schedule([a, b])
    assert [i.id for i in plan.ready] == [2, 1]
    assert plan.deferred == {}


def test_unmet_predecessor_defers_successor():
    # #2 depends on #1; #1 is still active (open) → #2 waits.
    plan = plan_schedule(
        [_item(2)],
        predecessors={2: {1}},
        active_ids={1},
    )
    assert plan.ready == []
    assert "tiền nhiệm" in plan.deferred[2] and "#1" in plan.deferred[2]


def test_met_predecessor_lets_successor_run():
    # #1 not in active_ids → considered done → #2 is free to go.
    plan = plan_schedule(
        [_item(2)],
        predecessors={2: {1}},
        active_ids=set(),
    )
    assert [i.id for i in plan.ready] == [2]
    assert plan.deferred == {}


def test_related_items_do_not_run_together():
    # Two related items: the higher-priority one runs, the other defers this wave.
    hi = _item(1, priority=1)
    lo = _item(2, priority=3)
    plan = plan_schedule([lo, hi], related={1: {2}, 2: {1}})
    assert [i.id for i in plan.ready] == [1]
    assert 2 in plan.deferred and "xung đột" in plan.deferred[2]


def test_unrelated_items_both_run():
    plan = plan_schedule([_item(1), _item(2)], related={1: {3}})  # #3 not a candidate
    assert {i.id for i in plan.ready} == {1, 2}
    assert plan.deferred == {}


def test_busy_related_neighbour_blocks_candidate():
    # #1 is already in-flight; its related #2 must wait even though #2 is alone.
    plan = plan_schedule([_item(2)], related={2: {1}}, busy_ids={1})
    assert plan.ready == []
    assert "#1" in plan.deferred[2]


def test_max_dispatch_caps_the_wave():
    items = [_item(1, priority=1), _item(2, priority=2), _item(3, priority=3)]
    plan = plan_schedule(items, max_dispatch=2)
    assert [i.id for i in plan.ready] == [1, 2]
    assert 3 in plan.deferred and "slot" in plan.deferred[3]


def test_scheduler_snapshot_shape():
    a = _item(1, priority=1)
    b = _item(2, priority=3)
    b.predecessor_ids = [1]
    plan = plan_schedule([a, b], predecessors={2: {1}}, active_ids={1})
    snap = _scheduler_snapshot([a, b], plan, at=None)
    assert [r["id"] for r in snap["ready"]] == [1]
    assert snap["candidates"] == 2
    assert snap["deferred"][0]["id"] == 2
    assert snap["deferred"][0]["title"] == "#2"
    assert "tiền nhiệm" in snap["deferred"][0]["reason"]


def test_predecessor_and_related_chain_forms_waves():
    # #1 free, #2 related to #1, #3 depends on #1. Wave-1: only #1 (2 defers on
    # conflict, 3 defers on predecessor).
    items = [_item(1, priority=1), _item(2, priority=2), _item(3, priority=2)]
    plan = plan_schedule(
        items,
        predecessors={3: {1}},
        related={1: {2}, 2: {1}},
        active_ids={1, 2, 3},
    )
    assert [i.id for i in plan.ready] == [1]
    assert set(plan.deferred) == {2, 3}
