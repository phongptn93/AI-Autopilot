"""Auto state transitions: merged PR → work-item state, deploy, and parent roll-up.

Opt-in (``auto_transition_enabled``). All three are best-effort and idempotent:

- **Merge → state**: when a PR the autopilot opened is completed (merged), move
  its work item to the merge state and mark it done.
- **Deploy → state**: a fresh successful deploy build advances the items sitting in
  their merge state.
- **Parent roll-up**: the parent follows its least-advanced child, per the
  ``"Child = Parent"`` map.

Every target state is resolved **per work-item type** through ``ai_autopilot.flows``
(falling back to the flat settings), because an ADO state belongs to a type: setting
``Ready to Deploy`` on a Requirement is rejected outright. A rejected transition now
stops the rest of its outcome — tagging an item done and commenting "marked it done"
when the state never moved left the board lying and the poller skipping the item
forever.

Only touches work items carrying a trigger tag, and never writes in ``dry_run``.
"""

from __future__ import annotations

import asyncio
import contextlib

from ai_autopilot.container import Container
from ai_autopilot.flows import (
    resolve_rollup,
    resolve_state,
    resolve_tag,
    rollup_entries,
    should_comment,
    stage_configured,
)
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.pr_feedback import is_bot_branch, parse_work_item_id

_POLL_INTERVAL_SECONDS = 90


def items_awaiting_deploy(tagged: list[WorkItemInfo], cfg: object) -> list[int]:
    """Ids of tagged items sitting in their own merge state (merged, awaiting deploy).

    Resolved per item: with per-type flows the "merged" state differs by work-item type
    (``Ready to Deploy`` for a Task, ``Implement Done`` for a Requirement), so a single
    state comparison would only ever find one type's items.
    """
    out: list[int] = []
    for item in tagged:
        target = resolve_state(cfg, "on_merge", item.work_item_type).strip().lower()
        if target and (item.state or "").strip().lower() == target:
            out.append(item.id)
    return out


def already_at_or_past_merge(state: str, cfg: object, work_item_type: str = "") -> bool:
    """True if an item is already at or past the merge state — i.e. its state is this
    type's merge state, its deploy state, or any ``done_states`` entry.

    The merge→state transition must never pull such an item BACKWARD. A merged PR
    stays ``completed`` forever, so without this guard a restart (which clears the
    in-memory dedup) re-applies the merge state to items that had already advanced
    (e.g. an item at "Ready to Testing" yanked back to "Ready to Deploy")."""
    s = (state or "").strip().lower()
    if not s:
        return False
    advanced = {
        resolve_state(cfg, "on_merge", work_item_type),
        resolve_state(cfg, "on_deploy", work_item_type),
        *(getattr(cfg, "done_states", None) or []),
    }
    return s in {x.strip().lower() for x in advanced if x and x.strip()}


def parse_rollup_map(entries: list[str]) -> list[tuple[str, str]]:
    """Parse ``["Child = Parent", ...]`` into ordered (child_state, parent_state)
    pairs. An entry without ``=``/``:`` maps a state to itself (same name)."""
    pairs: list[tuple[str, str]] = []
    for entry in entries:
        sep = "=" if "=" in entry else (":" if ":" in entry else "")
        child, parent = (entry.split(sep, 1) if sep else (entry, entry))
        child, parent = child.strip(), parent.strip()
        if child and parent:
            pairs.append((child, parent))
    return pairs


def unmapped_child_states(
    children: list[WorkItemInfo], pairs: list[tuple[str, str]]
) -> list[str]:
    """Child states the roll-up map doesn't cover — the reason a roll-up does nothing.

    A single missing line silently disables roll-up for the whole parent (see
    ``parent_rollup_target``), and until this was surfaced there was no way to tell an
    incomplete map from a parent that was simply up to date.
    """
    known = {child.strip().lower() for child, _ in pairs}
    return sorted({
        (c.state or "").strip() for c in children
        if (c.state or "").strip() and (c.state or "").strip().lower() not in known
    })


