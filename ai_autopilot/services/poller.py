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
from ai_autopilot.config import find_bot_mention, match_command, matches_any_user
from ai_autopilot.container import Container
from ai_autopilot.data import PipelineState, QualityKind
from ai_autopilot.execution.feedback_handler import resolve_command
from ai_autopilot.execution.pr_scorer import RunScore, ScoreInput, score_badge_html, score_run
from ai_autopilot.execution.sdlc_plan import handoff_state, resolve_profile_name
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, TaskCategory, WorkItemInfo
from ai_autopilot.outcomes import apply_outcome
from ai_autopilot.routing import plan_schedule, sort_by_priority
from ai_autopilot.routing.planning_groups import group_by_links
from ai_autopilot.services.planning_analyzer import run_due_plans
from ai_autopilot.services.pr_feedback import parse_pr_url
from ai_autopilot.services.spec_guard import SpecGuard

# An ADO pull-request URL, e.g.
# https://dev.azure.com/org/Project/_git/Micro-Frontend/pullrequest/2470
# The repo NAME is captured on purpose: the git REST endpoints accept a name wherever
# they accept a repository id, so no extra lookup is needed to reach the PR.


def _scheduler_snapshot(items: list[WorkItemInfo], plan: object, at: datetime) -> dict:
    """A JSON-friendly snapshot of one scheduling decision for the Planning UI:
    the wave dispatched now (priority order) and the items held, each with a reason."""
    by_id = {i.id: i for i in items}

    def row(i: WorkItemInfo) -> dict:
        return {
            "id": i.id, "title": i.title, "category": str(i.category), "priority": i.priority,
            "predecessors": sorted(i.predecessor_ids), "related": sorted(i.related_ids),
        }

    deferred = []
    for iid, reason in plan.deferred.items():
        it = by_id.get(iid)
        deferred.append({"id": iid, "title": it.title if it else "", "reason": reason})
    return {
        "at": at,
        "candidates": len(items),
        "ready": [row(i) for i in plan.ready],
        "deferred": deferred,
    }


def _symmetrize(related: dict[int, set[int]]) -> dict[int, set[int]]:
    """Make a Related map undirected — if A links B, B also links A. ADO stores the
    link on both items, but symmetrising is cheap insurance for partial fetches."""
    out: dict[int, set[int]] = {k: set(v) for k, v in related.items()}
    for a, peers in related.items():
        for b in peers:
            out.setdefault(b, set()).add(a)
    return out


def _add_sibling_conflicts(items: list[WorkItemInfo], related: dict[int, set[int]]) -> None:
    """Heuristic: candidates sharing a Parent and repo-category likely touch the same
    code, so mark them mutually Related (soft conflict). Mutates ``related``."""
    groups: dict[tuple[int, object], list[int]] = {}
    for it in items:
        if it.parent_id is None:
            continue
        groups.setdefault((it.parent_id, it.category), []).append(it.id)
    for ids in groups.values():
        if len(ids) < 2:
            continue
        for a in ids:
            related.setdefault(a, set()).update(x for x in ids if x != a)


