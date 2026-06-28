"""Plugin base classes (ported from ``IPlugin`` and friends)."""

from __future__ import annotations

from typing import Any

from ai_autopilot.models import ExecutionResult, WorkItemInfo


class Plugin:
    """Base class all plugins extend."""

    name: str = "plugin"
    version: str = "0.0.0"

    async def initialize(self, services: Any) -> None:  # noqa: B027 - optional hook
        """Called once at startup with the DI container."""


class PreProcessor(Plugin):
    """Runs before a work item is executed; may mutate/enrich it."""

    async def pre_process(self, item: WorkItemInfo) -> WorkItemInfo:
        return item


class PostProcessor(Plugin):
    """Runs after execution completes."""

    async def post_process(self, item: WorkItemInfo, result: ExecutionResult) -> None:
        ...


class SkillProvider(Plugin):
    """Provides a custom skill command for matching work items."""

    def can_handle(self, item: WorkItemInfo) -> bool:
        return False

    def get_skill_command(self, item: WorkItemInfo) -> str:
        raise NotImplementedError