def parent_rollup_target(
    children: list[WorkItemInfo], pairs: list[tuple[str, str]]
) -> str | None:
    """Parent's target state = the parent-state mapped from the LEAST-advanced child.

    ``pairs`` is the ordered child→parent progression (e.g. Active→Active,
    Ready to Testing→Implement Done). Returns None if there are no children/pairs, or
    any child is in a child-state outside the map (then we don't touch the parent) —
    a child whose stage is unknown could be the slowest, so advancing the parent past
    it would be a guess. ``unmapped_child_states`` reports which state was missing.
    """
    if not children or not pairs:
        return None
    order = {child.strip().lower(): (i, parent) for i, (child, parent) in enumerate(pairs)}
    best: tuple[int, str] | None = None  # (stage index, parent state) of the slowest child
    for child in children:
        entry = order.get((child.state or "").strip().lower())
        if entry is None:
            return None  # a child outside the map → leave the parent alone
        if best is None or entry[0] < best[0]:
            best = entry
    return best[1] if best else None


class StateSyncService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.state_sync")
        self._merged: set[int] = set()   # PR ids already transitioned
        self._parent_targets: dict[int, str] = {}  # parent id → last state we rolled it to
        self._last_deploy_build: int | None = None  # newest deploy build id seen
        self._task: asyncio.Task | None = None
        # Persisted dedup (survives restarts). Optional so tests can omit it.
        self._sync_repo = getattr(c, "sync_repo", None)

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
        # Restore the dedup from disk so a restart doesn't re-transition merged PRs.
        if self._sync_repo is not None:
            with contextlib.suppress(Exception):
                self._merged = await self._sync_repo.seen_merged_prs()
                self._log.info("state-sync: restored merged-PR memory", count=len(self._merged))
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
        # Gated on the flat map OR any flow's own lines — an installation that configured
        # roll-up only per type must not find the whole sweep switched off.
        if cfg.parent_rollup_map or rollup_entries(cfg):
            try:
                tagged = await c.ado.get_all_tagged_work_items()
            except Exception as exc:  # noqa: BLE001
                self._log.warning("state-sync: fetch tagged failed", error=str(exc))
                tagged = []
            for parent_id in {i.parent_id for i in tagged if i.parent_id}:
                await self._maybe_roll_up_parent(parent_id)
        # C. deploy: a fresh successful deploy build → items awaiting deploy are deployed.
        if stage_configured(cfg, "on_deploy") and stage_configured(cfg, "on_merge"):
            await self._check_deploys()
        # D. bound the merged-PR memory (in-RAM set + persisted table) so neither
        # grows unbounded over the project's life. ADO only re-surfaces recent
        # completed PRs, so keeping the newest _MERGED_CAP ids is safe.
        await self._bound_merged()

    _MERGED_CAP = 5000

    async def _bound_merged(self) -> None:
        if len(self._merged) > self._MERGED_CAP:
            self._merged = set(sorted(self._merged)[-self._MERGED_CAP:])
        with contextlib.suppress(Exception):  # best-effort — never block the loop
            await self._sync_repo.prune_merged_prs(self._MERGED_CAP)

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
        awaiting = set(items_awaiting_deploy(tagged, cfg))
        for item in tagged:
            if item.id not in awaiting or not self._assignee_ok(item):
                continue
            target = resolve_state(cfg, "on_deploy", item.work_item_type)
            if not target:
                continue  # this type's flow doesn't track deploys
            if cfg.dry_run:
                self._log.info("[DRY-RUN] would mark deployed", id=item.id, state=target)
                continue
            if not await c.ado.update_state(item.id, target):
                # Same reasoning as the merge path: don't comment "Deployed" on an item
                # that is still sitting in the merge state.
                self._log.error("deploy transition failed", id=item.id, state=target,
                                type=item.work_item_type, build=newest)
                continue
            if should_comment(cfg, "on_deploy", item.work_item_type):
                await c.ado.add_comment(
                    item.id, "<div><b>🚀 Deployed</b> — deploy pipeline succeeded.</div>"
                )
            self._log.info("marked deployed", id=item.id, state=target, build=newest)

    def _has_trigger_tag(self, item: WorkItemInfo) -> bool:
        item_tags = {t.lower() for t in item.tags}
        return any(t.lower() in item_tags for t in self._config.effective_trigger_tags)

    def _assignee_ok(self, item: WorkItemInfo) -> bool:
        """The auto-transition assignee gate: item must be assigned to the
        configured person — matched against the display name OR the email/uniqueName
        (substring, case-insensitive). Blank config → any assignee."""
        who = (self._config.auto_transition_assignee or "").strip().lower()
        if not who:
            return True
        return who in (item.assigned_to or "").lower() or who in (item.assigned_to_email or "").lower()

    async def _handle_merged_pr(self, pr: dict) -> None:
        c, cfg = self._c, self._config
        pr_id = pr.get("pullRequestId")
        source = pr.get("sourceRefName", "")
        if pr_id is None or pr_id in self._merged:
            return
        if not is_bot_branch(source, tuple(cfg.bot_branch_prefixes)):
            return
        work_item_id = parse_work_item_id(source)
        if work_item_id is None:
            return
        item = await c.ado.get_work_item(work_item_id)
        if item is None or not self._has_trigger_tag(item) or not self._assignee_ok(item):
            return
        # Never pull an item BACKWARD: if it's already at/after the merge state
        # (merged / deployed / done), just remember the PR and leave it alone. This
        # keeps the transition idempotent even when the in-memory dedup was lost.
        if already_at_or_past_merge(item.state, cfg, item.work_item_type):
            await self._remember(pr_id, work_item_id, item.state or "")
            return
        target = resolve_state(cfg, "on_merge", item.work_item_type)
        if cfg.dry_run:
            self._merged.add(pr_id)
            self._log.info("[DRY-RUN] would transition on merge", id=work_item_id, pr=pr_id,
                           state=target, type=item.work_item_type)
            return
        if target and not await c.ado.update_state(work_item_id, target):
            # The state move FAILED — almost always because `target` isn't a state of
            # this item's type. Stop here: tagging it done and commenting "autopilot
            # marked it done" on an item that never moved makes the board and the
            # comment lie, and the done tag then makes the poller skip it forever.
            # Not remembering the PR means a corrected flow gets a second chance.
            self._log.error(
                "merge transition failed — not marking done", id=work_item_id, pr=pr_id,
                state=target, type=item.work_item_type,
                hint="state must exist on this work-item type — check /dashboard/flow",
            )
            return
        await c.ado.add_tag(
            work_item_id, resolve_tag(cfg, "on_merge", item.work_item_type, cfg.processed_tag)
        )
        if should_comment(cfg, "on_merge", item.work_item_type):
            await c.ado.add_comment(
                work_item_id, "<div><b>🔀 PR merged</b> — autopilot marked it done.</div>"
            )
        await self._remember(pr_id, work_item_id, target)
        self._log.info("transitioned on merge", id=work_item_id, pr=pr_id, state=target,
                       type=item.work_item_type)

    async def _remember(self, pr_id: int, work_item_id: int, state: str) -> None:
        """Record a PR as handled — in memory and (best-effort) persisted."""
        self._merged.add(pr_id)
        if self._sync_repo is not None:
            with contextlib.suppress(Exception):
                await self._sync_repo.mark_merged_pr(pr_id, work_item_id, state)

    async def _maybe_roll_up_parent(self, parent_id: int) -> None:
        c, cfg = self._c, self._config
        children = await c.ado.get_children(parent_id)
        if not children:
            return
        # The parent is fetched BEFORE computing the target: roll-up lines live on the
        # parent's own flow (the state they set is the parent's), so its type is needed
        # to know which map applies.
        parent = await c.ado.get_work_item(parent_id)
        if parent is None or not self._assignee_ok(parent):
            return
        pairs = parse_rollup_map(resolve_rollup(cfg, parent.work_item_type))
        if not pairs:
            return
        # Parent = the parent-state mapped from its least-advanced child.
        target = parent_rollup_target(children, pairs)
        if target is None:
            # An unmapped child state stops the roll-up. Name it: an incomplete map was
            # indistinguishable from "parent already up to date", which is how a map with
            # one wrong line stayed broken indefinitely.
            missing = unmapped_child_states(children, pairs)
            if missing:
                self._log.info(
                    "parent roll-up held — child states not in the map", id=parent_id,
                    type=parent.work_item_type, states=missing,
                    hint="add a line per child state at /dashboard/flow",
                )
            return
        if self._parent_targets.get(parent_id) == target:
            return  # already rolled to this state
        self._parent_targets[parent_id] = target
        if (parent.state or "").strip().lower() == target.strip().lower():
            return  # already there
        if cfg.dry_run:
            self._log.info("[DRY-RUN] would roll up parent", id=parent_id, state=target)
            return
        if not await c.ado.update_state(parent_id, target):
            self._log.error("parent roll-up failed", id=parent_id, state=target,
                            type=parent.work_item_type)
            self._parent_targets.pop(parent_id, None)  # retry next cycle once fixed
            return
        if should_comment(cfg, "rollup", parent.work_item_type):
            await c.ado.add_comment(
                parent_id,
                f"<div><b>↔ Parent updated</b> — moved to <b>{target}</b> as its children "
                f"progressed.</div>",
            )
        self._log.info("rolled up parent", id=parent_id, state=target)
