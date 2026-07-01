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
from ai_autopilot.data import PipelineState
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, TaskCategory, WorkItemInfo
from ai_autopilot.routing import sort_by_priority


def outcome_policy(cfg: object, outcome: str) -> tuple[str, str]:
    """The single, configurable source of truth for tag + ADO state per outcome.

    Returns ``(tag_to_add, ado_state_to_set)`` — either may be blank (= skip). Edit
    the underlying fields in Settings ("Outcomes → tag + state").

    Outcomes:
      in_progress  – the autopilot starts working the item
      review       – a draft PR opened, awaiting human review
      done         – completed with a (real) PR
      report       – a plan was commented, no code change (report mode)
      needs_human  – the agent escalated and held the item
      failed       – gave up after exhausting retries
    """
    return {
        "in_progress": ("", cfg.state_in_progress),
        "review": (cfg.review_tag, cfg.state_in_review),
        "done": (cfg.processed_tag, cfg.resolved_state),
        "report": (cfg.processed_tag, cfg.state_report),
        "needs_human": (cfg.escalation_tag, cfg.state_needs_human),
        "failed": (cfg.failed_tag or cfg.processed_tag, cfg.state_failed),
    }.get(outcome, ("", ""))


class AdoPollerService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.poller")
        self._processed: dict[int, datetime] = {}
        # Interactive mode: live Remote-Control sessions awaiting their result.json.
        self._live: dict[int, int] = {}  # work_item_id → execution record id
        self._live_dirs: dict[int, str] = {}  # work_item_id → run dir (worktree scratch)
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

        # Resume: re-queue any runs left mid-flight by a previous (crashed) process.
        requeued = await self._c.state_repo.requeue_in_progress()
        orphaned = await self._c.execution_repo.fail_running()
        if requeued or orphaned:
            self._log.info("resumed interrupted runs", requeued=requeued, orphaned=orphaned)
        # Best-effort: clean worktree scratch dirs orphaned by a crash.
        with contextlib.suppress(Exception):
            await self._c.executor.prune_orphans()

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

        # Finalise any interactive sessions that have written their result.
        await self._finalize_live_sessions()

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

        skip_tags = {cfg.processed_tag.lower(), cfg.review_tag.lower(), cfg.escalation_tag.lower()}
        new_items = [
            i
            for i in items
            if i.id not in self._processed
            and not any(t.lower() in skip_tags for t in i.tags)
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

    def _matched_tag(self, item: WorkItemInfo) -> str | None:
        """The first effective trigger tag this item carries (for dashboard filtering)."""
        item_tags = {t.lower() for t in item.tags}
        for tag in self._config.effective_trigger_tags:
            if tag.lower() in item_tags:
                return tag
        return None

    async def _apply_outcome(self, item_id: int, outcome: str) -> None:
        """Apply the configured ADO tag + state for a pipeline outcome.

        Single place that mutates ADO tags/state — see ``outcome_policy``. Applies
        in every execution mode. Blank tag/state or ``dry_run`` → skipped.
        Best-effort: ADO-client failures are logged and never block the pipeline.
        """
        tag, state = outcome_policy(self._config, outcome)
        if self._config.dry_run:
            return
        if tag:
            await self._c.ado.add_tag(item_id, tag)
        if state:
            await self._c.ado.update_state(item_id, state)

    async def _process(self, item: WorkItemInfo) -> None:
        c, cfg = self._c, self._config
        async with self._gate:
            try:
                self._processed[item.id] = datetime.now(UTC)

                classified = c.router.classify(item)
                classified = await c.plugins.run_pre_processors(classified)
                self._log.info("processing", item=str(item), category=str(classified.category))

                if not c.rbac.is_user_allowed(item):
                    self._log.warning("rbac denied user", id=item.id, user=item.created_by)
                    return

                # AI-native: hand the item to the agent and let it reason end-to-end.
                # Legacy (hardcoded classify→skill→git): only when no workspace is set.
                if cfg.workspace_directory:
                    await self._process_agent(item, classified)
                else:
                    await self._process_legacy(item, classified)
            except Exception as exc:  # noqa: BLE001
                self._log.error("error processing", id=item.id, error=str(exc))
                await c.notifier.notify_error(item, str(exc))

    async def _process_agent(self, item: WorkItemInfo, classified: WorkItemInfo) -> None:
        """Control plane around the agent: track, run, read structured result, react."""
        c, cfg = self._c, self._config

        # Interactive mode: launch a Remote-Control session and let the human steer.
        if cfg.execution_mode == "interactive":
            if len(self._live) >= cfg.max_concurrent:
                self._processed.pop(item.id, None)  # at capacity — retry next cycle
                return
            await self._dispatch_interactive(item)
            return

        await c.state_repo.set(item.id, PipelineState.IN_PROGRESS, title=item.title)
        await self._apply_outcome(item.id, "in_progress")
        await c.notifier.notify_started(item, "agent")
        record_id = await c.execution_repo.start_execution(
            item, "agent", trigger_tag=self._matched_tag(item)
        )

        result = await c.executor.run_agent(
            item, autonomy=cfg.autonomy_level, draft_pr=cfg.pr_is_draft
        )

        await c.execution_repo.complete_execution(record_id, result)
        if result.cost_tokens:
            await c.cost_tracker.track(record_id, result.cost_tokens)
            metrics.record_cost(result.cost_tokens)

        await self._handle_agent_result(item, result)
        await c.plugins.run_post_processors(item, result)

        status = (
            "NEEDS_HUMAN" if result.needs_human else ("SUCCESS" if result.success else "FAILED")
        )
        metrics.record_task(status.lower(), str(classified.category), "agent")
        metrics.record_duration(str(classified.category), result.duration_seconds)
        self._log.info(
            "finished", id=item.id, status=status, duration=round(result.duration_seconds, 1)
        )

    async def _dispatch_interactive(self, item: WorkItemInfo) -> None:
        """Launch a Remote-Control session for the item; finalise later from its result."""
        c, cfg = self._c, self._config
        launched, session, run_dir = await c.executor.dispatch_interactive(
            item, autonomy=cfg.autonomy_level, draft_pr=cfg.pr_is_draft
        )
        if not launched:
            await self._handle_agent_result(
                item, ExecutionResult.fail(item.id, "interactive", "failed to launch session")
            )
            return
        self._live_dirs[item.id] = run_dir
        await c.state_repo.set(
            item.id, PipelineState.IN_PROGRESS, title=item.title, detail=f"live session: {session}"
        )
        await self._apply_outcome(item.id, "in_progress")
        record_id = await c.execution_repo.start_execution(
            item, f"interactive:{session}", trigger_tag=self._matched_tag(item)
        )
        self._live[item.id] = record_id
        await c.ado.add_comment(
            item.id,
            "<div><b>🎮 Live session started</b><br/>Remote Control enabled — open claude.ai "
            f"and attach to session <code>{session}</code> to watch or steer it.</div>",
        )
        self._log.info("interactive session dispatched", id=item.id, session=session)

    async def _finalize_live_sessions(self) -> None:
        """Finalise interactive sessions whose result.json has appeared."""
        c, cfg = self._c, self._config
        for item_id, record_id in list(self._live.items()):
            run_dir = self._live_dirs.get(item_id, cfg.workspace_directory)
            item = await c.ado.get_work_item(item_id)
            if item is None:
                self._live.pop(item_id, None)
                await c.executor.release_scratch(self._live_dirs.pop(item_id, None))
                continue
            result = c.executor.finalize_interactive(item, run_dir)
            if result is None:
                continue  # session still running
            await c.execution_repo.complete_execution(record_id, result)
            if result.cost_tokens:
                await c.cost_tracker.track(record_id, result.cost_tokens)
                metrics.record_cost(result.cost_tokens)
            await self._handle_agent_result(item, result)
            self._processed[item_id] = datetime.now(UTC)
            self._live.pop(item_id, None)
            await c.executor.release_scratch(self._live_dirs.pop(item_id, None))
            self._log.info(
                "interactive session finalized",
                id=item_id,
                status="SUCCESS" if result.success else "FAILED",
            )

    async def _handle_agent_result(self, item: WorkItemInfo, result: ExecutionResult) -> None:
        c, cfg = self._c, self._config
        if result.needs_human:
            c.retry_policy.record_success(item.id)  # escalated — not a retryable failure
            await c.state_repo.set(item.id, PipelineState.NEEDS_HUMAN, detail=result.error or "")
            # Hold the item (tag) + set state so the poller skips it until a human steps in.
            await self._apply_outcome(item.id, "needs_human")
            await c.ado.add_comment(
                item.id,
                "<div><b>🙋 ADO Autopilot — Needs human input</b><br/>"
                f"<p>{result.error or result.output}</p></div>",
            )
            self._log.info("escalated to human", id=item.id)
            return

        if result.success:
            c.retry_policy.record_success(item.id)
            if result.pr_url and cfg.pr_is_draft:
                await c.state_repo.set(item.id, PipelineState.IN_REVIEW, pr_url=result.pr_url)
                await self._apply_outcome(item.id, "review")
                await c.ado.add_comment(
                    item.id,
                    "<div><b>🔍 PR created (draft)</b>, awaiting human review.<br/>"
                    f'PR: <a href="{result.pr_url}">{result.pr_url}</a></div>',
                )
                await c.notifier.notify_completed(item, result)
            elif result.pr_url:
                await c.state_repo.set(item.id, PipelineState.DONE, pr_url=result.pr_url)
                await self._apply_outcome(item.id, "done")
                await c.notifier.notify_completed(item, result)
            else:
                # report mode: the agent commented a plan, no PR.
                await c.state_repo.set(item.id, PipelineState.DONE)
                await self._apply_outcome(item.id, "report")
                await c.notifier.notify_completed(item, result)
            return

        c.retry_policy.record_failure(item.id, result.error or "Unknown error")
        await c.state_repo.set(item.id, PipelineState.FAILED, detail=result.error or "")
        exhausted = c.retry_policy.is_exhausted(item.id)
        await c.notifier.notify_completed(item, result)
        if exhausted:
            await self._apply_outcome(item.id, "failed")
            state = c.retry_policy.get_state(item.id)
            count = state.retry_count if state else cfg.max_retries
            self._log.error("gave up after retries", id=item.id, count=count)
            await c.ado.add_comment(
                item.id,
                f"<b>⛔ Autopilot gave up after {count} retries.</b> Last error: {result.error}",
            )
        else:
            self._processed.pop(item.id, None)

    async def _process_legacy(self, item: WorkItemInfo, classified: WorkItemInfo) -> None:
        """Legacy hardcoded flow (classify → fixed skill → Python git/PR)."""
        c, cfg = self._c, self._config
        skill = c.router.route(classified)
        if skill is None:
            self._log.warning("no skill found", item=str(item))
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

        # L1 (report): triage only — comment the plan, don't change code.
        if cfg.report_only:
            self._log.info("report-only triage", id=item.id, skill=skill)
            await c.ado.add_comment(
                item.id,
                "<b>🧭 ADO Autopilot — Triage (report mode)</b><br/>"
                f"Would route to <code>{skill}</code> "
                f"(category: {classified.category}).",
            )
            await c.ado.add_tag(item.id, cfg.processed_tag)
            return

        await c.notifier.notify_started(item, skill)
        await self._apply_outcome(item.id, "in_progress")
        record_id = await c.execution_repo.start_execution(
            item, skill, trigger_tag=self._matched_tag(item)
        )

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

    async def _execute(self, item: WorkItemInfo, skill: str) -> ExecutionResult:
        cfg = self._config
        if cfg.dry_run:
            self._log.info("[DRY-RUN] would execute", skill=skill, id=item.id)
            result = ExecutionResult.ok(item.id, skill, "[DRY-RUN] Skipped execution")
            result.duration_seconds = 0.1
            return result
        return await self._c.executor.execute(item, skill, draft_pr=cfg.pr_is_draft)

    async def _handle_result(
        self, item: WorkItemInfo, skill: str, result: ExecutionResult, classified: WorkItemInfo
    ) -> None:
        c, cfg = self._c, self._config
        if result.success:
            c.retry_policy.record_success(item.id)
            if cfg.pr_is_draft and result.pr_url:
                await self._apply_outcome(item.id, "review")
                await c.ado.add_comment(
                    item.id,
                    f'<b>🔍 PR created (draft)</b>, awaiting human review.<br/>'
                    f'PR: <a href="{result.pr_url}">{result.pr_url}</a>',
                )
                await c.notifier.notify_completed(item, result)
            else:
                await self._apply_outcome(item.id, "done" if result.pr_url else "report")
                await c.notifier.notify_completed(item, result)
        else:
            c.retry_policy.record_failure(item.id, result.error or "Unknown error")
            exhausted = c.retry_policy.is_exhausted(item.id)
            await c.notifier.notify_completed(item, result)
            if exhausted:
                await self._apply_outcome(item.id, "failed")
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