class AdoPollerService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.poller")
        # Post-run obligations: the PR names its work item, and decisions the agent had
        # to make on the team's behalf reach a human.
        self._spec_guard = SpecGuard(c)
        self._processed: dict[int, datetime] = {}
        # Interactive mode: live Remote-Control sessions awaiting their result.json.
        self._live: dict[int, int] = {}  # work_item_id → execution record id
        self._live_dirs: dict[int, str] = {}  # work_item_id → run dir (worktree scratch)
        # Dependency scheduling: ids we've already told the human are deferred, so we
        # comment "waiting for #X" once per episode rather than every poll cycle.
        self._deferred_notified: set[int] = set()
        # Batched runs: lead item id → the whole linked cluster it stands for this
        # cycle. Populated by _schedule, consumed (and cleared) by _dispatch.
        self._batch_of: dict[int, list[WorkItemInfo]] = {}
        # Comment-reaction loop: last human comment id handled per item (baseline), a
        # per-item round cap, comments to apply after an in-flight run finishes, and the
        # ids with a run currently in flight (so we defer rather than interrupt).
        self._comment_seen: dict[int, int] = {}
        self._comment_rounds: dict[int, int] = {}
        self._comment_capped: set[int] = set()   # items we've told the human hit the cap
        self._pending_comment: dict[int, str] = {}
        self._inflight: set[int] = set()
        self._gate = asyncio.Semaphore(c.config.max_concurrent)
        self._task: asyncio.Task | None = None
        self._comment_task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="ado-poller")
        self._comment_task = asyncio.create_task(self._comment_run(), name="ado-comment-loop")

    async def stop(self) -> None:
        for task in (self._task, self._comment_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _comment_run(self) -> None:
        """Dedicated loop for ``/command`` comments — its own fast cadence, decoupled from
        the heavier work-item poll so commands are picked up within
        ``comment_poll_interval_seconds`` regardless of ``poll_interval_seconds``."""
        cfg = self._config
        if not cfg.has_auth:
            return
        while True:
            try:
                await asyncio.sleep(cfg.comment_poll_interval_seconds or 15)
                await self._reconcile_human_replies()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.error("comment loop failed", error=str(exc))

    async def _run(self) -> None:
        cfg = self._config
        self._log.info(
            "ADO Autopilot started",
            org=cfg.ado_organization,
            projects=cfg.effective_ado_projects,
            workspaces=[w.label for w in cfg.effective_workspaces],
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
            # Bound the comment-reaction bookkeeping. Unlike _processed these have no
            # timestamp, and they only grow (one entry per distinct item ever seen),
            # so cap them as a memory backstop — dropping the oldest-inserted keys
            # once far past any realistic active-item count. Keeps a long-running
            # poller from leaking over weeks.
            self._bound_tracking()
            await asyncio.sleep(cfg.poll_interval_seconds)

    def _bound_tracking(self, cap: int = 5000) -> None:
        """Trim the per-item comment-loop caches to their most-recently-inserted
        ``cap`` keys (insertion order). A no-op until a cache exceeds the cap."""
        def trim_dict(d: dict) -> dict:
            return dict(list(d.items())[-cap:]) if len(d) > cap else d

        def trim_set(s: set) -> set:
            return set(list(s)[-cap:]) if len(s) > cap else s

        self._comment_seen = trim_dict(self._comment_seen)
        self._comment_rounds = trim_dict(self._comment_rounds)
        self._pending_comment = trim_dict(self._pending_comment)
        self._comment_capped = trim_set(self._comment_capped)
        self._deferred_notified = trim_set(self._deferred_notified)

    async def _poll_and_process(self) -> None:
        c, cfg = self._c, self._config

        # Finalise any interactive sessions that have written their result.
        await self._finalize_live_sessions()

        # Fire any Planning-workbench runs whose scheduled time has come.
        with contextlib.suppress(Exception):
            await run_due_plans(c)

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

        # Reopen items a human dragged back to a trigger state (clears skip tags),
        # so the pending query below picks them up again this cycle.
        await self._reconcile_reopened()

        # Force a clean re-run for items a human tagged for restart: wipe SDLC
        # progress and dispatch immediately (from any state) — see method docstring.
        await self._reconcile_restart_requests()

        self._log.debug("polling ADO for pending work items")
        items = await c.ado.get_pending_work_items()

        skip_tags = {
            cfg.processed_tag.lower(), cfg.review_tag.lower(),
            cfg.escalation_tag.lower(), cfg.live_tag.lower(),
        } - {""}
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

        ready = await self._schedule(new_items)

        metrics.set_poll_items_found(len(new_items))
        self._log.info(
            "found new work items", count=len(new_items), dispatching=len(ready)
        )

        await asyncio.gather(*(self._dispatch(item) for item in ready))

    async def _schedule(self, new_items: list[WorkItemInfo]) -> list[WorkItemInfo]:
        """Order the cycle's candidates by the ADO link graph (P1).

        Returns the items to dispatch *this* cycle. Deferred items are left
        un-processed so the next poll re-evaluates them — waves form across cycles.
        Falls back to plain priority order if scheduling is disabled or the link
        fetch fails (offline / bad PAT), so a graph hiccup never stalls the board.
        """
        c, cfg = self._c, self._config
        if not cfg.dependency_scheduling_enabled or not new_items:
            return sort_by_priority(new_items)

        candidate_ids = {i.id for i in new_items}
        try:
            preds, related = await c.ado.get_work_item_links(list(candidate_ids))
        except Exception as exc:  # noqa: BLE001 — never let scheduling break the poll
            self._log.warning("link-graph fetch failed — plain priority order", error=str(exc))
            return sort_by_priority(new_items)

        related = _symmetrize(related)
        if cfg.sibling_conflict_scheduling:
            _add_sibling_conflicts(new_items, related)
        # Feed AI-confirmed hidden conflicts (from Planning Analyze) back in as
        # Related soft-conflicts, so the autopilot avoids running them concurrently.
        if cfg.scheduler_use_ai_conflicts:
            try:
                ai_edges = await c.ai_conflict_repo.related_edges(
                    list(candidate_ids), cfg.scheduler_ai_conflict_min_score
                )
                for a, peers in ai_edges.items():
                    related.setdefault(a, set()).update(peers)
            except Exception as exc:  # noqa: BLE001 — never let this break the poll
                self._log.warning("ai-conflict edges fetch failed", error=str(exc))

        # Attach links to the items (handy for the dashboard) and work out which
        # predecessors are still open (block their successors).
        for item in new_items:
            item.predecessor_ids = sorted(preds.get(item.id, set()))
            item.related_ids = sorted(related.get(item.id, set()))

        # Batching (opt-in): collapse each linked cluster to a single LEAD, so the
        # scheduler reasons about the cluster as one unit — its predecessors and
        # conflicts are the union of its members', minus the edges inside it. The
        # lead is later dispatched as one batched run that opens a PR per member.
        new_items, preds, related = self._collapse_batches(new_items, preds, related)

        live_ids = set(self._live)
        active_ids = candidate_ids | live_ids
        # Predecessors that aren't candidates/in-flight: fetch their state; anything
        # not in a done state (or unfetchable) still blocks — safest for ordering.
        all_pred = set().union(*preds.values()) if preds else set()
        unknown = all_pred - candidate_ids - live_ids
        if unknown:
            done_states = {s.lower() for s in cfg.done_states}
            linked = await c.ado.get_work_items_by_ids(list(unknown))
            fetched = {wi.id: (wi.state or "") for wi in linked}
            active_ids |= {i for i, st in fetched.items() if st.lower() not in done_states}
            active_ids |= unknown - set(fetched)  # couldn't fetch → assume still open

        plan = plan_schedule(
            new_items,
            predecessors=preds,
            related=related,
            active_ids=active_ids,
            busy_ids=live_ids,
            max_dispatch=cfg.scheduler_max_dispatch or None,
        )

        # Publish the decision for the Planning dashboard (ready order + why deferred).
        snapshot = _scheduler_snapshot(new_items, plan, datetime.now(UTC))
        self._c.scheduler_view = snapshot
        # Persist a bounded history of *interesting* cycles (something was deferred)
        # so the dashboard shows the recent trend across restarts. Best-effort.
        if plan.deferred and cfg.scheduler_history_limit > 0:
            with contextlib.suppress(Exception):
                await self._c.scheduler_history_repo.record(
                    snapshot["at"], snapshot["candidates"],
                    [i.id for i in plan.ready], snapshot["deferred"],
                    keep=cfg.scheduler_history_limit,
                )

        # Tell the human (once per episode) why an item is waiting.
        for item in plan.ready:
            self._deferred_notified.discard(item.id)
        if plan.deferred:
            self._log.info("deferred by dependency scheduler", deferred=plan.deferred)
        for item_id, reason in plan.deferred.items():
            if item_id in self._deferred_notified or cfg.dry_run:
                continue
            self._deferred_notified.add(item_id)
            with contextlib.suppress(Exception):
                await c.ado.add_comment(item_id, f"<div>⏸️ <b>Autopilot hoãn:</b> {reason}.</div>")

        return plan.ready

    def _batching_allowed(self) -> bool:
        """Batching only applies to the headless agent path.

        Interactive mode gives the human ONE steerable session per item, and the
        SDLC loop drives one item through stages with its own per-item state — in
        both, a shared run has nowhere to report to.
        """
        cfg = self._config
        return bool(
            cfg.batch_related_enabled
            and cfg.batch_max_items > 1
            and cfg.workspace_directory      # legacy path is per-item by construction
            and cfg.execution_mode != "interactive"
            and not cfg.sdlc_loop_enabled
        )

    def _collapse_batches(
        self,
        items: list[WorkItemInfo],
        preds: dict[int, set[int]],
        related: dict[int, set[int]],
    ) -> tuple[list[WorkItemInfo], dict[int, set[int]], dict[int, set[int]]]:
        """Replace each linked cluster with its lead; record the cluster in ``_batch_of``.

        Returns the rewritten ``(items, predecessors, related)`` for ``plan_schedule``.
        Clusters that are too big to batch are left completely untouched, so they
        keep the existing one-item-per-wave behaviour.
        """
        self._batch_of = {}
        if not self._batching_allowed():
            return items, preds, related

        groups = group_by_links(items, preds, related)
        by_id = {i.id: i for i in items}
        cap = self._config.batch_max_items
        # Predecessor chains first: their ORDER is the stacking order. Related
        # clusters have no inherent order, so priority decides.
        clusters: list[list[int]] = []
        taken: set[int] = set()
        for chain in groups["serial"]:
            if 1 < len(chain) <= cap and not (set(chain) & taken):
                clusters.append(chain)
                taken.update(chain)
        for cluster in groups["conflicts"]:
            if 1 < len(cluster) <= cap and not (set(cluster) & taken):
                ordered = [i.id for i in sort_by_priority([by_id[x] for x in cluster])]
                clusters.append(ordered)
                taken.update(ordered)

        if not clusters:
            return items, preds, related

        out_items: list[WorkItemInfo] = []
        out_preds = {k: set(v) for k, v in preds.items()}
        out_related = {k: set(v) for k, v in related.items()}
        collapsed: set[int] = set()
        for cluster in clusters:
            members = [by_id[x] for x in cluster]
            lead = members[0]
            inside = set(cluster)
            # The lead inherits every link the cluster has to the OUTSIDE world, so a
            # batch still waits for an external predecessor and still conflicts with
            # an external Related item.
            out_preds[lead.id] = set().union(*(preds.get(x, set()) for x in cluster)) - inside
            out_related[lead.id] = set().union(*(related.get(x, set()) for x in cluster)) - inside
            self._batch_of[lead.id] = members
            collapsed |= inside - {lead.id}
            self._log.info(
                "batching linked cluster", lead=lead.id, members=cluster, size=len(cluster)
            )
        out_items = [i for i in items if i.id not in collapsed]
        return out_items, out_preds, out_related

    async def _dispatch(self, item: WorkItemInfo) -> None:
        """Run one ready unit: a batched cluster if this item leads one, else itself."""
        members = self._batch_of.pop(item.id, None)
        if members and len(members) > 1:
            await self._process_batch(members)
            return
        await self._process(item)

    def _matched_tag(self, item: WorkItemInfo) -> str | None:
        """The first effective trigger tag this item carries (for dashboard filtering)."""
        item_tags = {t.lower() for t in item.tags}
        for tag in self._config.effective_trigger_tags:
            if tag.lower() in item_tags:
                return tag
        return None

    async def _apply_outcome(self, item: WorkItemInfo, outcome: str) -> None:
        """Apply the configured ADO tag + state for a pipeline outcome — clearing any other
        outcome tag first so the board never shows a stale one (see ``apply_outcome``).
        Applies in every execution mode; blank tag/state or ``dry_run`` → skipped.

        Takes the ITEM rather than its id so the work-item type is always available to
        pick the per-type flow. An id-only signature let a call site silently fall back
        to the flat project-wide state, which is the bug this indirection prevents.

        Resolved against the item's OWN project: state names come from that project's
        process, so on an instance serving two projects the root vocabulary would be
        wrong for one of them (see ``Settings.scoped_for_project``).
        """
        await apply_outcome(
            self._c.ado,
            self._config.scoped_for_project(item.project),
            item.id, outcome, item.work_item_type,
        )

    async def _reconcile_reopened(self) -> None:
        """Reopen items a human dragged back to a trigger state: clear their skip
        tags so they re-enter the pipeline.

        Loop-safe: only acts on trigger states the autopilot never sets itself
        (its output states are excluded), and only on items carrying a skip tag.
        """
        c, cfg = self._c, self._config
        if cfg.dry_run or not cfg.reprocess_on_reopen:
            return
        output = {s.lower() for s in (
            cfg.state_in_progress, cfg.state_in_review, cfg.state_needs_human,
            cfg.resolved_state, cfg.state_report, cfg.state_failed,
        ) if s}
        reopen_states = {s.lower() for s in cfg.trigger_states} - output
        skip_tags = {t.lower() for t in (
            cfg.processed_tag, cfg.review_tag, cfg.escalation_tag, cfg.failed_tag,
        ) if t}
        if not reopen_states or not skip_tags:
            return
        try:
            tagged = await c.ado.get_all_tagged_work_items()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("reopen reconcile: fetch failed", error=str(exc))
            return
        for item in tagged:
            held = [t for t in item.tags if t.lower() in skip_tags]
            if (item.state or "").lower() not in reopen_states or not held:
                continue
            for tag in held:
                await c.ado.remove_tag(item.id, tag)
            await c.state_repo.set(item.id, PipelineState.QUEUED, title=item.title)
            # Record BEFORE clearing: record_success wipes the in-memory attempt count,
            # and a reopen is the strongest rework signal there is — a human looked at
            # the result and sent it back.
            await c.quality_repo.record(
                work_item_id=item.id, kind=QualityKind.REOPENED,
                actor="human", detail=f"reopened from state {item.state!r}; cleared {held}",
            )
            c.retry_policy.record_success(item.id)  # fresh retries on reopen
            self._processed.pop(item.id, None)
            await c.ado.add_comment(
                item.id,
                "<div><b>🔄 Reopened</b> — moved back to a trigger state; cleared "
                "autopilot tags and will reprocess.</div>",
            )
            self._log.info("reopened item", id=item.id, state=item.state, cleared=held)

    async def _reconcile_restart_requests(self) -> None:
        """Force a CLEAN re-run for items a human tagged with ``restart_tag``.

        Reopen (above) *resumes* an item mid-loop — right when a human fixed a
        blocker and wants it to carry on. Restart is the opposite intent: the run
        went wrong and the human wants it redone from scratch. So this wipes the
        item's saved SDLC progress (→ starts at stage 0), clears any skip tags,
        resets the retry budget, and dispatches it immediately — from ANY state
        (Done, in review, held...), not only trigger states. The fresh run reads the
        item's latest comments, which is where the human writes what to do differently.

        Loop-safe: the restart tag is removed before dispatch, and the item is marked
        processed for this cycle so the pending query below can't double-dispatch it.
        """
        c, cfg = self._c, self._config
        tag = cfg.restart_tag
        if cfg.dry_run or not tag:
            return
        try:
            tagged = await c.ado.get_all_tagged_work_items()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("restart reconcile: fetch failed", error=str(exc))
            return
        tag_l = tag.lower()
        skip_tags = {t.lower() for t in (
            cfg.processed_tag, cfg.review_tag, cfg.escalation_tag, cfg.failed_tag, cfg.live_tag,
        ) if t}
        for item in tagged:
            if tag_l not in {t.lower() for t in item.tags} or item.id in self._live:
                continue  # not requested, or a live session — don't yank it
            await c.ado.remove_tag(item.id, tag)
            for held in [t for t in item.tags if t.lower() in skip_tags]:
                await c.ado.remove_tag(item.id, held)
            # Wipe SDLC progress → a true restart, not a resume. Best-effort: a
            # missing/absent row (non-SDLC item) is fine.
            with contextlib.suppress(Exception):
                await c.sdlc_state_repo.clear(item.id)
            c.retry_policy.record_success(item.id)  # fresh retry budget
            await c.state_repo.set(item.id, PipelineState.QUEUED, title=item.title)
            self._processed[item.id] = datetime.now(UTC)  # block same-cycle re-pick
            await c.ado.add_comment(
                item.id,
                "<div><b>♻️ Restart requested</b> — cleared SDLC progress; reprocessing "
                "from scratch using your latest comments.</div>",
            )
            self._log.info("restart requested", id=item.id, state=item.state)
            asyncio.create_task(self._process(item))

    def _is_my_user(self, email: str | None, name: str | None) -> bool:
        """True if the identity is one THIS machine acts for — its owner, or anyone listed
        in ``command_users``. Blank owner and empty list = anyone.

        Ownership only (which items this stream picks up). For "may this person command
        me", call :meth:`_may_command` — that one honours ``commands_from_anyone``."""
        return matches_any_user(email, name, self._config.effective_command_users)

    def _may_command(self, email: str | None, name: str | None) -> bool:
        """True if the identity may issue /commands and @mentions to this machine.

        Same roster as :meth:`_is_my_user` unless ``commands_from_anyone`` is on, in which
        case everyone may command while ownership stays scoped to the roster."""
        return matches_any_user(email, name, self._config.command_allowlist)

    def _owns_item(self, item: WorkItemInfo) -> bool:
        """True if this machine's stream owns the item — mirrors the main poll's candidate
        rule: it carries one of this machine's trigger tags, OR the shared assignee-trigger
        tag AND is assigned to this machine's user."""
        cfg = self._config
        tags = {t.lower() for t in item.tags}
        if {t.lower() for t in cfg.effective_trigger_tags} & tags:
            return True
        atag = (cfg.assignee_trigger_tag or "").strip().lower()
        return bool(
            atag and atag in tags and self._is_my_user(item.assigned_to_email, item.assigned_to)
        )

    async def _reconcile_human_replies(self) -> None:
        """Pick up command comments on this machine's items and act on them.

        Trigger = an explicit ``comment_command`` (e.g. ``/ai fix the null check``) **or an
        @mention of the bot** — not any comment, so this stays intentional and light.

        The @mention half was missing: the same "@AI Autopilot xem lại chỗ này" that works
        on a pull request did nothing on a work item, because this path only ever called
        ``match_command`` (which requires a LEADING /command) and never received the bot
        identity. Mentions now go through ``resolve_command``, the shared helper whose whole
        purpose is that the advisory default for a bare mention can't be honoured in one
        caller and forgotten in another — this was the caller that forgot it.

        Scoping is multi-machine safe:
          • per-machine   — only items this machine's stream OWNS (``_owns_item``);
          • per-commenter — only commands from the user this machine acts for (``_is_my_user``);
          • idempotent    — the watermark is the id of the item's last BOT comment, so once
            the bot has replied a command is never re-run (durable across restarts);
          • bounded       — ``max_comment_rounds`` caps a runaway exchange (one-time notice);
          • cheap         — a cadence gate plus the ownership filter keep the fetch off the
            whole board.
        An in-flight run is never interrupted; its follow-up is deferred (see ``_process``).
        """
        c, cfg = self._c, self._config
        if cfg.dry_run or not cfg.comment_reprocess_enabled or not cfg.comment_commands:
            return
        try:
            tagged = await c.ado.get_all_tagged_work_items()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("comment reconcile: fetch failed", error=str(exc))
            return
        # None when comment_mention_enabled is off — the same switch the PR path uses, so
        # mentions are enabled or disabled everywhere at once.
        bot = await c.mention_identity()
        for item in tagged:
            if item.id in self._live or not self._owns_item(item):
                continue  # not this machine's stream — or a live session steers it directly
            try:
                comments = await c.ado.get_work_item_comments(item.id)
            except Exception as exc:  # noqa: BLE001
                self._log.warning(
                    "comment reconcile: comments fetch failed", id=item.id, error=str(exc)
                )
                continue
            # Watermark = last BOT comment id → a command is "handled" once the bot has
            # replied after it (durable across restarts). _comment_seen dedups per session.
            bot_ids = [cm["id"] for cm in comments if cm.get("is_bot")]
            baseline = self._comment_seen.get(item.id, max(bot_ids) if bot_ids else 0)
            # Unhandled commands from THIS machine's user, oldest→newest. A comment counts
            # when it names a /command OR @mentions the bot.
            cmds: list[tuple[int, str, bool]] = []
            for cm in comments:
                if (
                    cm["id"] <= baseline
                    or cm.get("is_bot")
                    or not self._may_command(cm.get("created_by_email"), cm.get("created_by"))
                ):
                    continue
                instr = match_command(cm["text"], cfg.comment_commands)
                via_mention = False
                if instr is None and bot is not None:
                    instr = find_bot_mention(cm["text"], bot)
                    via_mention = instr is not None
                if instr:
                    cmds.append((cm["id"], instr, via_mention))
            if not cmds:
                continue
            self._comment_seen[item.id] = cmds[-1][0]
            # A bare @mention carries no command, but everything downstream is keyed on
            # one — so infer it and fold it into the text, exactly as the PR path does.
            parts: list[str] = []
            for _, instr, via_mention in cmds:
                if via_mention:
                    cmd = {"instruction": instr, "via_mention": True}
                    advisory = await resolve_command(cfg, cmd)
                    instr = cmd["instruction"]
                    self._log.info("work-item @mention resolved", id=item.id,
                                   advisory=advisory, instruction=instr[:120])
                parts.append(instr)
            instruction = "\n\n".join(parts)
            rounds = self._comment_rounds.get(item.id, 0) + 1
            if rounds > cfg.max_comment_rounds:
                # Tell the human ONCE that we've stopped, and how to force a fresh run.
                if item.id not in self._comment_capped and cfg.restart_tag:
                    self._comment_capped.add(item.id)
                    with contextlib.suppress(Exception):
                        await c.ado.add_comment(
                            item.id,
                            f"<div>⏸️ <b>Autopilot đã đạt {cfg.max_comment_rounds} vòng xử lý "
                            f"lệnh</b> cho item này. Gắn tag <code>{cfg.restart_tag}</code> để "
                            "buộc chạy lại từ đầu.</div>",
                        )
                self._log.info("comment reconcile: max rounds reached", id=item.id, rounds=rounds)
                continue
            self._comment_rounds[item.id] = rounds
            if item.id in self._inflight:
                # Don't interrupt a run in flight — apply this once it completes.
                self._pending_comment[item.id] = instruction
                self._log.info("/ai command queued (item in flight)", id=item.id)
                continue
            await self._dispatch_with_comment(item, instruction)

    async def _dispatch_with_comment(self, item: WorkItemInfo, comment: str) -> None:
        """Re-enter an autopilot-owned item to act on a fresh human comment: clear its
        skip tags (keep the trigger tag → still owned), re-queue, and dispatch with the
        comment injected into the agent brief."""
        c, cfg = self._c, self._config
        skip_tags = {t.lower() for t in (
            cfg.processed_tag, cfg.review_tag, cfg.escalation_tag, cfg.failed_tag,
        ) if t}
        for held in [t for t in item.tags if t.lower() in skip_tags]:
            await c.ado.remove_tag(item.id, held)
        item.tags = [t for t in item.tags if t.lower() not in skip_tags]
        c.retry_policy.record_success(item.id)  # fresh retry budget for the follow-up
        await c.state_repo.set(item.id, PipelineState.QUEUED, title=item.title)
        # Mark processed for THIS cycle so the pending query later in _poll_and_process
        # can't ALSO dispatch it (a concurrent double-run) — which happens when the
        # item's ADO state is itself a trigger state (e.g. a held needs_human item whose
        # state is "Active"). _process overwrites this stamp when it starts.
        self._processed[item.id] = datetime.now(UTC)
        item.pending_comment = comment
        await c.ado.add_comment(
            item.id,
            "<div><b>💬 Got your comment</b> — reprocessing with your latest guidance.</div>",
        )
        self._log.info("reprocessing on human comment", id=item.id)
        asyncio.create_task(self._process(item))

    async def _process(self, item: WorkItemInfo) -> None:
        c, cfg = self._c, self._config
        if item.id in self._inflight:
            return  # already running — guard against a concurrent dispatch (double-run)
        self._inflight.add(item.id)
        try:
            async with self._gate:
                try:
                    self._processed[item.id] = datetime.now(UTC)

                    classified = c.router.classify(item)
                    classified = await c.plugins.run_pre_processors(classified)
                    self._log.info(
                        "processing", item=str(item), category=str(classified.category)
                    )

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
        finally:
            self._inflight.discard(item.id)
        # A human commented while this run was in flight — apply it now as a follow-up
        # pass (on top of what just ran) rather than interrupting mid-run.
        follow = self._pending_comment.pop(item.id, None)
        if follow is not None and not cfg.dry_run:
            await self._dispatch_with_comment(item, follow)

    async def _process_batch(self, items: list[WorkItemInfo]) -> None:
        """Run a linked cluster as ONE agent run, then report PER ITEM.

        The run is shared; everything the board sees is not. Each item keeps its own
        execution record, state, tags, comment, retry budget and metrics — so a batch
        where 2 of 3 PRs land leaves the third FAILED and retryable on its own, and a
        human reading any one work item sees only its own outcome.
        """
        c, cfg = self._c, self._config
        fresh = [i for i in items if i.id not in self._inflight]
        if len(fresh) != len(items):
            # Part of the cluster is already running (webhook / comment re-entry):
            # don't take the whole cluster hostage — let this cycle skip it.
            self._log.info(
                "batch skipped — member already in flight", ids=[i.id for i in items]
            )
            return
        ids = [i.id for i in items]
        self._inflight.update(ids)
        try:
            async with self._gate:
                now = datetime.now(UTC)
                records: dict[int, int] = {}
                try:
                    for item in items:
                        self._processed[item.id] = now
                        classified = c.router.classify(item)
                        await c.plugins.run_pre_processors(classified)
                        if not c.rbac.is_user_allowed(item):
                            self._log.warning(
                                "rbac denied user — batch aborted", id=item.id, user=item.created_by
                            )
                            return
                        await c.state_repo.set(
                            item.id, PipelineState.IN_PROGRESS, title=item.title
                        )
                        await self._apply_outcome(item, "in_progress")
                        await c.notifier.notify_started(item, "agent-batch")
                        records[item.id] = await c.execution_repo.start_execution(
                            item, "agent-batch", trigger_tag=self._matched_tag(item)
                        )

                    self._log.info("processing batch", ids=ids)
                    results = await c.executor.run_agent_batch(
                        items, autonomy=cfg.autonomy_level, draft_pr=cfg.pr_is_draft
                    )

                    for item in items:
                        result = results.get(item.id)
                        if result is None:  # executor contract says never, be safe anyway
                            result = ExecutionResult.fail(
                                item.id, "agent-batch", "batch returned no result for this item"
                            )
                        await c.execution_repo.complete_execution(records[item.id], result)
                        if result.cost_tokens:
                            await c.cost_tracker.track(records[item.id], result.cost_tokens)
                            metrics.record_cost(result.cost_tokens)
                        await self._handle_agent_result(item, result)
                        await c.plugins.run_post_processors(item, result)
                        status = (
                            "NEEDS_HUMAN" if result.needs_human
                            else ("SUCCESS" if result.success else "FAILED")
                        )
                        metrics.record_task(status.lower(), str(item.category), "agent-batch")
                        metrics.record_duration(str(item.category), result.duration_seconds)
                        self._log.info("batch member finished", id=item.id, status=status)
                except Exception as exc:  # noqa: BLE001 — one crash must not strand the cluster
                    self._log.error("error processing batch", ids=ids, error=str(exc))
                    for item in items:
                        await c.notifier.notify_error(item, str(exc))
        finally:
            for iid in ids:
                self._inflight.discard(iid)
        # Comments that arrived mid-run are applied per item, one normal run each.
        for item in items:
            follow = self._pending_comment.pop(item.id, None)
            if follow is not None and not cfg.dry_run:
                await self._dispatch_with_comment(item, follow)

    async def _process_agent(self, item: WorkItemInfo, classified: WorkItemInfo) -> None:
        """Control plane around the agent: track, run, read structured result, react."""
        c, cfg = self._c, self._config

        # Closed-loop SDLC engine (opt-in) — headless only, takes precedence.
        if cfg.sdlc_loop_enabled:
            await self._process_sdlc(item, classified)
            return

        # Interactive mode: launch a Remote-Control session and let the human steer.
        if cfg.execution_mode == "interactive":
            if len(self._live) >= cfg.max_concurrent:
                self._processed.pop(item.id, None)  # at capacity — retry next cycle
                return
            await self._dispatch_interactive(item)
            return

        await c.state_repo.set(item.id, PipelineState.IN_PROGRESS, title=item.title)
        await self._apply_outcome(item, "in_progress")
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

    async def _process_sdlc(self, item: WorkItemInfo, classified: WorkItemInfo) -> None:
        """Run the item through the closed-loop SDLC engine, then hand it off.

        Reuses the headless bookkeeping and ``_handle_agent_result`` for terminal
        state/tags, then overrides the ADO state with the profile's handoff state so
        the NEXT machine's role (its ``trigger_states``) picks the item up."""
        c = self._c

        await c.state_repo.set(item.id, PipelineState.IN_PROGRESS, title=item.title)
        await self._apply_outcome(item, "in_progress")
        await c.notifier.notify_started(item, "sdlc")
        record_id = await c.execution_repo.start_execution(
            item, "sdlc", trigger_tag=self._matched_tag(item)
        )

        result = await c.sdlc_engine.run(item)

        await c.execution_repo.complete_execution(record_id, result)
        if result.cost_tokens:
            await c.cost_tracker.track(record_id, result.cost_tokens)
            metrics.record_cost(result.cost_tokens)

        await self._handle_agent_result(item, result)
        await self._apply_sdlc_handoff(item, result)
        await c.plugins.run_post_processors(item, result)

        status = (
            "NEEDS_HUMAN" if result.needs_human else ("SUCCESS" if result.success else "FAILED")
        )
        metrics.record_task(status.lower(), str(classified.category), "sdlc")
        metrics.record_duration(str(classified.category), result.duration_seconds)
        self._log.info(
            "sdlc finished", id=item.id, status=status, duration=round(result.duration_seconds, 1)
        )

    async def _apply_sdlc_handoff(self, item: WorkItemInfo, result: ExecutionResult) -> None:
        """On a successful SDLC run, set the profile's handoff ADO state (unless a
        draft PR should await human review first)."""
        cfg = self._config
        if cfg.dry_run or not result.success or result.needs_human:
            return
        name = resolve_profile_name(item.tags, item.work_item_type, cfg)
        state = handoff_state(name, cfg)
        if not state:
            return
        # A draft PR shouldn't auto-advance to the next role before human review.
        draft_block = bool(result.pr_url) and cfg.pr_is_draft and not cfg.sdlc_advance_on_draft
        if draft_block:
            self._log.info("sdlc handoff held (draft PR)", id=item.id, would_be=state)
            return
        await self._c.ado.update_state(item.id, state)
        self._log.info("sdlc handoff", id=item.id, profile=name, state=state)

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
        # Mark it live so a restart doesn't re-dispatch (and re-open) a duplicate session.
        if cfg.live_tag and not cfg.dry_run:
            await c.ado.add_tag(item.id, cfg.live_tag)
        await c.state_repo.set(
            item.id, PipelineState.IN_PROGRESS, title=item.title, detail=f"live session: {session}"
        )
        await self._apply_outcome(item, "in_progress")
        record_id = await c.execution_repo.start_execution(
            item, f"interactive:{session}", trigger_tag=self._matched_tag(item)
        )
        self._live[item.id] = record_id
        await c.ado.add_comment(
            item.id,
            "<div><b>🎮 Live session started</b><br/>Remote Control enabled — open claude.ai "
            f"and attach to session <code>{session}</code> to watch or steer it.</div>",
        )
        # Broadcast the start to Teams/Zalo/email as every other execution mode does. This
        # path only ever wrote the ADO comment above, so on an interactive machine a
        # "completed" card arrived with no "started" card before it. `post_comment=False`
        # because the comment right above already says it, with the session id.
        await c.notifier.notify_started(item, f"interactive:{session}", post_comment=False)
        self._log.info("interactive session dispatched", id=item.id, session=session)

    async def _finalize_live_sessions(self) -> None:
        """Finalise interactive sessions whose result.json has appeared."""
        c, cfg = self._c, self._config
        for item_id, record_id in list(self._live.items()):
            run_dir = self._live_dirs.get(item_id, cfg.workspace_directory)
            item = await c.ado.get_work_item(item_id)
            if item is None:
                self._live.pop(item_id, None)
                # The work item is gone — nothing can reuse this session any more.
                await c.executor.close_interactive(run_dir, item_id)
                await c.executor.release_scratch(self._live_dirs.pop(item_id, None))
                continue
            result = c.executor.finalize_interactive(item, run_dir)
            if result is None:
                continue  # session still running
            await c.execution_repo.complete_execution(record_id, result)
            if result.cost_tokens:
                await c.cost_tracker.track(record_id, result.cost_tokens)
                metrics.record_cost(result.cost_tokens)
            await self._remove_live_tag(item_id)
            await self._handle_agent_result(item, result)
            self._processed[item_id] = datetime.now(UTC)
            self._live.pop(item_id, None)
            closed = await self._close_live_session(item_id, run_dir)
            scratch = self._live_dirs.pop(item_id, None)
            if closed:
                await c.executor.release_scratch(scratch)
            self._log.info(
                "interactive session finalized",
                id=item_id,
                status="SUCCESS" if result.success else "FAILED",
            )
        await self._finalize_orphan_sessions()

    async def _close_live_session(self, item_id: int, run_dir: str) -> bool:
        """Shut the item's Remote-Control console now that it is finalised.

        The CLI is a REPL: it writes ``result.json`` and then sits idle forever, so
        nothing closes the console on its own. Whether *now* is the right moment is
        the operator's call (``interactive_close_on``) — under the default
        ``pr_closed`` the console and its scratch stay alive while the PR is open, so
        review feedback can be worked in the SAME session; the PR monitor closes it
        when the PR merges. Returns True when the scratch may also be released.
        """
        if self._config.interactive_close_on != "result":
            return False
        try:
            await self._c.executor.close_interactive(run_dir, item_id)
        except Exception as exc:  # noqa: BLE001 — never block finalise on cleanup
            self._log.warning("could not close interactive session", id=item_id, error=str(exc))
        return True

    async def _remove_live_tag(self, item_id: int) -> None:
        cfg = self._config
        if cfg.live_tag and not cfg.dry_run:
            await self._c.ado.remove_tag(item_id, cfg.live_tag)

    async def _finalize_orphan_sessions(self) -> None:
        """Interactive sessions tagged live but not tracked in-memory (e.g. after a
        restart) — finalise from their deterministic scratch once the result lands."""
        c, cfg = self._c, self._config
        if not cfg.live_tag:
            return
        try:
            tagged = await c.ado.get_all_tagged_work_items()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("orphan finalize: fetch failed", error=str(exc))
            return
        live = cfg.live_tag.lower()
        for item in tagged:
            if item.id in self._live or live not in {t.lower() for t in item.tags}:
                continue
            run_dir = c.executor.interactive_scratch_dir(item.id)
            result = c.executor.finalize_interactive(item, run_dir)
            if result is None:
                continue  # session still running / no result yet
            await self._remove_live_tag(item.id)
            await self._handle_agent_result(item, result)
            self._processed[item.id] = datetime.now(UTC)
            if await self._close_live_session(item.id, run_dir):
                await c.executor.release_scratch(run_dir)
            self._log.info("orphan interactive session finalized", id=item.id)

    def _score_run(self, result: ExecutionResult) -> RunScore | None:
        """Grade a successful run from its objective signals (None if disabled)."""
        cfg = self._config
        if not cfg.pr_scoring_enabled:
            return None
        inp = ScoreInput(
            completed=result.success,
            has_pr=bool(result.pr_url or result.pr_urls),
            files_changed=len(result.files_changed),
            needs_human=result.needs_human,
            had_error=bool(result.error),
            ci_passed=result.tests_passed,  # auto-test-gate result (None = not run)
        )
        return score_run(
            inp, auto_min=cfg.pr_score_auto_min, review_min=cfg.pr_score_review_min
        )

    async def _add_reviewers(self, item: WorkItemInfo, result: ExecutionResult) -> None:
        """Put the work item's assignee (and any configured extras) on the PR as reviewers.

        Runs for BOTH draft and normal PRs: a draft is exactly the case where someone has
        to be told to look. Every PR of the run is covered, not just the first — a
        multi-repo item opens one per repo and the reviewer is needed on each. Entirely
        best-effort: the PRs are already open, so a failed reviewer add is logged and
        swallowed rather than turning a good run into a failure.
        """
        cfg = self._config
        ids: list[str] = []
        if cfg.pr_add_assignee_as_reviewer and item.assigned_to_id:
            ids.append(item.assigned_to_id)
        ids += [i.strip() for i in (cfg.pr_extra_reviewer_ids or []) if (i or "").strip()]
        if not ids or cfg.dry_run:
            if ids and cfg.dry_run:
                self._log.info("[DRY-RUN] would add PR reviewers", id=item.id, count=len(ids))
            return
        urls = list(dict.fromkeys([u for u in [result.pr_url, *result.pr_urls] if u]))
        for url in urls:
            target = parse_pr_url(url)
            if target is None:
                # The agent reported a PR we can't address (unexpected url shape) — say so
                # rather than silently doing nothing, since the operator opted into this.
                self._log.warning("add reviewers: unrecognised PR url", id=item.id, url=url)
                continue
            repo, pr_id = target
            for reviewer_id in dict.fromkeys(ids):
                ok = await self._c.ado.add_pull_request_reviewer(
                    repo, pr_id, reviewer_id, required=cfg.pr_reviewers_required
                )
                self._log.info(
                    "PR reviewer added" if ok else "PR reviewer add failed",
                    id=item.id, pr=pr_id, reviewer=reviewer_id,
                )

    async def _record_failed_attempt(self, item: WorkItemInfo, result: ExecutionResult) -> int:
        """Register one failed attempt everywhere it needs to land, and return its number.

        ``RetryPolicy`` alone is an in-memory dict that a restart empties and
        ``record_success`` deletes outright, so the attempt count used to vanish exactly
        when it became interesting. This also writes the durable copies: the execution
        row's ``retry_count`` (the History page's "Retries" column), the Prometheus
        counter, and an append-only quality event.
        """
        c = self._c
        c.retry_policy.record_failure(item.id, result.error or "Unknown error")
        state = c.retry_policy.get_state(item.id)
        attempt = state.retry_count if state else 1
        metrics.RETRY_TOTAL.inc()
        await c.execution_repo.mark_retrying(item.id, attempt)
        await c.quality_repo.record(
            work_item_id=item.id, kind=QualityKind.EXECUTION_RETRY, value=attempt,
            actor="autopilot", detail=(result.error or "Unknown error"),
        )
        return attempt

    async def _handle_agent_result(self, item: WorkItemInfo, result: ExecutionResult) -> None:
        c, cfg = self._c, self._config
        if result.needs_human:
            c.retry_policy.record_success(item.id)  # escalated — not a retryable failure
            await c.state_repo.set(item.id, PipelineState.NEEDS_HUMAN, detail=result.error or "")
            # Hold the item (tag) + set state so the poller skips it until a human steps in.
            await self._apply_outcome(item, "needs_human")
            await c.ado.add_comment(
                item.id,
                "<div><b>🙋 ADO Autopilot — Needs human input</b><br/>"
                f"<p>{result.error or result.output}</p></div>",
            )
            self._log.info("escalated to human", id=item.id)
            return

        if result.success:
            c.retry_policy.record_success(item.id)
            score = self._score_run(result)
            # "get a score" gate: a verified-weak run is held for a human rather
            # than silently going to review/done. Skipped in report mode (L1, no PR
            # is expected) and when scoring is disabled.
            if score and score.gate == "escalate" and cfg.autonomy_level != "report":
                await c.state_repo.set(
                    item.id, PipelineState.NEEDS_HUMAN, detail=f"run score {score.score}/100"
                )
                await self._apply_outcome(item, "needs_human")
                await c.ado.add_comment(
                    item.id,
                    score_badge_html(score)
                    + "<div><b>🙋 Held for human</b> — điểm dưới ngưỡng review.</div>",
                )
                await c.notifier.notify_completed(item, result)
                self._log.info("run below review threshold — escalated", id=item.id, score=score.score)
                return
            badge = score_badge_html(score) if score else ""
            if result.pr_url or result.pr_urls:
                await self._add_reviewers(item, result)
                # Traceability first: the link must exist before anyone is told the PR
                # is ready, or the reviewer opens a PR that names no ticket.
                await self._spec_guard.ensure_pr_links(item, result)
            drifts = await self._spec_guard.report_drift(item, result, result.deviations)
            if drifts:
                badge += (
                    f"<div><b>⚠️ {drifts} điểm code khác mô tả</b> — xem comment "
                    "SPEC-DRIFT bên dưới, spec cần cập nhật.</div>"
                )
                if cfg.spec_drift_holds_item:
                    await c.state_repo.set(
                        item.id, PipelineState.NEEDS_HUMAN, detail=f"{drifts} spec drift(s)"
                    )
                    await self._apply_outcome(item, "needs_human")
                    await c.notifier.notify_completed(item, result)
                    self._log.info("held for spec update", id=item.id, drifts=drifts)
                    return
            if result.pr_url and cfg.pr_is_draft:
                await c.state_repo.set(item.id, PipelineState.IN_REVIEW, pr_url=result.pr_url)
                await self._apply_outcome(item, "review")
                await c.ado.add_comment(
                    item.id,
                    badge
                    + "<div><b>🔍 PR created (draft)</b>, awaiting human review.<br/>"
                    f'PR: <a href="{result.pr_url}">{result.pr_url}</a></div>',
                )
                await c.notifier.notify_completed(item, result)
            elif result.pr_url:
                await c.state_repo.set(item.id, PipelineState.DONE, pr_url=result.pr_url)
                await self._apply_outcome(item, "done")
                if badge:
                    await c.ado.add_comment(item.id, badge)
                await c.notifier.notify_completed(item, result)
            else:
                # report mode: the agent commented a plan, no PR.
                await c.state_repo.set(item.id, PipelineState.DONE)
                await self._apply_outcome(item, "report")
                await c.notifier.notify_completed(item, result)
            return

        count = await self._record_failed_attempt(item, result)
        await c.state_repo.set(item.id, PipelineState.FAILED, detail=result.error or "")
        exhausted = c.retry_policy.is_exhausted(item.id)
        await c.notifier.notify_completed(item, result)
        if exhausted:
            await self._apply_outcome(item, "failed")
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
        await self._apply_outcome(item, "in_progress")
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
            if result.pr_url or result.pr_urls:
                await self._add_reviewers(item, result)
            if cfg.pr_is_draft and result.pr_url:
                await self._apply_outcome(item, "review")
                await c.ado.add_comment(
                    item.id,
                    f'<b>🔍 PR created (draft)</b>, awaiting human review.<br/>'
                    f'PR: <a href="{result.pr_url}">{result.pr_url}</a>',
                )
                await c.notifier.notify_completed(item, result)
            else:
                await self._apply_outcome(item, "done" if result.pr_url else "report")
                await c.notifier.notify_completed(item, result)
        else:
            count = await self._record_failed_attempt(item, result)
            exhausted = c.retry_policy.is_exhausted(item.id)
            await c.notifier.notify_completed(item, result)
            if exhausted:
                await self._apply_outcome(item, "failed")
                self._log.error("gave up after retries", id=item.id, count=count)
                await c.ado.add_comment(
                    item.id,
                    f"<b>⛔ Autopilot gave up after {count} retries.</b> "
                    f"Last error: {result.error}",
                )
            else:
                self._processed.pop(item.id, None)
