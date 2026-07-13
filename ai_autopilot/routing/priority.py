"""Priority scoring for work items (ported from ``WorkItemPriority``)."""

from __future__ import annotations

from ai_autopilot.models import TaskCategory, WorkItemInfo

_BASE_SCORE = {
    TaskCategory.BUG: 60,
    TaskCategory.BACKEND_TASK: 40,
    TaskCategory.FRONTEND_TASK: 40,
    TaskCategory.TEST_TASK: 35,
    TaskCategory.DATABASE_TASK: 30,
    TaskCategory.REQUIREMENT: 20,
    TaskCategory.UNKNOWN: 10,
}

_PRIORITY_BOOST = {1: 40, 2: 20, 3: 0, 4: -10}


def score(item: WorkItemInfo) -> int:
    base = _BASE_SCORE.get(item.category, 10)
    return base + _PRIORITY_BOOST.get(item.priority, 0)


def sort_by_priority(items: list[WorkItemInfo]) -> list[WorkItemInfo]:
    return sorted(items, key=score, reverse=True)
