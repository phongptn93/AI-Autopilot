"""PR babysitter (loop-engineering "PR Babysitter" pattern).

Polls open autopilot PRs for unresolved human review comments and, when found,
feeds them back to Claude to revise the existing branch (which updates the PR).
Bounded by ``max_revisions`` per work item to avoid runaway loops.
"""

from __future__ import annotations

import asyncio
import contextlib

from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger
from ai_autopilot.services.pr_feedback import (
    actionable_comments,
    is_bot_branch,
    parse_work_item_id,
)

_POLL_INTERVAL_SECONDS = 120
_BOT_BRANCH_PREFIXES = ("feature/", "fix/", "autopilot/")


class PrMonitorService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.pr_monitor")
        self._revision_counts: dict[int, int] = {}
        self._seen_comments: dict[int, int] = {}  # pr_id → comment count last handled
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
        self._log.info("PR babysitter started — watching open PRs for review feedback")
        while True:
            try:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.error("PR babysitter cycle failed", error=str(exc))

    async def _scan(self) -> None:
        c = self._c
        for repo in await c.ado.get_repositories():
            repo_id = repo.get("id")
            if not repo_id:
                continue
            for pr in await c.ado.get_active_pull_requests(repo_id):
                await self._inspect_pr(repo_id, pr)

    async def _inspect_pr(self, repo_id: str, pr: dict) -> None:
        c, cfg = self._c, self._config
        source_ref = pr.get("sourceRefName", "")
        if not is_bot_branch(source_ref, _BOT_BRANCH_PREFIXES):
            return

        pr_id = pr.get("pullRequestId")
        work_item_id = parse_work_item_id(source_ref)
        if pr_id is None or work_item_id is None:
            return

        if self._revision_counts.get(work_item_id, 0) >= cfg.max_revisions:
            return

        threads = await c.ado.get_pull_request_threads(repo_id, pr_id)
        comments = actionable_comments(threads)
        if len(comments) <= self._seen_comments.get(pr_id, 0):
            return  # nothing new since we last revised

        self._seen_comments[pr_id] = len(comments)
        item = await c.ado.get_work_item(work_item_id)
        if item is None:
            return

        revision = self._revision_counts.get(work_item_id, 0) + 1
        self._revision_counts[work_item_id] = revision
        branch = source_ref.removeprefix("refs/heads/")
        feedback = "\n".join(f"- {c}" for c in comments[-5:])

        self._log.info(
            "addressing PR feedback", id=work_item_id, pr=pr_id, revision=revision
        )
        result = await c.feedback.handle_feedback(item, branch, feedback, revision)
        if result.success:
            await c.ado.add_comment(
                work_item_id,
                f"<b>🔁 Autopilot addressed review feedback</b> (revision {revision}).",
            )
        else:
            await c.ado.add_comment(
                work_item_id,
                f"<b>⚠️ Autopilot could not address feedback</b> (revision {revision}): "
                f"{result.error}",
            )
