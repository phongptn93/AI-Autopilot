"""Decompose Requirement work items into child tasks (ported from ``RequirementDecomposer``)."""

from __future__ import annotations

from ai_autopilot.ado.client import AdoClient
from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import WorkItemInfo


class RequirementDecomposer:
    def __init__(self, ado: AdoClient, config: Settings) -> None:
        self._ado = ado
        self._config = config
        self._log = get_logger("routing.decomposer")

    async def decompose(self, parent: WorkItemInfo) -> list[int]:
        if not self._config.auto_decompose:
            self._log.debug("auto-decompose disabled", id=parent.id)
            return []

        self._log.info("decomposing requirement", id=parent.id, title=parent.title)
        created: list[int] = []
        for title, item_type in self._child_tasks(parent):
            new_id = await self._ado.create_work_item(
                title, item_type, parent.id, self._config.trigger_tag
            )
            if new_id > 0:
                created.append(new_id)
                self._log.info("created child", child_id=new_id, title=title)

        if created:
            links = ", ".join(f"#{i}" for i in created)
            await self._ado.add_comment(
                parent.id,
                f"<b>📋 Decomposed into {len(created)} child tasks:</b> {links}",
            )
        return created

    @staticmethod
    def _child_tasks(parent: WorkItemInfo) -> list[tuple[str, str]]:
        title = parent.title
        for marker in ("[BE]", "[FE]", "[DB]"):
            title = title.replace(marker, "")
        title = title.strip()
        return [
            (f"[BE] Implement {title} API", "Task"),
            (f"[FE] Implement {title} UI", "Task"),
            (f"[DB] Migration for {title}", "Task"),
            (f"[QC] Test cases for {title}", "Task"),
        ]
