"""Background poller (ported from ``AdoPollerService``).

Polls ADO for tagged work items, classifies/prioritises them, and drives each one
through the execute → notify → persist pipeline with retry/backoff, RBAC, schedule
windows, requirement decomposition, and the approval gate — all preserved from the
.NET version.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

from ai_autopilot import metrics
from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, TaskCategory, WorkItemInfo
from ai_autopilot.routing import sort_by_priority


class AdoPollerService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.poller")
        self._processed: dict[int, datetime] = {}
        self._gate = asyncio.Semaphore(c.config.max_concurrent)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ado-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        cfg = self._config
        self._log.info(
            "ADO Autopilot started",
            org=cfg.ado_organization,
            project=cfg.ado_project,
            trigger_tag=cfg.trigger_tag,
            poll_interval=cfg.poll_interval_seconds,
            dry_run=cfg.dry_run,
        )

        if not cfg.has_auth:
            self._log.warning(
                "no auth configured — offline mode (no ADO polling). "
                "Set AUTOPILOT_ADO_PAT or AUTOPILOT_OAUTH_APP_ID + AUTOPILOT_OAUTH_APP_SECRET"
            )
            return

        while True:
            try:
                await self._poll_and_process()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.error("poll cycle failed", error=str(exc))

            cutoff = datetime.now(UTC) - timedelta(hours=1)
            self._processed = {k: v for k, v in self._processed.items() if v >= cutoff}
            await asyncio.sleep(cfg.poll_interval_seconds)

    async def _poll_and_process(self) -> None:
        c, cfg = self._c, self._config

        if not c.schedule.is_within_window():
            self._log.debug("outside schedule window — skipping")
            return

        # Webhook-triggered items first.
        webhook_ids = c.webhook_queue.drain()
        if webhook_ids:
            self._log.info("processing webhook items", count=len(webhook_ids))
            for wi in await c.ado.get_work_items_by_ids(webhook_ids):
                if wi.id not in self._processed and not c.retry_policy.is_exhausted(wi.id):
                    asyncio.create_task(self._process(wi))

        self._log.debug("polling ADO for pending work items")
        items = await c.ado.get_pending_work_items()

        new_items = [
            i
            for i in items
            if i.id not in self._processed
            and not any(t.lower() == cfg.processed_tag.lower() for t in i.tags)
            and not any(t.lower() == cfg.review_tag.lower() for t in i.tags)
            and not c.retry_policy.is_backoff_active(i.id)
            and not c.retry_policy.is_exhausted(i.id)
        ]

        if not new_items:
            self._log.debug("no new work items found")
            return

        for item in new_items:
            c.router.classify(item)
        new_items = sort_by_priority(new_items)

        metrics.set_poll_items_found(len(new_items))
        self._log.info("found new work items", count=len(new_items))

        await asyncio.gather(*(self._process(item) for item in new_items))

    async def _process(self, item: WorkItemInfo) -> None:
        c, cfg = self._c, self._config
        async with self._gate:
            try:
                self._processed[item.id] = datetime.now(UTC)

                classified = c.router.classify(item)
                classified = await c.plugins.run_pre_processors(classified)
                self._log.info("processing", item=str(item), category=str(classified.category))

                skill = c.router.route(classified)
                if skill is None:
                    self._log.warning("no skill found", item=str(item))
                    return

                if not c.rbac.is_user_allowed(item):
                    self._log.warning("rbac denied user", id=item.id, user=item.created_by)
                    return
                if not c.rbac.is_skill_allowed(skill):
                    self._log.warning("rbac denied skill", skill=skill, id=item.id)
                    return

                self._log.info("routing to skill", id=item.id, skill=skill)

                if classified.category == TaskCategory.REQUIREMENT and cfg.auto_decompose:
                    await c.decomposer.decompose(classified)
                    await c.ado.add_tag(item.id, cfg.processed_tag)
                    self._log.info("decomposed into child tasks", id=item.id)
                    return

                await c.notifier.notify_started(item, skill)
                record_id = await c.execution_repo.start_execution(item, skill)

                result = await self._execute(item, skill)

                await c.execution_repo.complete_execution(record_id, result)
                if result.cost_tokens:
                    await c.cost_tracker.track(record_id, result.cost_tokens)
                    metrics.record_cost(result.cost_tokens)

                await self._handle_result(item, skill, result, classified)
                await c.plugins.run_post_processors(item, result)

                status = "SUCCESS" if result.success else "FAILED"
                metrics.record_task(status.lower(), str(classified.category), skill)
                metrics.record_duration(str(classified.category), result.duration_seconds)
                self._log.info(
                    "finished",
                    id=item.id,
                    status=status,
                    duration=round(result.duration_seconds, 1),
                    skill=skill,
                )
            except Exception as exc:  # noqa: BLE001
                self._log.error("error processing", id=item.id, error=str(exc))
                await c.notifier.notify_error(item, str(exc))

    async def _execute(self, item: WorkItemInfo, skill: str) -> ExecutionResult:
        cfg = self._config
        if cfg.dry_run:
            self._log.info("[DRY-RUN] would execute", skill=skill, id=item.id)
            result = ExecutionResult.ok(item.id, skill, "[DRY-RUN] Skipped execution")
            result.duration_seconds = 0.1
            return result
        return await self._c.executor.execute(item, skill, draft_pr=cfg.require_approval)

    async def _handle_result(
        self, item: WorkItemInfo, skill: str, result: ExecutionResult, classified: WorkItemInfo
    ) -> None:
        c, cfg = self._c, self._config
        if result.success:
            c.retry_policy.record_success(item.id)
            if cfg.require_approval and result.pr_url:
                await c.ado.add_tag(item.id, cfg.review_tag)
                await c.ado.add_comment(
                    item.id,
                    f'<b>🔍 PR created (draft)</b>, awaiting human review.<br/>'
                    f'PR: <a href="{result.pr_url}">{result.pr_url}</a>',
                )
                await c.notifier.notify_completed(item, result, mark_processed=False)
            else:
                await c.notifier.notify_completed(item, result, mark_processed=True)
        else:
            c.retry_policy.record_failure(item.id, result.error or "Unknown error")
            exhausted = c.retry_policy.is_exhausted(item.id)
            await c.notifier.notify_completed(item, result, mark_processed=exhausted)
            if exhausted:
                state = c.retry_policy.get_state(item.id)
                count = state.retry_count if state else cfg.max_retries
                self._log.error("gave up after retries", id=item.id, count=count)
                await c.ado.add_comment(
                    item.id,
                    f"<b>⛔ Autopilot gave up after {count} retries.</b> "
                    f"Last error: {result.error}",
                )
            else:
                self._processed.pop(item.id, None)
