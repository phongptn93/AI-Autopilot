"""Auto state transitions: merged PR → work-item state, and parent roll-up.

Opt-in (``auto_transition_enabled``). Two automations, both best-effort and
idempotent:

- **Merge → state**: when a PR the autopilot opened is completed (merged), move
  its work item to ``on_merge_state`` and mark it done (``processed_tag``).
- **Parent roll-up**: when EVERY child of a parent is done, move the parent to
  ``parent_done_state``.

Only touches work items carrying a trigger tag, and never writes in ``dry_run``.
"""

from __future__ import annotations

import asyncio
import contextlib

from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.pr_feedback import is_bot_branch, parse_work_item_id

_POLL_INTERVAL_SECONDS = 90
_BOT_BRANCH_PREFIXES = ("feature/", "fix/", "autopilot/")


def items_awaiting_deploy(tagged: list[WorkItemInfo], on_merge_state: str) -> list[int]:
    """Ids of tagged items sitting in ``on_merge_state`` (merged, awaiting deploy)."""
    target = (on_merge_state or "").strip().lower()
    if not target:
        return []
    return [i.id for i in tagged if (i.state or "").strip().lower() == target]


def all_children_done(
    children: list[WorkItemInfo], done_states: list[str], processed_tag: str
) -> bool:
    """True if every child is done — its ADO state is in ``done_states`` or it
    carries ``processed_tag``. Empty list → False (nothing to roll up)."""
    if not children:
        return False
    done = {s.strip().lower() for s in done_states if s.strip()}
    ptag = (processed_tag or "").lower()
    for child in children:
        state = (child.state or "").strip().lower()
        tags = {t.lower() for t in child.tags}
        if state in done or (ptag and ptag in tags):
            continue
        return False
    return True


class StateSyncService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.state_sync")
        self._merged: set[int] = set()   # PR ids already transitioned
        self._rolled: set[int] = set()   # parent ids already rolled up
        self._last_deploy_build: int | None = None  # newest deploy build id seen
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._config.auto_transition_enabled:
            self._log.info("auto state transitions disabled")
            return
        self._task = asyncio.create_task(self._run(), name="state-sync")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        self._log.info("state-sync started — merged PRs → state, parent roll-up")
        while True:
            try:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.error("state-sync cycle failed", error=str(exc))

    async def _scan(self) -> None:
        c, cfg = self._c, self._config
        # A. merged PRs → transition the work item.
        for repo in await c.ado.get_repositories():
            repo_id = repo.get("id")
            if not repo_id:
                continue
            for pr in await c.ado.get_completed_pull_requests(repo_id):
                await self._handle_merged_pr(pr)
        # B. parent roll-up: sweep the parents of every tagged item.
        if cfg.parent_done_state:
            try:
                tagged = await c.ado.get_all_tagged_work_items()
            except Exception as exc:  # noqa: BLE001
                self._log.warning("state-sync: fetch tagged failed", error=str(exc))
                tagged = []
            for parent_id in {i.parent_id for i in tagged if i.parent_id}:
                await self._maybe_roll_up_parent(parent_id)
        # C. deploy: a fresh successful deploy build → items awaiting deploy are deployed.
        if cfg.on_deploy_state and cfg.on_merge_state:
            await self._check_deploys()

    async def _check_deploys(self) -> None:
        c, cfg = self._c, self._config
        branch = cfg.deploy_branch or cfg.base_branch
        builds = await c.ado.get_successful_builds(cfg.deploy_pipeline_id, branch)
        ids = [b.get("id") for b in builds if isinstance(b.get("id"), int)]
        if not ids:
            return
        newest = max(ids)
        if self._last_deploy_build is None:
            self._last_deploy_build = newest  # baseline on first scan — don't transition old builds
            return
        if newest <= self._last_deploy_build:
            return  # no new successful build since last check
        self._last_deploy_build = newest
        tagged = await c.ado.get_all_tagged_work_items()
        for wid in items_awaiting_deploy(tagged, cfg.on_merge_state):
            if cfg.dry_run:
                self._log.info("[DRY-RUN] would mark deployed", id=wid)
                continue
            await c.ado.update_state(wid, cfg.on_deploy_state)
            await c.ado.add_comment(
                wid, "<div><b>🚀 Deployed</b> — deploy pipeline succeeded.</div>"
            )
            self._log.info("marked deployed", id=wid, state=cfg.on_deploy_state, build=newest)

    def _has_trigger_tag(self, item: WorkItemInfo) -> bool:
        item_tags = {t.lower() for t in item.tags}
        return any(t.lower() in item_tags for t in self._config.effective_trigger_tags)

    async def _handle_merged_pr(self, pr: dict) -> None:
        c, cfg = self._c, self._config
        pr_id = pr.get("pullRequestId")
        source = pr.get("sourceRefName", "")
        if pr_id is None or pr_id in self._merged:
            return
        if not is_bot_branch(source, _BOT_BRANCH_PREFIXES):
            return
        work_item_id = parse_work_item_id(source)
        if work_item_id is None:
            return
        item = await c.ado.get_work_item(work_item_id)
        if item is None or not self._has_trigger_tag(item):
            return
        self._merged.add(pr_id)
        if cfg.dry_run:
            self._log.info("[DRY-RUN] would transition on merge", id=work_item_id, pr=pr_id)
            return
        if cfg.on_merge_state:
            await c.ado.update_state(work_item_id, cfg.on_merge_state)
        await c.ado.add_tag(work_item_id, cfg.processed_tag)
        await c.ado.add_comment(
            work_item_id, "<div><b>🔀 PR merged</b> — autopilot marked it done.</div>"
        )
        self._log.info("transitioned on merge", id=work_item_id, pr=pr_id, state=cfg.on_merge_state)

    async def _maybe_roll_up_parent(self, parent_id: int) -> None:
        c, cfg = self._c, self._config
        if parent_id in self._rolled:
            return
        children = await c.ado.get_children(parent_id)
        if not all_children_done(children, cfg.done_states, cfg.processed_tag):
            return
        parent = await c.ado.get_work_item(parent_id)
        if parent is None:
            return
        self._rolled.add(parent_id)
        if (parent.state or "").strip().lower() == cfg.parent_done_state.strip().lower():
            return  # already there
        if cfg.dry_run:
            self._log.info("[DRY-RUN] would roll up parent", id=parent_id)
            return
        await c.ado.update_state(parent_id, cfg.parent_done_state)
        await c.ado.add_comment(
            parent_id,
            "<div><b>✅ All child items done</b> — autopilot moved the parent forward.</div>",
        )
        self._log.info("rolled up parent", id=parent_id, state=cfg.parent_done_state)
