"""Records ADO state transitions so the Delivery page can measure time.

Everything on the Delivery page that involves *duration* — lead time, cycle time, the
cumulative-flow chart, "stuck for 3 days" — needs to know when a work item entered its
current state. Azure DevOps does not hand that over cheaply: ``System.ChangedDate`` is
bumped by any edit at all (a comment, a tag), and the revisions API that does hold the
truth costs one request per work item.

So this service watches instead. Once a cycle it reads the work items in the polled
projects and asks the repository to record anything whose state differs from the last
recorded one. A cycle is two requests total regardless of item count, and a cycle where
nothing moved writes nothing.

The deliberate consequence: **history begins when this service first runs.** The page
reports ``history_since`` rather than pretending an empty chart means a quiet week.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger


class DeliveryTrackerService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.delivery_tracker")
        self._task: asyncio.Task | None = None
        self._last_prune: datetime | None = None

    def start(self) -> None:
        if not self._config.delivery_history_enabled:
            self._log.info("delivery history recording disabled")
            return
        if not self._config.has_auth:
            self._log.info("delivery history: no ADO auth — not recording")
            return
        self._task = asyncio.create_task(self._run(), name="delivery-tracker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        interval = max(1, self._config.delivery_history_interval_minutes) * 60
        self._log.info(
            "delivery tracker started — recording work-item state transitions",
            interval_minutes=self._config.delivery_history_interval_minutes,
        )
        # Record once immediately: the baseline snapshot is what every later transition
        # is measured against, so waiting a full interval for it only delays the point
        # at which the page becomes useful.
        while True:
            try:
                await self.record_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a bad cycle must not kill the loop
                self._log.error("delivery tracker cycle failed", error=str(exc))
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def record_once(self) -> int:
        """One recording cycle. Returns how many transitions were written."""
        c = self._c
        items = await c.ado.get_all_active_work_items(top=self._config.delivery_max_items)
        if not items:
            return 0
        # Best-effort: an unreachable type/state map costs the category on THIS cycle's
        # rows, not the rows themselves — a missing transition would leave a permanent
        # hole in the timeline, a missing category only weakens one chart.
        try:
            categories = await c.ado.get_state_categories()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("delivery tracker: state categories failed", error=str(exc))
            categories = {}
        written = await c.state_history.record(items, categories)
        if written:
            self._log.info("recorded work-item state transitions", count=written)
        await self._maybe_prune()
        return written

    async def _maybe_prune(self) -> None:
        """Drop history past the retention window, at most once a day.

        Retention is generous (months) because the table is tiny — one short row per
        actual transition — and because shortening it silently shortens how far back
        any trend on the page can look."""
        days = self._config.delivery_history_retention_days
        if days <= 0:
            return
        now = datetime.now(UTC)
        if self._last_prune and (now - self._last_prune) < timedelta(days=1):
            return
        self._last_prune = now
        with contextlib.suppress(Exception):
            removed = await self._c.state_history.prune(now - timedelta(days=days))
            if removed:
                self._log.info("pruned old state history", removed=removed, keep_days=days)
