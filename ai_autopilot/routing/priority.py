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

# A production incident outranks everything else on the board: more than the widest
# gap between any two ordinary items (a P1 bug at 100 vs a P4 unknown at 0).
HOTFIX_BOOST = 100
DEFAULT_HOTFIX_TAGS: tuple[str, ...] = ("Hotfix", "Production Incident")


def is_hotfix(item: WorkItemInfo, hotfix_tags=DEFAULT_HOTFIX_TAGS) -> bool:
    wanted = {t.strip().lower() for t in hotfix_tags or () if t and t.strip()}
    return any((t or "").strip().lower() in wanted for t in item.tags or [])


def score(item: WorkItemInfo, hotfix_tags=DEFAULT_HOTFIX_TAGS) -> int:
    base = _BASE_SCORE.get(item.category, 10)
    boost = HOTFIX_BOOST if is_hotfix(item, hotfix_tags) else 0
    return base + _PRIORITY_BOOST.get(item.priority, 0) + boost


def sort_by_priority(items: list[WorkItemInfo],
                     hotfix_tags=DEFAULT_HOTFIX_TAGS) -> list[WorkItemInfo]:
    return sorted(items, key=lambda i: score(i, hotfix_tags), reverse=True)
