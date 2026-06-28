"""PR feedback-loop monitor (ported from ``PrMonitorService``).

Polls for work items in the review state and, when enabled, feeds reviewer
comments back to Claude for revision. The ADO PR-thread integration is a
deliberate extension point (parity with the .NET placeholder).
"""

from __future__ import annotations

import asyncio
import contextlib

from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger

_POLL_INTERVAL_SECONDS = 120


class PrMonitorService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.pr_monitor")
        self._revision_counts: dict[int, int] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._config.feedback_loop_enabled:
            self._log.info("PR feedback loop disabled")
            return
        self._task = asyncio.create_task(self._run(), name="pr-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        self._log.info("PR monitor started — polling for rejected PRs")
        c, cfg = self._c, self._config
        while True:
            try:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                items = await c.ado.get_pending_work_items()
                review_items = [
                    i
                    for i in items
                    if any(t.lower() == cfg.review_tag.lower() for t in i.tags)
                ]
                for item in review_items:
                    if self._revision_counts.get(item.id, 0) >= cfg.max_revisions:
                        continue
                    # Extension point: query ADO PR threads for "changes requested",
                    # then call container.feedback.handle_feedback(...). Left as a hook
                    # so the loop infrastructure is in place without a hard ADO Git dep.
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.error("PR monitor cycle failed", error=str(exc))
