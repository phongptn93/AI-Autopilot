"""PR merge-conflict tracking and resolution (see ``ai_autopilot.pr_conflicts``).

Every ``pr_conflict_poll_minutes`` the service lists active, in-scope PRs and:

- **detects** the ones ADO reports as ``mergeStatus: conflicts``, with their files;
- **tracks** each as one row — since when, which target commit, what was tried;
- **tells people once** per episode: a comment on the PR and one notification;
- **resolves** when allowed — automatically on PRs the autopilot owns when
  ``pr_conflict_autoresolve`` is on, and on anyone's PR when an allowlisted person asks
  (``/resolve`` on the PR, or the dashboard button). Resolution runs under the SAME
  per-branch lock as the babysitter's ``/ai`` revisions, so the two never push to one
  branch at once;
- **closes the loop**: a PR that merges cleanly again is marked resolved (by whom), one
  completed or abandoned while conflicted is marked closed.

A PR whose conflict disappeared is re-checked against ADO before its row changes: the
list endpoint returns ``[]`` on an error, and "the list came back empty" must never read
as "every conflict was fixed".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from ai_autopilot.config import matches_any_user
from ai_autopilot.container import Container
from ai_autopilot.execution.conflict_resolver import ConflictResolver
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.notifications.base import NotificationMessage, NotificationType
from ai_autopilot.pr_conflicts import (
    BUSY_STATUSES,
    BY_AGENT,
    BY_CLEAN_MERGE,
    BY_OTHER,
    CLOSED,
    ESCALATED,
    IN_SESSION,
    OPEN,
    RESOLVED,
    ConflictResolution,
    ResolveContext,
    files_html,
    is_conflicted,
    merge_status,
    target_commit,
)
from ai_autopilot.services.pr_feedback import command_threads, is_bot_branch, parse_work_item_id


class PrConflictService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.pr_conflicts")
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        # One resolution at a time: each is a full checkout + model run + test suite,
        # and two in parallel on one repo would contend for the same working tree.
        self._sem = asyncio.Semaphore(1)
        self._resolver = ConflictResolver(c.executor, c.config)
        self._said_unconfigured = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._config.pr_conflict_tracking_enabled:
            self._log.info("PR conflict tracking disabled")
            return
        self._task = asyncio.create_task(self._run())
        self._log.info(
            "PR conflict tracking started",
            every_minutes=self._config.pr_conflict_poll_minutes,
            autoresolve=self._config.pr_conflict_autoresolve,
        )

    async def stop(self) -> None:
        for task in [self._task, *self._tasks]:
            if task is not None:
                task.cancel()
        for task in [self._task, *self._tasks]:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _run(self) -> None:
        with contextlib.suppress(Exception):
            stuck = await self._c.pr_conflict_repo.reset_stuck()
            if stuck:
                self._log.info("conflict runs interrupted by a restart re-opened", count=stuck)
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except httpx.TransportError as exc:
                self._log.warning("conflict scan cut short by a connection failure",
                                  error=describe_exc(exc))
            except Exception as exc:  # noqa: BLE001
                self._log.error("conflict scan failed", error=describe_exc(exc))
            await asyncio.sleep(max(1, self._config.pr_conflict_poll_minutes) * 60)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── scan ─────────────────────────────────────────────────────────────────

    async def scan(self) -> dict[str, int]:
        """One pass. Returns counts, for the log and for tests."""
        c, cfg = self._c, self._config
        counts = {"prs": 0, "conflicted": 0, "new": 0, "cleared": 0, "closed": 0,
                  "finalized": 0}
        if not (cfg.ado_pat or (cfg.oauth_app_id and cfg.oauth_app_secret)):
            # Not configured yet is a state, not an error: say so once, not every cycle.
            if not self._said_unconfigured:
                self._log.info("PR conflict scan idle — no ADO credentials configured")
                self._said_unconfigured = True
            return counts
        self._said_unconfigured = False
        counts["finalized"] = await self._finalize_sessions()
        repos = await c.ado.get_repositories()
        conflicted: set[tuple[str, int]] = set()
        for repo in repos:
            rid, rname = repo.get("id"), repo.get("name") or ""
            if not rid:
                continue
            for pr in await c.ado.get_active_pull_requests(rid):
                if not cfg.target_in_scope(pr.get("targetRefName", "")):
                    continue
                counts["prs"] += 1
                if not is_conflicted(pr):
                    continue
                counts["conflicted"] += 1
                conflicted.add((rid, int(pr.get("pullRequestId") or 0)))
                if await self._on_conflicted(rid, rname, pr):
                    counts["new"] += 1
        if repos:
            cleared, closed = await self._reconcile_gone(conflicted)
            counts["cleared"], counts["closed"] = cleared, closed
        if counts["conflicted"] or counts["cleared"] or counts["closed"] or counts["finalized"]:
            self._log.info("conflict scan", **counts)
        return counts

    async def _on_conflicted(self, repo_id: str, repo_name: str, pr: dict) -> bool:
        """Record one conflicted PR; comment/notify on a new episode; maybe resolve."""
        c, cfg = self._c, self._config
        pr_id = int(pr.get("pullRequestId") or 0)
        source = pr.get("sourceRefName", "")
        linked = await c.ado.get_pull_request_work_items(repo_id, pr_id)
        wid = (linked[0] if linked else parse_work_item_id(source)) or 0
        files = [f["path"] for f in await c.ado.get_pull_request_conflicts(repo_id, pr_id)]
        row, fresh = await c.pr_conflict_repo.observe(
            repo_id, pr_id,
            repo_name=repo_name, title=(pr.get("title") or "")[:400],
            author=(pr.get("createdBy") or {}).get("displayName") or "",
            url=self._pr_url(repo_name, pr_id),
            source_branch=source.removeprefix("refs/heads/"),
            target_branch=(pr.get("targetRefName") or "").removeprefix("refs/heads/"),
            work_item_id=int(wid), owned=is_bot_branch(source, tuple(cfg.bot_branch_prefixes)),
            is_draft=bool(pr.get("isDraft")), files=files, target_commit=target_commit(pr),
        )
        if fresh and not row.notified:
            await self._announce(row, files)
            await c.pr_conflict_repo.update(row.id, notified=True)

        requested_by = await self._resolve_command(row)
        if requested_by:
            self._spawn(self.resolve(row.id, requested_by=requested_by))
        elif cfg.pr_conflict_autoresolve and row.owned and row.status in (OPEN, ESCALATED):
            # claim_attempt decides whether these inputs were already tried.
            self._spawn(self.resolve(row.id, requested_by=""))
        return fresh

    async def _reconcile_gone(self, conflicted: set[tuple[str, int]]) -> tuple[int, int]:
        """Tracked conflicts ADO no longer lists as conflicted — confirmed one by one."""
        c = self._c
        cleared = closed = 0
        for row in await c.pr_conflict_repo.active():
            if (row.repo_id, row.pr_id) in conflicted or row.status in BUSY_STATUSES:
                continue
            pr = await c.ado.get_pull_request(row.repo_id, row.pr_id)
            if pr is None:
                continue  # unknown right now — never guess
            status = str(pr.get("status") or "").lower()
            if status in ("completed", "abandoned"):
                await c.pr_conflict_repo.update(
                    row.id, status=CLOSED, resolved_at=datetime.now(UTC),
                    resolved_by=row.resolved_by or status,
                )
                closed += 1
                continue
            if is_conflicted(pr) or merge_status(pr) != "succeeded":
                continue  # still conflicted, or ADO has not recomputed yet
            await c.pr_conflict_repo.update(
                row.id, status=RESOLVED, resolved_at=datetime.now(UTC),
                resolved_by=row.resolved_by or BY_OTHER,
            )
            cleared += 1
        return cleared, closed

    # ── /resolve ─────────────────────────────────────────────────────────────

    async def _resolve_command(self, row) -> str:
        """The allowlisted author of a new ``/resolve`` on this PR, or ``""``.

        Answered in its own thread either way (ack or refusal) — the bot-signed reply is
        the durable "handled" mark, and the stored thread:comment is the second guard.
        """
        c, cfg = self._c, self._config
        command = (cfg.pr_conflict_command or "").strip()
        if not command or row.status in BUSY_STATUSES:
            return ""
        threads = await c.ado.get_pull_request_threads(row.repo_id, row.pr_id)
        for cmd in command_threads(threads, [command]):
            key = f"{cmd['thread_id']}:{cmd['comment_id']}"
            if key == row.handled_command:
                continue
            await c.pr_conflict_repo.update(row.id, handled_command=key)
            who = cmd.get("author_name") or cmd.get("author_email") or "?"
            if not matches_any_user(cmd.get("author_email"), cmd.get("author_name"),
                                    cfg.command_allowlist):
                await c.ado.reply_to_pull_request_thread(
                    row.repo_id, row.pr_id, cmd["thread_id"],
                    "<div>⛔ Tôi chỉ nhận lệnh giải conflict từ người trong danh sách được "
                    "phép. Nhờ tác giả PR hoặc người phụ trách comment lại.</div>",
                )
                continue
            await c.ado.reply_to_pull_request_thread(
                row.repo_id, row.pr_id, cmd["thread_id"],
                f"<div><b>🔧 Đã nhận</b> — tôi sẽ merge <code>{row.target_branch}</code> vào "
                f"<code>{row.source_branch}</code> và giải các hunk xung đột. Chỉ push khi "
                "hết marker, không đụng file khác, test + security gate đều pass. "
                "Kết quả báo ngay tại PR này.</div>",
            )
            return who
        return ""

    # ── resolution ───────────────────────────────────────────────────────────

    def _interactive(self) -> bool:
        """Follow the machine's execution_mode, exactly as work items do."""
        return (self._config.execution_mode or "").strip().lower() == "interactive"

    async def resolve(self, conflict_id: int, *, requested_by: str = "") -> str:
        """Run one resolution attempt. Returns a short outcome word (for the dashboard
        and tests): ``resolved`` / ``escalated`` / ``in_session`` / ``skipped``.

        Interactive mode opens a session and returns ``in_session``; the outcome is
        recorded later, when ``scan`` finds the session's result (``_finalize_sessions``).
        """
        c, cfg = self._c, self._config
        row = await c.pr_conflict_repo.get(conflict_id)
        if row is None:
            return "skipped"
        # A person asking is a new decision: they get one more attempt even against a
        # target commit the automatic path already spent its allowance on.
        allowance = (row.attempts + 1) if requested_by else cfg.pr_conflict_max_attempts
        if not await c.pr_conflict_repo.claim_attempt(row.id, row.target_commit, allowance):
            return "skipped"
        # The attempt is a run from this moment: open its History row now, so it shows
        # as running while it works and every path that ends it — resolved, escalated,
        # timed out, cancelled — closes the SAME row.
        row.execution_id = await self._open_execution(row, requested_by)

        repo_path, problem = self._repo_path(row.repo_name)
        if problem:
            await self._finish_failed(row, problem, {}, 0)
            return "escalated"
        ctx = await self._context(row)
        interactive = self._interactive()
        if not requested_by and not interactive:
            await c.ado.add_pull_request_comment(
                row.repo_id, row.pr_id,
                f"<div><b>🔧 Đang tự giải conflict</b> — merge <code>{row.target_branch}</code>"
                f" vào <code>{row.source_branch}</code> (không rebase, không force-push).</div>",
            )
        self._log.info("resolving PR conflict", pr=row.pr_id, repo=row.repo_name,
                       files=len(json.loads(row.files_json or "[]")),
                       by=requested_by or "auto", mode="interactive" if interactive else "headless")
        lock = c.executor.branch_lock(row.repo_id, row.source_branch)
        if interactive:
            # Only the staging (and a clean-merge push) runs under the branch lock; the
            # session itself may last hours and must not block /ai on this branch.
            async with lock, self._sem:
                prepared = await self._resolver.prepare_session(
                    repo_name=row.repo_name, branch=row.source_branch,
                    target_branch=row.target_branch, ctx=ctx, key=f"conflict-{row.id}",
                )
            if isinstance(prepared, ConflictResolution):
                return await self._record_outcome(row, prepared, requested_by)
            await c.pr_conflict_repo.update(
                row.id, status=IN_SESSION, session_dir=prepared.run_dir,
                session_name=prepared.session, session_started=datetime.now(UTC),
            )
            await c.ado.add_pull_request_comment(
                row.repo_id, row.pr_id,
                f"<div><b>🧑‍💻 Đã mở phiên interactive</b> <code>{prepared.session}</code> để "
                f"giải conflict — merge <code>{row.target_branch}</code> đã được đặt sẵn trên "
                f"<code>{row.source_branch}</code>. Attach qua Remote Control (claude.ai) để "
                "theo dõi hoặc chỉ cách giải các hunk. Khi phiên xong, AI Autopilot kiểm tra "
                "(không còn marker, không đụng file khác, security gate, test) rồi mới push "
                "— không rebase, không force-push.</div>",
            )
            await c.audit_repo.record(
                actor=requested_by or "autopilot", source="pr-conflicts",
                action="pr.conflict_session_opened", target=f"PR !{row.pr_id}",
                detail=prepared.session,
            )
            # A session waiting for someone to attach is exactly the notice a person
            # needs: it closes itself after pr_session_hours with the branch untouched.
            await self._notify(
                row, NotificationType.REMINDER,
                heading=f"🧑‍💻 PR !{row.pr_id} — phiên giải conflict đang chờ",
                text=(f"{row.repo_name}: {row.target_branch} → {row.source_branch}. Attach "
                      f"phiên {prepared.session} qua Remote Control để theo dõi hoặc chỉ "
                      f"cách giải; tự đóng sau {self._config.pr_session_hours or 8}h nếu "
                      "không có kết quả."),
            )
            return "in_session"
        async with lock, self._sem:
            result = await self._resolver.resolve(
                repo_path=repo_path, branch=row.source_branch,
                target_branch=row.target_branch, ctx=ctx,
            )
        return await self._record_outcome(row, result, requested_by)

    async def _context(self, row) -> ResolveContext:
        c = self._c
        pr = await c.ado.get_pull_request(row.repo_id, row.pr_id) or {}
        item_title = ""
        if row.work_item_id:
            with contextlib.suppress(Exception):
                item = await c.ado.get_work_item(row.work_item_id)
                item_title = item.title if item else ""
        return ResolveContext(
            pr_id=row.pr_id, title=row.title, description=str(pr.get("description") or ""),
            work_item_id=row.work_item_id, work_item_title=item_title,
            source_branch=row.source_branch, target_branch=row.target_branch,
        )

    async def _record_outcome(self, row, result: ConflictResolution, actor: str) -> str:
        """One place that turns a finished attempt — headless or interactive — into a
        row update, an audit entry, a PR comment, the History row and a notification.
        Failures go on to ``_finish_failed``, which every other way an attempt can end
        (timed-out or cancelled session, missing repo) also goes through."""
        c = self._c
        await c.audit_repo.record(
            actor=actor or "autopilot", source="pr-conflicts",
            action="pr.conflict_resolved" if result.success else "pr.conflict_escalated",
            target=f"PR !{row.pr_id}",
            detail=(result.how if result.success else result.error)[:300],
        )
        if not result.success:
            await self._finish_failed(row, result.error, result.checks, result.tokens,
                                      files=result.files)
            return "escalated"
        await c.pr_conflict_repo.update(
            row.id, status=RESOLVED, resolved_at=datetime.now(UTC),
            resolved_by=result.how, merge_commit=result.merge_commit,
            checks=result.checks, files=result.files or json.loads(row.files_json or "[]"),
            last_error="", cost_tokens=(row.cost_tokens or 0) + result.tokens,
            session_dir="", session_name="", execution_id=None,
        )
        await c.ado.add_pull_request_comment(
            row.repo_id, row.pr_id, self._success_html(row, result),
        )
        await self._close_execution(
            row, success=True, tokens=result.tokens,
            detail=f"{result.how}: merged {row.target_branch} into {row.source_branch}",
        )
        how = ("merge sạch (ADO chưa kịp tính lại)" if result.how == BY_CLEAN_MERGE
               else "agent đã giải các hunk")
        await self._notify(
            row, NotificationType.COMPLETED,
            heading=f"✅ PR !{row.pr_id} — đã giải conflict",
            text=(f"{row.repo_name}: {row.target_branch} → {row.source_branch} · {how} · "
                  f"{len(result.files)} file. Review lại commit merge trước khi approve."),
            result=ExecutionResult.ok(row.work_item_id or 0, "resolve-conflict", how),
        )
        return "resolved"

    # ── history + notifications (shared by every way an attempt ends) ────────

    def _item(self, row, title: str = "") -> WorkItemInfo:
        """The work item a notice or History row is about — the PR's linked item, or
        none. Never the PR id: that is not a work item id, and passing it made the
        notifier link "Mở work item" to an unrelated item with the same number."""
        return WorkItemInfo(id=row.work_item_id or 0,
                            title=(title or f"[conflict] PR !{row.pr_id} — {row.title}")[:400])

    async def _open_execution(self, row, requested_by: str) -> int | None:
        """Open the History row of an attempt. None when the write fails — history is
        a mirror of the attempt, never a condition for making it."""
        try:
            record_id = await self._c.execution_repo.start_execution(
                self._item(row), "resolve-conflict", profile="conflict",
            )
            await self._c.pr_conflict_repo.update(row.id, execution_id=record_id)
            return record_id
        except Exception as exc:  # noqa: BLE001
            self._log.warning("conflict attempt not recorded in history",
                              pr=row.pr_id, error=describe_exc(exc))
            return None

    async def _close_execution(self, row, *, success: bool, detail: str, tokens: int) -> None:
        """Close the attempt's History row. Duration is left to wall-clock (open →
        close), which for an interactive attempt is the whole session — the number a
        reader wants — rather than just the checks at the end."""
        record_id = getattr(row, "execution_id", None)
        if not record_id:
            return
        with contextlib.suppress(Exception):
            make = ExecutionResult.ok if success else ExecutionResult.fail
            res = make(row.work_item_id or 0, "resolve-conflict", detail or "escalated")
            res.branch_name = row.source_branch
            if row.url:
                res.pr_url, res.pr_urls = row.url, [row.url]
            res.cost_tokens = tokens
            await self._c.execution_repo.complete_execution(record_id, res)
            if tokens:
                await self._c.cost_tracker.track(record_id, tokens)
        with contextlib.suppress(Exception):
            await self._c.pr_conflict_repo.update(row.id, execution_id=None)

    async def _notify(self, row, kind: NotificationType, *, heading: str, text: str,
                      result: ExecutionResult | None = None) -> None:
        """One notice about this PR, through the notifier's alert policy and quiet
        window. The PR is the button; the linked work item (if any) is added by the
        notifier itself."""
        with contextlib.suppress(Exception):
            await self._c.notifier.notify(NotificationMessage(
                work_item=self._item(row, row.title), type=kind,
                heading=heading, text=text, result=result,
                actions=[("🔗 Mở PR", row.url)] if row.url else [],
            ))

    async def _finalize_sessions(self) -> int:
        """Record the outcome of every interactive session that has written its result;
        close and escalate the ones that ran past ``pr_session_hours``."""
        c, cfg = self._c, self._config
        done = 0
        limit = max(1, int(cfg.pr_session_hours or 8)) * 3600
        now = datetime.now(UTC)
        for row in await c.pr_conflict_repo.in_session():
            key = f"conflict-{row.id}"
            started = row.session_started
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if not row.session_dir:
                await self._finish_failed(row, "phiên interactive không còn thư mục làm việc",
                                          {}, 0)
                done += 1
                continue
            if started is not None and (now - started).total_seconds() > limit:
                await self._resolver.cancel_session(row.session_dir, key)
                await self._finish_failed(
                    row, f"phiên interactive quá {cfg.pr_session_hours} giờ không có "
                         "kết quả — đã đóng phiên, branch không bị thay đổi", {}, 0)
                done += 1
                continue
            lock = c.executor.branch_lock(row.repo_id, row.source_branch)
            async with lock, self._sem:
                result = await self._resolver.finalize_session(row.session_dir, key)
            if result is None:
                continue          # still working
            await self._record_outcome(row, result, "")
            done += 1
        return done

    async def cancel(self, conflict_id: int) -> bool:
        """The dashboard's ✕ Close session: close it, leave the branch as it was."""
        c = self._c
        row = await c.pr_conflict_repo.get(conflict_id)
        if row is None or row.status != IN_SESSION:
            return False
        await self._resolver.cancel_session(row.session_dir, f"conflict-{row.id}")
        await self._finish_failed(row, "phiên interactive đã được đóng từ dashboard — "
                                       "branch không bị thay đổi", {}, 0)
        return True

    async def _finish_failed(self, row, error: str, checks: dict, tokens: int,
                             files: list[str] | None = None) -> None:
        c = self._c
        await self._close_execution(row, success=False, detail=error or "escalated",
                                    tokens=tokens)
        await c.pr_conflict_repo.update(
            row.id, status=ESCALATED, last_error=(error or "")[:2000], checks=checks,
            cost_tokens=(row.cost_tokens or 0) + tokens, session_dir="", session_name="",
            **({"files": files} if files else {}),
        )
        await c.ado.add_pull_request_comment(
            row.repo_id, row.pr_id,
            "<div><b>🙋 Chưa tự giải được conflict — cần người xử lý.</b><br/>"
            f"Lý do: {_esc(error or 'không rõ')}<br/>"
            f"{self._checks_html(checks)}"
            "Branch trên origin <b>không bị thay đổi</b> (merge đã được huỷ). Giải tay: "
            f"<code>git merge origin/{row.target_branch}</code> trên "
            f"<code>{row.source_branch}</code>. Comment <code>"
            f"{self._config.pr_conflict_command or '/resolve'}</code> để tôi thử lại sau khi "
            "có thêm thông tin.</div>",
            active=True,
        )
        await self._notify(
            row, NotificationType.REMINDER,
            heading=f"🙋 PR !{row.pr_id} — conflict cần người giải",
            text=f"{row.repo_name}: {row.source_branch} → {row.target_branch}. "
                 f"{(error or '')[:300]}",
        )

    # ── announcements ────────────────────────────────────────────────────────

    async def _announce(self, row, files: list[str]) -> None:
        """The one comment + one notification of a new conflict episode."""
        c, cfg = self._c, self._config
        if cfg.pr_conflict_comment:
            if cfg.pr_conflict_autoresolve and row.owned:
                how = ("AI Autopilot sẽ <b>tự thử giải</b>: merge target vào branch này, "
                       "giải các hunk, chỉ push khi hết marker, không đụng file khác, test và "
                       "security gate đều pass.")
            elif cfg.pr_conflict_command:
                how = (f"Comment <code>{cfg.pr_conflict_command}</code> để AI Autopilot thử "
                       "giải: merge target vào branch (không rebase, không force-push), chỉ "
                       "push khi hết marker, không đụng file khác, test và security gate pass.")
            else:
                how = (f"Giải tay: <code>git merge origin/{row.target_branch}</code> trên "
                       f"<code>{row.source_branch}</code>.")
            await c.ado.add_pull_request_comment(
                row.repo_id, row.pr_id,
                f"<div><b>⚠️ PR đang bị merge conflict</b> với <code>{row.target_branch}</code>"
                f" — không merge được cho tới khi giải xong.<br/>File xung đột:"
                f"{files_html(files)}{how}</div>",
            )
        await self._notify(
            row, NotificationType.REMINDER,
            heading=f"⚠️ PR !{row.pr_id} bị merge conflict",
            text=(f"{row.repo_name}: {row.source_branch} → {row.target_branch} · "
                  f"{len(files)} file · tác giả {row.author or '?'}"),
        )

    def _success_html(self, row, result) -> str:
        how = ("git merge sạch (ADO chưa kịp tính lại)" if result.how == BY_CLEAN_MERGE
               else "đã giải các hunk xung đột" if result.how == BY_AGENT else result.how)
        commit = f" · commit <code>{result.merge_commit[:10]}</code>" if result.merge_commit else ""
        return (
            f"<div><b>✅ Đã giải conflict</b> — merge <code>{row.target_branch}</code> vào "
            f"<code>{row.source_branch}</code>, {how}{commit}.<br/>"
            f"File:{files_html(result.files)}{self._checks_html(result.checks)}"
            "Vui lòng <b>review lại commit merge</b> trước khi approve — đây là thay đổi "
            "mới trên PR.</div>"
        )

    @staticmethod
    def _checks_html(checks: dict) -> str:
        if not checks:
            return ""
        label = {"markers": "Conflict marker", "scope": "Chỉ sửa file conflict",
                 "security": "Security gate", "tests": "Test", "merge": "Merge"}
        rows = "".join(
            f"<li>{'✅' if v == 'ok' or str(v).startswith(('skipped', 'already')) else '❌'} "
            f"{label.get(k, k)}: {_esc(str(v))}</li>"
            for k, v in checks.items()
        )
        return f"Kiểm tra:<ul>{rows}</ul>"

    # ── helpers ──────────────────────────────────────────────────────────────

    def _repo_path(self, repo_name: str) -> tuple[str, str]:
        """The local checkout of the PR's repo, or a reason there is none.

        Refuses rather than falls back: the executor's legacy fallback is the single
        ``repo_working_directory``, and merging a PR's target into the WRONG repository
        is not a failure mode to allow.
        """
        cfg = self._config
        if cfg.workspace_directory:
            path = Path(cfg.workspace_directory) / repo_name
            if (path / ".git").exists():
                return str(path), ""
            return "", (f"repo `{repo_name}` không có trong workspace "
                        f"({cfg.workspace_directory}) — không thể checkout để giải")
        repo = cfg.repo_working_directory
        if repo and Path(repo).name.lower() == repo_name.lower() and (Path(repo) / ".git").exists():
            return repo, ""
        return "", f"không tìm thấy checkout local của repo `{repo_name}`"

    def _pr_url(self, repo_name: str, pr_id: int) -> str:
        cfg = self._config
        org = (cfg.ado_organization or "").rstrip("/")
        project = quote(cfg.code_project or cfg.ado_project or "", safe="")
        if not (org and project and repo_name and pr_id):
            return ""
        return f"{org}/{project}/_git/{quote(repo_name, safe='')}/pullrequest/{pr_id}"


def _esc(s: str) -> str:
    """Errors carry git output and the agent's own words — always plain text, always
    escaped before they reach a PR comment or the dashboard."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
