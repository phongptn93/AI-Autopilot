"""PR babysitter (loop-engineering "PR Babysitter" pattern).

Polls open autopilot PRs for unresolved human review comments and, when found,
feeds them back to Claude to revise the existing branch (which updates the PR).
Bounded by ``max_revisions`` per work item to avoid runaway loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from ai_autopilot.config import describe_users, match_command, matches_any_user
from ai_autopilot.container import Container
from ai_autopilot.data import QualityKind
from ai_autopilot.execution.feedback_handler import resolve_command
from ai_autopilot.logging_config import get_logger
from ai_autopilot.outcomes import apply_outcome
from ai_autopilot.services.pr_feedback import (
    command_threads,
    is_bot_branch,
    parse_work_item_id,
)


class PrMonitorService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.pr_monitor")
        # In-memory caches over PrCommandRepository (survives restarts). Loaded lazily
        # per PR / per item; every mutation is written through to the DB best-effort.
        self._revision_counts: dict[int, int] = {}
        self._handled: dict[int, set[int]] = {}  # pr_id → comment ids already handled
        self._task: asyncio.Task | None = None
        # Command handling runs as bounded background tasks so a slow revise never blocks the
        # scan loop (other PRs keep getting picked up). Same-repo revises are still serialised
        # by the executor's per-repo git lock.
        # Own cap when set (see ReviewerTrackerService) so PR work can't starve execution.
        self._sem = asyncio.Semaphore(
            c.config.pr_review_max_concurrent or c.config.max_concurrent
        )
        self._tasks: set[asyncio.Task] = set()
        # One human often replies /ai to SEVERAL review threads at once; running those
        # concurrently on the same branch corrupts the run (worktree mode: second
        # `worktree add -B` fails "already checked out"; workspace mode: stale fetch).
        # Serialise per (repo, branch) — different branches still run in parallel.
        self._branch_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # Hot lane: PRs the bot recently engaged, re-polled fast so follow-up replies
        # feel chat-like even with no webhook (localhost). (repo_id, pr_id) →
        # (expires_at_monotonic, repo_name, minimal pr dict).
        self._hot: dict[tuple[str, int], tuple[float, str, dict]] = {}
        self._hot_task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._config.feedback_loop_enabled:
            self._log.info("PR feedback loop disabled")
            return
        self._task = asyncio.create_task(self._run(), name="pr-monitor")
        self._hot_task = asyncio.create_task(self._run_hot(), name="pr-monitor-hot")

    async def stop(self) -> None:
        for task in [self._task, self._hot_task, *self._tasks]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    # ── Hot lane: chat-fast follow-ups without a webhook ─────────────────────

    def _mark_hot(self, repo_id: str, repo_name: str, pr: dict) -> None:
        """Put a PR on the fast lane for ``pr_hot_window_minutes`` (refreshes on every
        new activity). While hot, only THIS PR's threads are re-fetched — cheap."""
        pr_id = pr.get("pullRequestId")
        if pr_id is None:
            return
        expires = time.monotonic() + self._config.pr_hot_window_minutes * 60
        self._hot[(repo_id, pr_id)] = (
            expires, repo_name,
            {"pullRequestId": pr_id, "sourceRefName": pr.get("sourceRefName", "")},
        )

    async def _run_hot(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._config.pr_hot_poll_interval_seconds or 3)
                await self._poll_hot_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.error("hot-lane cycle failed", error=str(exc))

    async def _poll_hot_once(self) -> None:
        now = time.monotonic()
        for key in [k for k, (exp, _, _) in self._hot.items() if exp < now]:
            self._hot.pop(key, None)  # cooled down → back to the global scan only
        for (repo_id, _pr_id), (_exp, repo_name, pr) in list(self._hot.items()):
            await self._inspect_pr(repo_id, repo_name, pr)

    def kick(self, repo_id: str, repo_name: str, pr: dict) -> None:
        """Webhook fast-path: inspect ONE PR right now instead of waiting for the
        next poll cycle (the ADO Service Hook fires this on every PR comment, so a
        ``/command`` gets its ack in ~1s). Safe alongside the poller: the in-memory
        handled set + the per-comment durable mark stop double-dispatch, and the
        per-branch lock serialises the actual runs."""
        if not self._config.feedback_loop_enabled:
            return
        self._spawn(self._kick(repo_id, repo_name, pr))

    async def _kick(self, repo_id: str, repo_name: str, pr: dict) -> None:
        try:
            await self._inspect_pr(repo_id, repo_name, pr)
        except Exception as exc:  # noqa: BLE001 — a background task must not die silently
            self._log.error(
                "webhook-kicked inspection failed", pr=pr.get("pullRequestId"), error=str(exc)
            )

    # ── Restart-proof command state (memory cache over PrCommandRepository) ──

    @property
    def _repo(self):
        return getattr(self._c, "pr_command_repo", None)

    async def _get_handled(self, pr_id: int) -> set[int]:
        if pr_id not in self._handled:
            loaded: set[int] = set()
            if self._repo is not None:
                with contextlib.suppress(Exception):
                    loaded = await self._repo.handled_comments(pr_id)
            self._handled[pr_id] = loaded
        return self._handled[pr_id]

    async def _mark_handled(self, pr_id: int, comment_id: int) -> None:
        self._handled.setdefault(pr_id, set()).add(comment_id)
        if self._repo is not None:
            with contextlib.suppress(Exception):
                await self._repo.mark_handled(pr_id, comment_id)

    async def _get_revisions(self, work_item_id: int) -> int:
        if work_item_id not in self._revision_counts:
            count = 0
            if self._repo is not None:
                with contextlib.suppress(Exception):
                    count = await self._repo.revision_count(work_item_id)
            self._revision_counts[work_item_id] = count
        return self._revision_counts[work_item_id]

    async def _set_revisions(self, work_item_id: int, count: int) -> None:
        previous = self._revision_counts.get(work_item_id, 0)
        self._revision_counts[work_item_id] = count
        if self._repo is not None:
            with contextlib.suppress(Exception):
                await self._repo.set_revision_count(work_item_id, count)
        # `revisions` is a BUDGET: `_release_closed_budgets` zeroes it the moment the PR
        # closes, i.e. exactly when "this item needed N rounds of rework" finally means
        # something. Append the increment to the durable log before that happens.
        quality = getattr(self._c, "quality_repo", None)
        if quality is not None and count > previous:
            await quality.record(
                work_item_id=work_item_id, kind=QualityKind.PR_REVISION, value=count,
                actor="human", detail=f"/ai revise round {count}",
            )

    def _branch_lock(self, repo_id: str, branch: str) -> asyncio.Lock:
        """Get-or-create the lock serialising command handling for one PR branch.
        Safe without its own lock — no ``await`` between lookup and store."""
        key = (repo_id, branch)
        lock = self._branch_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._branch_locks[key] = lock
        return lock

    def _spawn(self, coro) -> None:
        """Run a command handler in the background — tracked so it isn't garbage-collected,
        and discarded when done. Bounded inside the coroutine by the semaphore."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self) -> None:
        self._log.info("PR babysitter started — watching open PRs for review feedback")
        while True:
            try:
                await asyncio.sleep(self._config.pr_poll_interval_seconds or 30)
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.error("PR babysitter cycle failed", error=str(exc))

    async def _scan(self) -> None:
        c = self._c
        repos = await c.ado.get_repositories()
        active_pr_ids: set[int] = set()
        active_branches: set[tuple[str, str]] = set()
        active_items: set[int] = set()
        for repo in repos:
            repo_id = repo.get("id")
            if not repo_id:
                continue
            repo_name = repo.get("name") or ""
            for pr in await c.ado.get_active_pull_requests(repo_id):
                pid = pr.get("pullRequestId")
                if pid is not None:
                    active_pr_ids.add(pid)
                ref = pr.get("sourceRefName", "")
                active_branches.add((repo_id, ref.removeprefix("refs/heads/")))
                wid = parse_work_item_id(ref)
                if wid is not None:
                    active_items.add(wid)
                await self._inspect_pr(repo_id, repo_name, pr)
        # Only prune when the scan actually saw repos — a transient empty result
        # (e.g. an ADO error) must not wipe the caches.
        if repos:
            self._prune_caches(active_pr_ids, active_branches, active_items)
            await self._release_closed_budgets(active_items)

    async def _release_closed_budgets(self, active_items: set[int]) -> None:
        """Give back the revision budget of work items whose PR is no longer open.

        The counter only ever incremented, so an item that spent its three revisions was
        capped for the life of the database — and the cap reply told people to "open a new
        PR", which freed nothing, because the budget is keyed by work item. Deriving the
        release from "has no active PR" needs no close event and repairs itself if a scan
        is missed. An in-flight revise is inherently safe: its PR is still open, so the
        item is in ``active_items``.
        """
        if self._repo is None:
            return
        try:
            spent = await self._repo.all_revision_counts()
            for work_item_id in spent.keys() - active_items:
                await self._repo.reset_revision_count(work_item_id)
                self._revision_counts.pop(work_item_id, None)
                self._log.info("revision budget released — PR closed", id=work_item_id)
        except Exception as exc:  # noqa: BLE001 — housekeeping must not break the scan
            self._log.warning("revision budget release failed", error=str(exc))

    def _prune_caches(
        self, active_pr_ids: set[int], active_branches: set[tuple[str, str]],
        active_items: set[int],
    ) -> None:
        """Bound the in-memory caches to currently-open PRs. ``_handled`` and
        ``_revision_counts`` are DB-backed (a dropped entry simply reloads on next
        access), and only UNLOCKED branch locks are evicted, so this is safe even if
        a scan is briefly incomplete."""
        self._handled = {k: v for k, v in self._handled.items() if k in active_pr_ids}
        self._revision_counts = {
            k: v for k, v in self._revision_counts.items() if k in active_items
        }
        self._branch_locks = {
            k: v for k, v in self._branch_locks.items()
            if k in active_branches or v.locked()
        }

    async def _apply_outcome(self, item: object, outcome: str) -> None:
        """Apply a pipeline outcome's tag + state to the work item (clearing stale outcome
        tags) so the board reflects it's working (``in_progress``) then back in ``review``.
        Best-effort — never blocks the loop.

        Takes the item, not its id, so the work-item type selects the per-type flow —
        an ADO state is only settable on the types that define it."""
        with contextlib.suppress(Exception):
            await apply_outcome(
                self._c.ado, self._config, item.id, outcome,
                getattr(item, "work_item_type", "") or "",
            )

    async def _related_draft_prs(
        self, work_item_id: int, exclude_pr_id: int
    ) -> list[tuple[str, str, dict]]:
        """The work item's OTHER open DRAFT autopilot PRs, across repos — returned as
        ``(repo_id, repo_name, pr)``. Matched by the work-item id in the branch name."""
        c, cfg = self._c, self._config
        out: list[tuple[str, str, dict]] = []
        for repo in await c.ado.get_repositories():
            rid = repo.get("id")
            if not rid:
                continue
            for pr in await c.ado.get_active_pull_requests(rid):
                if pr.get("pullRequestId") == exclude_pr_id or not pr.get("isDraft"):
                    continue
                ref = pr.get("sourceRefName", "")
                if is_bot_branch(ref, tuple(cfg.bot_branch_prefixes)) and (
                    parse_work_item_id(ref) == work_item_id
                ):
                    out.append((rid, repo.get("name") or "", pr))
        return out

    async def _adjust_related_drafts(
        self, work_item_id: int, primary_pr_id: int, instruction: str
    ) -> None:
        """After the primary PR was changed, review the item's OTHER open draft PRs and
        adjust each only where the agent judges it must stay consistent with that change
        (e.g. a BE column change that a matching FE PR should follow). Draft-only = safe."""
        c = self._c
        if not c.config.pr_adjust_related_drafts:
            return
        try:
            related = await self._related_draft_prs(work_item_id, primary_pr_id)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("related-draft scan failed", id=work_item_id, error=str(exc))
            return
        if not related:
            return
        item = await c.ado.get_work_item(work_item_id)
        if item is None:
            return
        for rid, rname, pr in related:
            pr_id2 = pr.get("pullRequestId")
            branch2 = pr.get("sourceRefName", "").removeprefix("refs/heads/")
            self._log.info("assessing related draft PR", id=work_item_id, pr=pr_id2, repo=rname)
            prompt = (
                f"The primary pull request for work item #{work_item_id} was just updated "
                f"per this instruction:\n\n{instruction}\n\nReview THIS related draft PR "
                f"(repo `{rname}`, branch `{branch2}`) and adjust it ONLY where it must stay "
                "consistent with that change (e.g. a matching API / DTO / UI change). If "
                "nothing needs changing, make no changes."
            )
            result = await c.executor.revise(item, branch2, prompt, draft_pr=True, repo=rname)
            if result.success:
                note = (
                    "<div><b>🔁 Đã tự đồng bộ PR liên quan này</b> theo thay đổi vừa rồi để "
                    "hai PR không lệch nhau.</div>"
                )
            elif result.error and "No file changes" in result.error:
                note = (
                    "<div><b>✅ Đã rà PR liên quan này</b> — không cần chỉnh gì để giữ "
                    "đồng bộ.</div>"
                )
            else:
                note = f"<div><b>⚠️ Chưa rà được PR liên quan này:</b> {result.error}</div>"
            with contextlib.suppress(Exception):
                await c.ado.add_pull_request_comment(rid, pr_id2, note)
            await c.ado.add_comment(work_item_id, note)

    async def _inspect_pr(self, repo_id: str, repo_name: str, pr: dict) -> None:
        """Pick the new ``/command`` threads on a bot PR and dispatch each as a background
        task, so a slow revise never blocks the scan loop (other PRs keep flowing).

        Per-commenter: only commands from the user this machine acts for. Commands are marked
        handled SYNCHRONOUSLY (before spawning) so a concurrent scan can't double-dispatch.
        Draft PRs are handled too. Bounded by ``max_revisions`` per work item."""
        c, cfg = self._c, self._config
        source_ref = pr.get("sourceRefName", "")
        if not is_bot_branch(source_ref, tuple(cfg.bot_branch_prefixes)):
            return
        pr_id = pr.get("pullRequestId")
        work_item_id = parse_work_item_id(source_ref)
        if pr_id is None or work_item_id is None:
            return

        threads = await c.ado.get_pull_request_threads(repo_id, pr_id)
        commands = command_threads(
            threads, cfg.comment_commands, bot=await c.mention_identity()
        )
        if not commands:
            return

        claimed = cfg.command_allowlist
        handled = await self._get_handled(pr_id)
        branch = source_ref.removeprefix("refs/heads/")
        to_run: list[tuple[dict, int]] = []
        for cmd in commands:
            cid = cmd["comment_id"]
            if cid in handled:
                continue
            if not matches_any_user(cmd["author_email"], cmd["author_name"], claimed):
                # Someone else's command: don't run it, but don't ghost them either —
                # say who drives this autopilot. The signed reply doubles as the durable
                # handled mark (command_threads skips bot-answered commands), so one
                # refusal per command, even across restarts.
                await self._mark_handled(pr_id, cid)
                self._spawn(self._reply_unauthorized(repo_id, pr_id, cmd["thread_id"], claimed))
                continue
            # Mark before dispatch → no double-run across concurrent scans or restarts.
            await self._mark_handled(pr_id, cid)
            # The revision cap guards against runaway CODE churn — advisory commands
            # (/review) change nothing, so they neither consume nor hit the budget:
            # you can ask for another review even after the item is revision-capped.
            # A bare @mention has its command inferred here, defaulting to advisory.
            advisory = await resolve_command(cfg, cmd)
            if advisory:
                if await self._advisory_exhausted(pr_id, pr):
                    self._spawn(self._reply_advisory_capped(repo_id, pr_id, cmd["thread_id"]))
                    continue
                to_run.append((cmd, await self._get_revisions(work_item_id)))
                continue
            if await self._get_revisions(work_item_id) >= cfg.max_revisions:
                self._spawn(self._reply_capped(repo_id, pr_id, cmd["thread_id"]))
                break
            revision = await self._get_revisions(work_item_id) + 1
            await self._set_revisions(work_item_id, revision)
            to_run.append((cmd, revision))
        if not to_run:
            return

        item = await c.ado.get_work_item(work_item_id)
        if item is None:
            return
        # Live conversation on this PR → fast lane, so the NEXT reply lands in seconds.
        self._mark_hot(repo_id, repo_name, pr)
        await self._apply_outcome(item, "in_progress")  # board: working again
        for cmd, revision in to_run:
            self._spawn(
                self._handle_command(
                    repo_id, repo_name, pr_id, work_item_id, branch, item, cmd, revision
                )
            )

    async def _advisory_exhausted(self, pr_id: int, pr: dict) -> bool:
        """True when this PR already had its allowance of advisory reviews AT THIS COMMIT.

        Advisory commands rightly don't spend the revision budget — they change nothing —
        but each is still a full agent run, so with no ceiling five replies cost five runs
        for findings that barely differ. Scoping the count to the commit keeps the useful
        case (push a fix, ask again) free while stopping repeat reviews of unchanged code.
        Counting happens here, before dispatch, so concurrent replies can't both slip past.
        """
        cap = self._config.pr_advisory_max_per_commit
        if cap <= 0 or self._repo is None:
            return False
        commit = (pr.get("lastMergeSourceCommit") or {}).get("commitId") or ""
        try:
            return await self._repo.record_advisory_run(pr_id, commit) > cap
        except Exception as exc:  # noqa: BLE001 — never block a review on bookkeeping
            self._log.warning("advisory budget check failed", pr=pr_id, error=str(exc))
            return False

    async def _reply_advisory_capped(self, repo_id: str, pr_id: int, thread_id: int) -> None:
        with contextlib.suppress(Exception):
            await self._c.ado.reply_to_pull_request_thread(
                repo_id, pr_id, thread_id,
                f"<div>🔁 Commit này tôi đã review "
                f"{self._config.pr_advisory_max_per_commit} lần — review lại cùng một "
                "commit gần như luôn ra cùng kết quả, nên tôi dừng ở đây.<br>"
                "Push thêm commit là tôi review lại ngay; hoặc nới "
                "<code>pr_advisory_max_per_commit</code>.</div>",
            )

    async def _reply_unauthorized(
        self, repo_id: str, pr_id: int, thread_id: int, claimed: list[str]
    ) -> None:
        # Name EVERYONE allowed, not just the owner: with several accounts permitted, a
        # refusal quoting one of them reads as "ask that person" when the reader may
        # already be on the list under a different address.
        with contextlib.suppress(Exception):
            await self._c.ado.reply_to_pull_request_thread(
                repo_id, pr_id, thread_id,
                f"<div>🔒 Trên máy này tôi chỉ nhận lệnh từ: <b>{describe_users(claimed)}</b> — "
                "nhờ đúng người reply để tôi xử lý.</div>",
            )

    async def _reply_capped(self, repo_id: str, pr_id: int, thread_id: int) -> None:
        with contextlib.suppress(Exception):
            await self._c.ado.reply_to_pull_request_thread(
                repo_id, pr_id, thread_id,
                f"<div>⏸️ Đã đạt giới hạn {self._config.max_revisions} lần sửa tự động cho "
                "item này. Tôi tạm dừng để tránh sửa lan man.<br>"
                "Hạn mức <b>tự hoàn lại khi PR này merge hoặc abandon</b>. Cần sửa tiếp "
                "ngay thì nới <code>max_revisions</code>, hoặc reply <code>/review</code> "
                "— lệnh chỉ-bình-luận không tính vào hạn mức.</div>",
            )

    async def _handle_command(
        self, repo_id: str, repo_name: str, pr_id: int, work_item_id: int,
        branch: str, item: object, cmd: dict, revision: int,
    ) -> None:
        """Handle one ``/command`` thread end-to-end (background, semaphore-bounded).

        ACTION commands (e.g. /ai) revise the branch, then on success resolve the thread,
        move the item to review, and assess related draft PRs. ADVISORY commands (e.g.
        /review) only post findings — no code change, no item-state move — and resolving the
        thread means "review done". The ack and result wording differ accordingly."""
        c, cfg = self._c, self._config
        tid = cmd["thread_id"]
        advisory = match_command(cmd["instruction"], cfg.advisory_commands) is not None
        lock = self._branch_lock(repo_id, branch)
        try:
            self._log.info(
                "addressing PR command", id=work_item_id, pr=pr_id, thread=tid,
                revision=revision, advisory=advisory,
            )
            # Ack BEFORE any lock/queue wait: when several /ai land at once (or one
            # arrives mid-run), the human must still see pickup within seconds —
            # a silent queue reads as "the bot missed my comment".
            if advisory:
                ack = (
                    "<div><b>🔍 Đang review</b> — tôi phân tích thay đổi và sẽ đăng nhận "
                    "xét ngay tại đây. Không chỉnh code.</div>"
                )
            elif lock.locked():
                ack = (
                    "<div><b>🕐 Đã nhận</b> — đang có một lệnh khác chạy trên "
                    f"<code>{branch}</code>. Tôi xử lý tuần tự và tiếp nhận ngay sau đó.</div>"
                )
            else:
                ack = (
                    "<div><b>🔧 Đang xử lý</b> — tôi tự chỉnh trên <code>"
                    f"{branch}</code>, commit &amp; push rồi báo kết quả tại đây.</div>"
                )
            await c.ado.reply_to_pull_request_thread(repo_id, pr_id, tid, ack)
            # Mark the thread Pending while we work, so the PR shows it's in progress.
            await c.ado.set_pull_request_thread_status(repo_id, pr_id, tid, "pending")
            # Advisory runs are read-only (no checkout) — they only need a concurrency
            # slot. Action commands serialise per branch so parallel /ai can't corrupt
            # one branch's run.
            guard = contextlib.nullcontext() if advisory else lock
            async with guard, self._sem:
                result = await c.feedback.handle_feedback(
                    item, branch, cmd["instruction"], revision,
                    repo=repo_name, review_only=advisory,
                )
            # Lock released: replies/board updates below don't touch the branch, and the
            # next queued command on it can start immediately.
            # Resolving below doesn't end the conversation: command detection is
            # per-comment (see ``command_threads``), so a reply here re-activates
            # the thread. Say so — otherwise nobody knows replying works.
            # Generated from comment_command / comment_advisory_commands, so the hint can
            # never advertise a command this instance would ignore. Blank when the command
            # trigger is off — then offer nothing rather than a dangling label.
            hint_html = self._config.comment_command_hint_html
            hint = f"<br/>{hint_html}" if hint_html else ""
            if result.success:
                msg = (
                    f"<div><b>🔍 Đã review xong</b> — nhận xét chi tiết ở trên.{hint}</div>"
                    if advisory else
                    f"<div><b>✅ Đã xử lý xong</b> — branch đã được cập nhật.{hint}</div>"
                )
            else:
                verb = "review" if advisory else "xử lý"
                msg = f"<div><b>⚠️ Chưa {verb} được:</b> {result.error}{hint}</div>"
            await c.ado.reply_to_pull_request_thread(repo_id, pr_id, tid, msg)
            await c.ado.add_comment(work_item_id, msg)   # also on the board's work item
            # The reply invites a follow-up ("Reply /ai …") — keep the fast lane warm.
            self._mark_hot(repo_id, repo_name, {
                "pullRequestId": pr_id, "sourceRefName": f"refs/heads/{branch}",
            })
            if result.success:
                # Resolve the thread (Fixed = durable "handled" mark). Only an ACTION
                # changes code → then move the item to review + assess draft PRs.
                await c.ado.set_pull_request_thread_status(repo_id, pr_id, tid, "fixed")
                if not advisory:
                    await self._apply_outcome(item, "review")
                    await self._adjust_related_drafts(work_item_id, pr_id, cmd["instruction"])
            else:
                # Back to Active so the PR still flags the thread as needing attention.
                await c.ado.set_pull_request_thread_status(repo_id, pr_id, tid, "active")
        except Exception as exc:  # noqa: BLE001 — a background task must not die silently
            self._log.error(
                "PR command handler failed", id=work_item_id, pr=pr_id, error=str(exc)
            )
