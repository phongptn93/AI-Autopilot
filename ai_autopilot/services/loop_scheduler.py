"""Scheduled autonomous loops (loop-engineering production patterns).

Runs configured skills on a cadence — e.g. a dependency sweeper, changelog
drafter, or CI sweeper — each opening a PR with any resulting changes. Backed by
APScheduler; cadence comes from a cron expression or a fixed interval.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ai_autopilot import reports
from ai_autopilot.config import ScheduledLoop
from ai_autopilot.container import Container
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.notifications.base import NotificationMessage, NotificationType
from ai_autopilot.workspace import discover_repos


class LoopScheduler:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.loop_scheduler")
        self._scheduler = AsyncIOScheduler()
        # Loops running right now. APScheduler's own ``max_instances=1`` covers the
        # SCHEDULED path only, and the page's ▶ Run does not go through it — so two
        # presses, or a press while the cron fires, would put two agents on one repo
        # and file two reports for one question.
        self._running: set[str] = set()

    def start(self) -> None:
        registered = self._register()
        self._scheduler.start()
        self._log.info("loop scheduler started", count=registered)

    def reload(self) -> int:
        """Re-read ``scheduled_loops`` and reschedule; returns how many are live.

        The Loops page edits the schedule of a process that is already running. Without
        this, saving a cadence changed the config and nothing else — the old job kept
        firing until someone restarted the service, which is the kind of gap where an
        operator concludes the page does not work.
        """
        for job in self._scheduler.get_jobs():
            job.remove()
        registered = self._register()
        if not self._scheduler.running:
            self._scheduler.start()
        self._log.info("loop scheduler reloaded", count=registered)
        return registered

    def _register(self) -> int:
        """Add a job per enabled loop with a valid cadence; returns how many."""
        loops = [loop for loop in self._config.scheduled_loops if loop.enabled]
        if not loops:
            self._log.info("no scheduled loops configured")
            return 0
        count = 0
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
            count += 1
            self._log.info(
                "scheduled loop registered", name=loop.name, mode=loop.mode,
                agents=loop.agents or None,
            )
        return count

    def is_running(self, name: str) -> bool:
        """Is this loop mid-run? Asked by the page before it offers to start another."""
        return name in self._running

    async def run_now(self, name: str) -> bool:
        """Run one loop immediately, off-schedule. False when no such loop is enabled.

        What the page's ▶ Run is: an audit whose cadence is weekly cannot otherwise be
        tried out, and "save it and wait until Saturday" is not a way to find out whether
        the prompt works.
        """
        loop = next(
            (le for le in self._config.scheduled_loops
             if le.name == name and le.enabled), None
        )
        if loop is None:
            return False
        await self._run_loop(loop)
        return True

    async def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def _run_loop(self, loop: ScheduledLoop) -> None:
        if loop.name in self._running:
            self._log.info("loop already running — this run skipped", name=loop.name)
            return
        self._running.add(loop.name)
        try:
            await self._dispatch(loop)
        finally:
            self._running.discard(loop.name)

    async def _dispatch(self, loop: ScheduledLoop) -> None:
        c, cfg = self._c, self._config
        # A loop bound to a project runs in THAT project's workspace, so its repo and
        # base branch default to the workspace's rather than the root config's.
        scoped = cfg.scoped_for_project(loop.project)
        repo = loop_repo(loop, cfg)
        base = loop.base_branch or scoped.base_branch
        if not repo:
            self._log.warning("scheduled loop has no repo configured", name=loop.name)
            return

        if loop.is_scan:
            await self._run_scan(loop, repo, base)
            return
        if loop.is_report:
            await self._run_report(loop, repo, base)
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

    async def _change_digest(self, repo: str, base: str, hours: int = 24) -> str:
        """The recent change set, computed here and bounded here.

        A review loop's first act is to discover what changed, and the obvious command
        for that — a log WITH patches over a day of a busy repo — is larger than the
        model's context. The run then dies before reviewing a single line, which is
        exactly what kept happening. Computing it here costs two cheap git calls and
        removes the reason to run the expensive one.

        Best-effort: a repo that cannot answer (shallow clone, no history in the window)
        returns nothing and the prompt simply carries no digest.
        """
        since = f"--since={hours} hours ago"
        log = await self._git_text(repo, ["log", since, "--oneline", "-n", "100"])
        stat = await self._git_text(repo, ["diff", "--stat", f"@{{{hours} hours ago}}", "--"])
        parts = []
        if log:
            parts.append("Commits:\n" + _clip(log, 100))
        if stat:
            parts.append("Files touched (git diff --stat):\n" + _clip(stat, 200))
        return "\n\n".join(parts)

    async def _git_text(self, repo: str, args: list[str]) -> str:
        """One read-only git command, or "" — never raises into the loop."""
        try:
            return (await self._c.executor._git(args, repo, check=False)).strip()
        except Exception as exc:  # noqa: BLE001 — a digest is a convenience
            self._log.warning(
                "change digest unavailable", repo=repo, args=args, error=describe_exc(exc)
            )
            return ""

    async def _run_report(self, loop: ScheduledLoop, repo: str, base: str) -> None:
        """Run an audit loop: read-only pass, parse findings, store and render.

        The report is saved whether the run succeeded or not. "The nightly review did
        not run" is precisely the fact an operator has no other way to notice — a loop
        that fails silently looks exactly like a loop that found nothing.
        """
        c = self._c
        started = datetime.now(UTC)
        self._log.info("running report loop", name=loop.name, repo=repo, agents=loop.agents)

        prompt = reports.audit_prompt(
            loop.prompt, loop.agents, repo, digest=await self._change_digest(repo, base)
        )
        item = WorkItemInfo(id=0, title=f"[audit] {loop.name}")
        record_id = await c.execution_repo.start_execution(
            item, f"audit:{loop.name}", profile=loop.name
        )
        result = await c.executor.run_audit(loop.name, prompt, repo, base, loop.project)
        await c.execution_repo.complete_execution(record_id, result)
        if result.cost_tokens:
            await c.cost_tracker.track(record_id, result.cost_tokens)

        summary, findings = reports.parse_findings(result.output or "")
        report = reports.Report(
            loop=loop.name,
            summary=summary or (result.error or "")[:500],
            body_md=result.output or "",
            findings=findings,
            status="success" if result.success else "failed",
            project=loop.project,
            repo=repo,
            started_at=started,
            finished_at=datetime.now(UTC),
            duration_seconds=result.duration_seconds,
            agents=list(loop.agents or []),
        )
        html_path = self._write_html(loop, report) if loop.report_html else ""
        report_id = 0
        # Storage is best-effort on purpose: a database hiccup must not also cost the
        # notification that tells someone the audit ran.
        with contextlib.suppress(Exception):
            report_id = await c.loop_report_repo.save(
                report, html_path=html_path, error=result.error or ""
            )

        counts = report.counts
        self._log.info(
            "report loop finished", name=loop.name, success=result.success,
            report_id=report_id or None, html=html_path or None,
            findings=len(findings), worst=report.worst,
            **{k: v for k, v in counts.items() if v},
        )
        await self._notify(item, result)

    async def _run_scan(self, loop: ScheduledLoop, repo: str, base: str) -> None:
        """Run a SCAN loop: the security scanner, stored with a lifecycle, then the same
        report row / HTML / notification a report loop produces — so the Reports page,
        the Security page and the notifier all see one run, not three."""
        from ai_autopilot.security_scan import ado_sync
        from ai_autopilot.security_scan.runner import ScanRequest, run_scan

        c, cfg = self._c, self._config
        sec = cfg.security_scan
        scoped = cfg.scoped_for_project(loop.project)
        ai_mode = (loop.scan_ai_mode or sec.ai_mode or "off").lower()
        self._log.info("running scan loop", name=loop.name, repo=repo, ai=ai_mode)
        item = WorkItemInfo(id=0, title=f"[scan] {loop.name}")
        record_id = await c.execution_repo.start_execution(
            item, f"scan:{loop.name}", profile=loop.name
        )
        req = ScanRequest(
            repo=repo, project=loop.project, workspace=scoped.workspace_directory,
            tools=list(loop.scan_tools or sec.tools), ai_mode=ai_mode,
            ai_agents=list(loop.agents or sec.ai_agents), scope=loop.scan_scope or "full",
            base_branch=base, fail_on=sec.fail_on, trigger="loop", loop_name=loop.name,
            write_html=loop.report_html, max_findings_per_tool=sec.max_findings_per_tool,
            semgrep_config=list(sec.semgrep_config),
            disabled_rules=list(sec.disabled_rules), ignore_paths=list(sec.ignore_paths),
        )
        scan = await run_scan(
            req, executor=c.executor, security_repo=c.security_repo,
            loop_report_repo=c.loop_report_repo, config=sec,
        )
        from ai_autopilot.models import ExecutionResult

        summary = scan.ai_summary or (
            f"{len(scan.findings)} finding(s), {len(scan.diff.new)} new, "
            f"gate {'passed' if scan.passed else 'FAILED'}"
        )
        result = (ExecutionResult.ok if scan.passed else ExecutionResult.fail)(
            0, f"scan:{loop.name}", summary
        )
        result.cost_tokens = scan.cost_tokens
        result.duration_seconds = scan.duration_seconds
        await c.execution_repo.complete_execution(record_id, result)
        if scan.cost_tokens:
            await c.cost_tracker.track(record_id, scan.cost_tokens)

        # Bugs for the new ones, a note on the fixed ones — both best-effort.
        with contextlib.suppress(Exception):
            repo_abs = str(Path(repo).resolve())  # noqa: ASYNC240 — local path math
            outcome = await ado_sync.file_new_findings(
                ado=c.ado_for(loop.project), security_repo=c.security_repo, config=cfg,
                repo=repo_abs, project=loop.project, scan_id=scan.scan_id,
                fingerprints=[f.fingerprint for f in scan.diff.new],
                repo_name=Path(repo).name,
            )
            fixed = await ado_sync.comment_fixed(
                ado=c.ado_for(loop.project), security_repo=c.security_repo,
                repo=repo_abs, fingerprints=list(scan.diff.fixed),
            )
            if outcome.filed or fixed:
                self._log.info("scan loop synced to ADO", name=loop.name,
                               filed=len(outcome.filed), fixed_commented=fixed)
        self._log.info(
            "scan loop finished", name=loop.name, passed=scan.passed,
            findings=len(scan.findings), new=len(scan.diff.new), fixed=len(scan.diff.fixed),
            report_id=scan.report_id or None, html=scan.html_path or None,
        )
        await self._notify(item, result)

    def _write_html(self, loop: ScheduledLoop, report) -> str:
        """Render the report next to the workspace; returns the path, or "" if it could
        not be written. Never raises — an unwritable directory must not lose the run."""
        workspace = self._config.scoped_for_project(loop.project).workspace_directory
        if not workspace:
            # Same rule as the activity feed: with no workspace the path would be
            # relative, and the file would land in whatever directory the process
            # happens to run from. No workspace, no file.
            return ""
        try:
            out_dir = Path(workspace) / "reports" / _slug(loop.name)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{datetime.now(UTC).strftime('%Y%m%d-%H%M')}.html"
            path.write_text(reports.render_html(report), encoding="utf-8")
            return str(path)
        except OSError as exc:
            self._log.warning(
                "report html not written", name=loop.name, error=describe_exc(exc)
            )
            return ""

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


def _clip(text: str, max_lines: int) -> str:
    """First ``max_lines`` lines, saying how many were dropped — a digest that silently
    truncates would have the reader review a change set that is not the whole one."""
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n… (+{len(lines) - max_lines} dòng nữa)"


def loop_repo(loop: ScheduledLoop, config) -> str:
    """The directory this loop actually runs git in, or "" when it has none.

    Three ways to name it, in the order a reader would expect:

    * the loop's own ``repo_path`` — absolute, or a repo NAME inside the workspace,
      which is what the field's own placeholder ("workspace's repo") promises and what
      anyone types first. Passing a bare name straight to git resolved it against the
      service's process directory instead, which is nobody's intention;
    * ``repo_working_directory`` — the single-repo setting that predates workspaces;
    * the workspace itself, when it holds exactly ONE repo. With several there is a
      real choice to make and guessing it would be worse than saying so.
    """
    scoped = config.scoped_for_project(loop.project)
    workspace = (scoped.workspace_directory or "").strip()
    named = (loop.repo_path or "").strip()
    if named:
        if workspace and not Path(named).is_absolute():
            inside = Path(workspace) / named
            if inside.is_dir():
                return str(inside)
        return named
    if (scoped.repo_working_directory or "").strip():
        return scoped.repo_working_directory.strip()
    repos = discover_repos(workspace)
    return str(Path(workspace) / repos[0]) if len(repos) == 1 else ""


def loop_blockers(loop: ScheduledLoop, config) -> list[str]:
    """Why this loop cannot run right now, in the reader's words. Empty = it can.

    The same two conditions ``_dispatch`` enforces, read from one place so the page
    cannot promise a run the scheduler will refuse. Both failures are silent by nature:
    the loop is listed, enabled, looks configured — and either never fires or stops on
    its first line with one warning in a log on the server.
    """
    out: list[str] = []
    if _trigger(loop) is None:
        out.append("cron/interval không hợp lệ — không bao giờ tới giờ chạy")
    if not loop_repo(loop, config):
        scoped = config.scoped_for_project(loop.project)
        repos = discover_repos((scoped.workspace_directory or "").strip())
        if len(repos) > 1:
            # Naming them turns "fill in a path" into "pick one of these" — the field
            # takes a repo name, and the reader should not have to go and find it.
            out.append(
                "workspace có " + str(len(repos)) + " repo (" + ", ".join(repos[:4])
                + ") — điền tên một repo vào ô Repo path"
            )
        else:
            out.append("chưa có repo — mọi lần chạy dừng trước khi bắt đầu")
    return out


def _slug(name: str) -> str:
    """A loop name as a path/branch segment. Shared so the branch a build loop pushes
    and the directory an audit writes into are named the same way."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (name or "").lower())).strip("-")


def _loop_branch(loop: ScheduledLoop) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    return f"{loop.branch_prefix}/{_slug(loop.name)}-{stamp}"
