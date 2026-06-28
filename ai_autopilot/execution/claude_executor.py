"""Orchestrate a work item end-to-end with Claude (ported from ``ClaudeExecutor``).

Flow: create feature branch → run the routed skill via the Claude Agent SDK →
verify file changes → commit + push → auto-review → create PR. Unlike the legacy
.NET version this no longer parses CLI stdout for token counts; it reads them
from the SDK's structured ``ResultMessage``.
"""

from __future__ import annotations

import asyncio
import re
import time

from ai_autopilot.config import Settings
from ai_autopilot.execution.auto_reviewer import AutoReviewer
from ai_autopilot.execution.claude_client import ClaudeRun, run_claude
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, TaskCategory, WorkItemInfo

_BRANCH_PREFIX = {
    TaskCategory.BUG: "fix",
    TaskCategory.FRONTEND_TASK: "feature/fe",
    TaskCategory.BACKEND_TASK: "feature/be",
}


class GitError(RuntimeError):
    """Raised when a git command exits non-zero."""


class ClaudeExecutor:
    def __init__(self, config: Settings, reviewer: AutoReviewer) -> None:
        self._config = config
        self._reviewer = reviewer
        self._log = get_logger("execution.claude_executor")

    async def execute(
        self, item: WorkItemInfo, skill_command: str, draft_pr: bool = False
    ) -> ExecutionResult:
        started = time.monotonic()
        branch = _branch_name(item)
        work_dir, base_branch = self._resolve_repo(item)

        try:
            self._log.info("creating branch", id=item.id, branch=branch, repo=work_dir)
            await self._git(f"checkout -b {branch} origin/{base_branch}", work_dir)

            self._log.info("executing skill", id=item.id, skill=skill_command)
            claude_run = await self._run_claude(skill_command, work_dir)

            if not await self._has_changes(work_dir):
                self._log.warning("no file changes after execution", id=item.id)
                await self._git(f"checkout {base_branch}", work_dir, check=False)
                return ExecutionResult.fail(item.id, skill_command, "No file changes produced")

            changed_files = await self._changed_files(work_dir)

            commit_msg = f"feat(autopilot): {item.title} (#{item.id})"
            await self._git("add -A", work_dir)
            await self._git(["commit", "-m", commit_msg], work_dir)
            await self._git(f"push -u origin {branch}", work_dir)

            review = await self._reviewer.review(work_dir)
            if not review.passed:
                self._log.warning(
                    "auto-review blocked PR", id=item.id, issues=len(review.critical_issues)
                )
                result = ExecutionResult.fail(
                    item.id,
                    skill_command,
                    "Auto-review blocked: " + "; ".join(review.critical_issues[:3]),
                )
                result.branch_name = branch
                result.files_changed = changed_files
                result.duration_seconds = time.monotonic() - started
                return result

            self._log.info("creating PR", id=item.id, draft=draft_pr)
            pr_flag = " --draft" if draft_pr else ""
            pr_run = await self._run_claude(f"/pr-create{pr_flag}", work_dir)

            result = ExecutionResult.ok(item.id, skill_command, claude_run.text)
            result.branch_name = branch
            result.files_changed = changed_files
            result.duration_seconds = time.monotonic() - started
            result.cost_tokens = claude_run.total_tokens + pr_run.total_tokens
            result.cost_usd = _sum_cost(claude_run, pr_run)
            result.pr_url = _extract_pr_url(pr_run.text)
            return result

        except TimeoutError:
            minutes = self._config.task_timeout_minutes
            self._log.error("execution timed out", id=item.id, minutes=minutes)
            result = ExecutionResult.fail(
                item.id, skill_command, f"Timed out after {minutes} minutes"
            )
            result.duration_seconds = time.monotonic() - started
            return result
        except Exception as exc:  # noqa: BLE001
            self._log.error("execution failed", id=item.id, error=str(exc))
            await self._git(f"checkout {base_branch}", work_dir, check=False)
            result = ExecutionResult.fail(item.id, skill_command, str(exc))
            result.duration_seconds = time.monotonic() - started
            return result

    async def _run_claude(self, prompt: str, work_dir: str) -> ClaudeRun:
        return await run_claude(
            prompt,
            work_dir,
            timeout_seconds=self._config.task_timeout_minutes * 60,
            model=self._config.claude_model or None,
            max_turns=self._config.claude_max_turns or None,
        )

    # ── git helpers ─────────────────────────────────────────────────────────

    async def _git(self, args: str | list[str], work_dir: str, check: bool = True) -> str:
        argv = args.split() if isinstance(args, str) else args
        proc = await asyncio.create_subprocess_exec(
            "git",
            *argv,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode(errors="replace")
        if proc.returncode != 0:
            err = stderr.decode(errors="replace") or out
            self._log.warning("git failed", code=proc.returncode, args=argv, error=err.strip())
            if check:
                raise GitError(f"git {' '.join(argv)} failed: {err.strip()}")
        return out

    async def _has_changes(self, work_dir: str) -> bool:
        return bool((await self._git("status --porcelain", work_dir)).strip())

    async def _changed_files(self, work_dir: str) -> list[str]:
        out = await self._git("status --porcelain", work_dir)
        files = []
        for line in out.splitlines():
            name = line[3:].strip() if len(line) > 3 else line.strip()
            if name:
                files.append(name)
        return files

    def _resolve_repo(self, item: WorkItemInfo) -> tuple[str, str]:
        if self._config.repos:
            category = str(item.category)
            for repo in self._config.repos:
                if any(c.lower() == category.lower() for c in repo.categories):
                    return repo.path, repo.base_branch
        return self._config.repo_working_directory, self._config.base_branch


def _branch_name(item: WorkItemInfo) -> str:
    prefix = _BRANCH_PREFIX.get(item.category, "feature")
    return f"{prefix}/{item.id}-{_slugify(item.title)}"


def _slugify(text: str) -> str:
    cleaned = "".join(c for c in text.lower() if c.isalnum() or c in " -")
    return re.sub(r"-+", "-", cleaned.replace(" ", "-")).strip("-")


def _extract_pr_url(output: str) -> str | None:
    for line in output.splitlines():
        if "pullrequest" not in line.lower():
            continue
        match = re.search(r"https://\S+", line)
        if match:
            return match.group(0)
    return None


def _sum_cost(*runs: ClaudeRun) -> float | None:
    costs = [r.cost_usd for r in runs if r.cost_usd is not None]
    return sum(costs) if costs else None
