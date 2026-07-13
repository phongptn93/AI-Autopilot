"""Classify work items and route them to Claude Code skills (ported from ``TaskRouter``)."""

from __future__ import annotations

import re

from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import TaskCategory, WorkItemInfo

# Skill command templates — these invoke skills already in .claude/skills/.
_SKILL_MAP: dict[TaskCategory, str] = {
    TaskCategory.BACKEND_TASK: "/crud-full-stack {id}",
    TaskCategory.FRONTEND_TASK: "/fe-module {id}",
    TaskCategory.DATABASE_TASK: "/sql-migration {id}",
    TaskCategory.BUG: "/bugfix-workflow {id}",
    TaskCategory.TEST_TASK: "/qc-test-management {id}",
    TaskCategory.REQUIREMENT: "/analyze-requirement {id}",
}

_BACKEND_KW = ("API", "CONTROLLER", "SERVICE", "ENTITY", "MIGRATION", "ENDPOINT")
_FRONTEND_KW = ("COMPONENT", "PAGE", "FORM", "UI", "ANGULAR", "SCREEN")


class TaskRouter:
    def __init__(self) -> None:
        self._log = get_logger("routing.task_router")

    def classify(self, item: WorkItemInfo) -> WorkItemInfo:
        item.category = self._detect_category(item)
        return item

    def route(self, item: WorkItemInfo) -> str | None:
        """Map a classified item to a skill command, or None if unroutable."""
        if item.category is TaskCategory.UNKNOWN:
            self._log.warning("cannot route", id=item.id, category=str(item.category))
            return None
        template = _SKILL_MAP.get(item.category)
        if template is None:
            return None
        return template.replace("{id}", str(item.id))

    @staticmethod
    def _detect_category(item: WorkItemInfo) -> TaskCategory:
        title = item.title.upper()
        tags = " ".join(item.tags).upper()
        item_type = item.work_item_type.upper()

        if item_type == "BUG":
            return TaskCategory.BUG

        if _has_prefix(title, "[BE]") or _has_tag(tags, "BE") or _has_tag(tags, "BACKEND"):
            return TaskCategory.BACKEND_TASK
        if _has_prefix(title, "[FE]") or _has_tag(tags, "FE") or _has_tag(tags, "FRONTEND"):
            return TaskCategory.FRONTEND_TASK
        if _has_prefix(title, "[DB]") or _has_tag(tags, "DB") or _has_tag(tags, "DATABASE"):
            return TaskCategory.DATABASE_TASK
        if (
            _has_prefix(title, "[QC]")
            or _has_prefix(title, "[TEST]")
            or _has_tag(tags, "QC")
            or _has_tag(tags, "TEST")
        ):
            return TaskCategory.TEST_TASK

        if item_type in ("USER STORY", "REQUIREMENT", "FEATURE"):
            return TaskCategory.REQUIREMENT

        if item_type == "TASK":
            if any(k in title for k in _BACKEND_KW):
                return TaskCategory.BACKEND_TASK
            if any(k in title for k in _FRONTEND_KW):
                return TaskCategory.FRONTEND_TASK

        return TaskCategory.UNKNOWN


def _has_prefix(title: str, prefix: str) -> bool:
    return title.startswith(prefix)


def _has_tag(tags: str, tag: str) -> bool:
    return re.search(rf"\b{re.escape(tag)}\b", tags) is not None
