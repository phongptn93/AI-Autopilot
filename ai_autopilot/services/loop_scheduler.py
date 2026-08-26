"""Scheduled autonomous loops (loop-engineering production patterns).

Runs configured skills on a cadence — e.g. a dependency sweeper, changelog
drafter, or CI sweeper — each opening a PR with any resulting changes. Backed by
APScheduler; cadence comes from a cron expression or a fixed interval.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ai_autopilot.config import ScheduledLoop
from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.notifications.base import NotificationMessage, NotificationType


class LoopScheduler:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.loop_scheduler")
        self._scheduler = AsyncIOScheduler()

    def start(self) -> None:
        loops = [loop for loop in self._config.scheduled_loops if loop.enabled]
        if not loops:
            self._log.info("no scheduled loops configured")
            return
        for loop in loops:
            trigger = _trigger(loop)
            if trigger is None:
                self._log.warning("scheduled loop has no valid cadence", name=loop.name)
                continue
            self._scheduler.add_job(
                self._run_loop,
                trigger,
                args=[loop],
                id=loop.name,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            self._log.info("scheduled loop registered", name=loop.name)
        self._scheduler.start()
        self._log.info("loop scheduler started", count=len(loops))

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def _run_loop(self, loop: ScheduledLoop) -> None:
        c, cfg = self._c, self._config
        # A loop bound to a project runs in THAT project's workspace, so its repo and
        # base branch default to the workspace's rather than the root config's.
        scoped = cfg.scoped_for_project(loop.project)
        repo = loop.repo_path or scoped.repo_working_directory
        base = loop.base_branch or scoped.base_branch
        if not repo:
            self._log.warning("scheduled loop has no repo configured", name=loop.name)
            return

        branch = _loop_branch(loop)
        self._log.info("running scheduled loop", name=loop.name, branch=branch)

        item = WorkItemInfo(id=0, title=f"[loop] {loop.name}")
        record_id = await c.execution_repo.start_execution(item, loop.prompt)
        result = await c.executor.run_loop(
            loop.name, loop.prompt, repo, base, branch, loop.draft_pr, loop.project
        )
        await c.execution_repo.complete_execution(record_id, result)
        if result.cost_tokens:
            await c.cost_tracker.track(record_id, result.cost_tokens)

        self._log.info(
            "scheduled loop finished",
            name=loop.name,
            success=result.success,
            pr=result.pr_url,
        )
        await self._notify(item, result)

    async def _notify(self, item: WorkItemInfo, result) -> None:
        # Through the notifier, not straight at the channels: a scheduled loop's result
        # is an ordinary "completed" notice and must obey the same event list, severity
        # floor and quiet window as one from the poller.
        with contextlib.suppress(Exception):
            await self._c.notifier.notify(NotificationMessage(
                work_item=item, type=NotificationType.COMPLETED, result=result
            ))


def _trigger(loop: ScheduledLoop):
    if loop.cron:
        try:
            return CronTrigger.from_crontab(loop.cron)
        except ValueError:
            return None
    if loop.interval_minutes > 0:
        return IntervalTrigger(minutes=loop.interval_minutes)
    return None


def _loop_branch(loop: ScheduledLoop) -> str:
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", loop.name.lower())).strip("-")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return f"{loop.branch_prefix}/{slug}-{stamp}"
