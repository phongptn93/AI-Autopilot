"""Priority scoring tests (ported from WorkItemPriority)."""

from __future__ import annotations

from ai_autopilot.models import TaskCategory, WorkItemInfo
from ai_autopilot.routing import score, sort_by_priority


def _item(category, priority=3, wid=1) -> WorkItemInfo:
    return WorkItemInfo(id=wid, category=category, priority=priority)


def test_bug_outranks_backend_at_same_priority():
    assert score(_item(TaskCategory.BUG)) > score(_item(TaskCategory.BACKEND_TASK))


def test_priority_field_boosts_score():
    critical = _item(TaskCategory.BACKEND_TASK, priority=1)
    low = _item(TaskCategory.BACKEND_TASK, priority=4)
    assert score(critical) == 80  # 40 + 40
    assert score(low) == 30  # 40 - 10


def test_sort_orders_by_descending_score():
    items = [
        _item(TaskCategory.REQUIREMENT, wid=1),
        _item(TaskCategory.BUG, priority=1, wid=2),
        _item(TaskCategory.FRONTEND_TASK, wid=3),
    ]
    ordered = [i.id for i in sort_by_priority(items)]
    assert ordered == [2, 3, 1]
