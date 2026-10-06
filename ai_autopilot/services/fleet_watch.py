"""Central side of fleet mode: notice the machines that stopped calling in.

The Fleet page has always shown a silent machine in red — to whoever happened to open
it. A worker that died at 2 a.m. was found at 10 a.m. when somebody wondered why the
board had not moved. This service turns that silence into a notice, once per episode,
and closes commands that nobody is going to answer.

Nothing here may take the process down, and nothing here calls a worker: the central
still only ever learns about a machine from the machine itself.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from ai_autopilot import fleet
from ai_autopilot.container import Container
from ai_autopilot.logging_config import describe_exc, get_logger

_TICK_SECONDS = 60


class FleetWatchService:
    def __init__(self, container: Container) -> None:
        self._c = container
        self._config = container.config
        self._log = get_logger("services.fleet_watch")
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="fleet-watch")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a watcher must outlive any one tick
                self._log.warning("fleet watch tick failed", error=describe_exc(exc))

    async def tick(self, now: datetime | None = None) -> list[str]:
        """One pass. Returns the names announced as offline (for tests and the log)."""
        cfg = self._config
        now = now or datetime.now(UTC)
        commands = getattr(self._c, "fleet_command_repo", None)
        if commands is not None:
            expired = await commands.expire(cfg.fleet_command_expire_minutes, now=now)
            if expired:
                self._log.info("fleet commands expired", count=expired)
        if not cfg.fleet_alert_offline:
            return []
        limit = max(1, int(cfg.fleet_offline_after_minutes)) * 60
        announced: list[str] = []
        for row in await self._c.fleet_repo.list_all():
            if row.offline_alerted:
                continue
            seen = row.last_seen if row.last_seen.tzinfo else row.last_seen.replace(tzinfo=UTC)
            quiet = (now - seen).total_seconds()
            if quiet <= limit:
                continue
            if not await self._c.fleet_repo.mark_offline_alerted(row.name):
                continue        # another tick claimed it first
            minutes = int(quiet // 60)
            await fleet.announce(
                self._c, f"🔴 Máy trạm {row.name} offline",
                f"{row.name} (v{row.version or '?'}) không gọi về trung tâm {minutes} phút. "
                "Kiểm tra máy còn bật, còn mạng, tiến trình autopilot còn chạy.",
                warning=True,
            )
            self._log.warning("fleet worker offline", worker=row.name, quiet_minutes=minutes)
            announced.append(row.name)
        return announced
