"""Orchestrate a work item end-to-end with Claude (ported from ``ClaudeExecutor``).

Flow: create feature branch → run the routed skill via the Claude Agent SDK →
verify file changes → commit + push → auto-review → create PR. Unlike the legacy
.NET version this no longer parses CLI stdout for token counts; it reads them
from the SDK's structured ``ResultMessage``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from ai_autopilot import activity
from ai_autopilot.config import BOT_COMMENT_INSTRUCTION, Settings
from ai_autopilot.execution.auto_reviewer import AutoReviewer
from ai_autopilot.execution.claude_client import ClaudeRun, run_claude
from ai_autopilot.execution.result_contract import clear_result, read_result
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, TaskCategory, WorkItemInfo
from ai_autopilot.workspace import discover_repos, parse_repo_descriptions

_BRANCH_PREFIX = {
    TaskCategory.BUG: "fix",
    TaskCategory.FRONTEND_TASK: "feature/fe",
    TaskCategory.BACKEND_TASK: "feature/be",
}


class GitError(RuntimeError):
    """Raised when a git command exits non-zero."""


@dataclass
class _Workspace:
    """An isolated checkout (git worktree or in-place branch) for one execution."""

    repo: str  # the main repository
    path: str  # directory git operates in (the repo / worktree checkout)
    branch: str
    base_branch: str
    is_worktree: bool
    claude_cwd: str = ""  # dir Claude runs in (workspace root); "" → same as path
    keep: bool = False  # cached revise worktree: survive release, reused next round


class ClaudeExecutor:
    def __init__(self, config: Settings, reviewer: AutoReviewer) -> None:
        self._config = config
        self._reviewer = reviewer
        self._log = get_logger("execution.claude_executor")
        # Serialise git worktree bookkeeping per source repo: two concurrent tasks
        # touching the SAME repo must not run `worktree add`/`prune` at once (they
        # write the same .git and would collide on locks). Keyed by repo path.
        self._repo_locks: dict[str, asyncio.Lock] = {}

    def _repo_lock(self, repo_path: str) -> asyncio.Lock:
        """Get-or-create the lock guarding git bookkeeping for one source repo.

        Safe without its own lock: there is no ``await`` here, so concurrent
        coroutines never interleave inside this method (cooperative scheduling).
        """
        lock = self._repo_locks.get(repo_path)
        if lock is None:
            lock = asyncio.Lock()
            self._repo_locks[repo_path] = lock
        return lock

    async def execute(
        self, item: WorkItemInfo, skill_command: str, draft_pr: bool = False
    ) -> ExecutionResult:
        """Implement a work item on a fresh branch and open a PR."""
        repo, base_branch = self._resolve_repo(item)
        prompt = self._build_prompt(item, skill_command, repo)
        return await self._run_in_workspace(
            item_id=item.id,
            repo=repo,
            branch=_branch_name(item),
            base_branch=base_branch,
            prompt=prompt,
            commit_msg=f"feat(autopilot): {item.title} (#{item.id})",
            draft_pr=draft_pr,
            existing_branch=False,
            create_pr=True,
        )

    def _build_prompt(self, item: WorkItemInfo, skill_command: str, repo: str) -> str:
        """Build the Claude prompt for a work item.

        When running from the workspace root, Claude must be told which repo
        subfolder to edit (its cwd is the workspace, not the repo). The full work
        item content is inlined so Claude can implement even if the ADO MCP is
        unavailable.
        """
        if not self._config.workspace_directory:
            return skill_command  # legacy: cwd is the repo, skill command is enough

        repo_name = _repo_name(repo, self._config.workspace_directory)
        parts = [
            f"Azure DevOps work item #{item.id}: {item.title}",
            f"Type: {item.work_item_type} | Category: {item.category}",
        ]
        if item.description:
            parts.append(f"\nDescription:\n{item.description}")
        if item.acceptance_criteria:
            parts.append(f"\nAcceptance criteria:\n{item.acceptance_criteria}")
        parts.append(
            f"\nTarget repository: ./{repo_name}\n"
            f"Make ALL file changes INSIDE the ./{repo_name}/ subfolder of this workspace. "
            f"Follow the project's .claude rules and skills.\n"
            f"You may use the Azure DevOps MCP to fetch more detail on #{item.id} if needed."
        )
        parts.append(f"\nNow run this skill: {skill_command}")
        return "\n".join(parts)

    # ── AI-native flow (Phase 1): control plane + agent + structured contract ──

    async def run_agent(
        self, item: WorkItemInfo, *, autonomy: str, draft_pr: bool
    ) -> ExecutionResult:
        """Hand the work item to Claude and let it reason end-to-end.

        The control plane runs the agent from the workspace and reads the
        structured result it writes to ``.autopilot/runs/<id>.json``. The agent
        discovers the allowed repos, chooses which to edit, writes code, and opens
        the PR(s) itself — Python does no git plumbing and pins no single repo.
        """
        workspace = self._config.workspace_directory
        started = time.monotonic()

        repos = self._allowed_repos(workspace)
        # Isolate this task in its own worktree scratch so it never touches the
        # user's main checkout; None → run in the shared workspace (disabled / no repos).
        scratch = await self._acquire_agent_scratch(item.id, repos)
        run_dir = scratch or workspace
        try:
            clear_result(run_dir, item.id)
            activity.clear(workspace, item.id)  # activity stays in the real workspace (dashboard reads it)
            isolated = " (isolated worktree)" if scratch else ""
            activity.append(
                workspace, item.id, f"🚀 agent started{isolated} — repos: {', '.join(repos) or '-'}"
            )
            brief = self._build_brief(item, repos, autonomy=autonomy, draft_pr=draft_pr)
            self._log.info("running agent", id=item.id, cwd=run_dir, repos=repos, isolated=bool(scratch))
            claude_run = await self._run_claude(
                brief, run_dir, on_event=lambda line: activity.append(workspace, item.id, line)
            )
            activity.append(workspace, item.id, "✅ agent run finished")
            result = self._result_from_agent(item, read_result(run_dir, item.id), autonomy)
            result.cost_tokens = claude_run.total_tokens
            result.cost_usd = claude_run.cost_usd
        except TimeoutError:
            mins = self._config.task_timeout_minutes
            self._log.error("agent timed out", id=item.id, minutes=mins)
            result = ExecutionResult.fail(item.id, "agent", f"Timed out after {mins} minutes")
        except Exception as exc:  # noqa: BLE001 — never leave the item stuck IN_PROGRESS
            self._log.error("agent crashed", id=item.id, error=str(exc))
            result = ExecutionResult.fail(item.id, "agent", str(exc))
        finally:
            await self.release_scratch(scratch)
        result.duration_seconds = time.monotonic() - started
        return result

    def _allowed_repos(self, workspace: str) -> list[str]:
        """Repos the agent may edit: the configured whitelist, or all discovered."""
        discovered = discover_repos(workspace)
        if not self._config.allowed_repos:
            return discovered
        wanted = {a.lower() for a in self._config.allowed_repos}
        return [r for r in discovered if r.lower() in wanted]

    # ── Per-task isolation: scratch workspace of git worktrees ────────────────

    def _scratch_base(self) -> str:
        """Base dir for per-task scratch worktrees.

        Defaults to ``<workspace>/.aiwt`` — kept inside the workspace so scratch
        lives next to the repos it mirrors (a dotfolder, so ``discover_repos``
        skips it and the worktrees are never mistaken for source repos). Short
        ``agent-<id>`` names keep paths under the Windows 260-char MAX_PATH limit.
        Override the location with ``worktrees_dir``.
        """
        if self._config.worktrees_dir:
            return self._config.worktrees_dir
        ws = self._config.workspace_directory
        if ws:
            return str(Path(ws) / ".aiwt")
        return str(Path(tempfile.gettempdir()) / "ai-autopilot-worktrees")

    async def _ref_exists(self, repo: str, ref: str) -> bool:
        out = await self._git(["rev-parse", "--verify", "--quiet", ref], repo, check=False)
        return bool(out.strip())

    async def _resolve_base_ref(self, repo: str, base_branch: str) -> str | None:
        """A base ref that exists in this repo: the configured base branch, else the
        repo's own default branch (origin/HEAD), else common fallbacks. Different
        repos in one workspace can have different base branches."""
        candidates = [f"origin/{base_branch}"]
        head = (
            await self._git(["symbolic-ref", "refs/remotes/origin/HEAD"], repo, check=False)
        ).strip()
        if head.startswith("refs/remotes/"):
            candidates.append(head[len("refs/remotes/"):])
        candidates += ["origin/main", "origin/master", "origin/develop", "origin/development"]
        for ref in candidates:
            if ref and await self._ref_exists(repo, ref):
                return ref
        return None

    def interactive_scratch_dir(self, item_id: int) -> str:
        """Deterministic scratch path for an interactive session, so it can be
        finalised even after a restart (in-memory tracking is lost)."""
        return str(Path(self._scratch_base()) / f"agent-{item_id}")

    async def _acquire_agent_scratch(
        self, item_id: int, repos: list[str], *, stable: bool = False
    ) -> str | None:
        """Build an isolated scratch workspace for one AI-native task.

        ``<base>/agent-<id>-<uuid>`` holds a *copy* of the shared ``.claude`` plus a
        ``git worktree`` of each allowed repo, so Claude (cwd = scratch) edits and
        commits in the worktrees — never touching the user's main checkout — and
        concurrent tasks never collide. Returns the scratch path, or ``None`` to
        fall back to running in the shared workspace (disabled / no repos / error).
        """
        workspace = self._config.workspace_directory
        if not (workspace and self._config.use_worktrees and repos):
            return None
        base_dir = self._scratch_base()
        Path(base_dir).mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - fast local mkdir
        if stable:  # interactive: deterministic path (reused across restarts); clean any leftover
            scratch = str(Path(base_dir) / f"agent-{item_id}")
            await self.release_scratch(scratch)
        else:  # headless: unique path
            scratch = str(Path(base_dir) / f"agent-{item_id}-{uuid.uuid4().hex[:8]}")
        base_branch = self._config.base_branch
        try:
            Path(scratch).mkdir(parents=True, exist_ok=False)  # noqa: ASYNC240
            # Copy (not link) the shared config so teardown can never delete the
            # real .claude. Exclude its own .git to keep the copy small.
            src_claude = Path(workspace) / ".claude"
            if src_claude.is_dir():
                shutil.copytree(  # noqa: ASYNC240 - small local copy
                    src_claude, Path(scratch) / ".claude",
                    ignore=shutil.ignore_patterns(".git"),
                )
            worktreed: list[str] = []
            for repo in repos:
                src_repo = str(Path(workspace) / repo)
                worktree = str(Path(scratch) / repo)
                # Serialise per-repo so a concurrent task on the same repo doesn't
                # collide on .git locks during fetch / worktree add.
                async with self._repo_lock(src_repo):
                    # --no-recurse-submodules: some repos vendor a private/relative
                    # submodule (e.g. .claude) that isn't reachable from this machine;
                    # recursing turns a fine fetch into a scary code=1 "Could not
                    # access submodule" error. We only need the repo's own refs.
                    await self._git(
                        ["fetch", "--no-recurse-submodules", "origin"], src_repo, check=False
                    )
                    # Repos may use different base branches — resolve one that exists
                    # in THIS repo (configured base → its default branch), else skip
                    # it rather than aborting the whole scratch.
                    base_ref = await self._resolve_base_ref(src_repo, base_branch)
                    if base_ref is None:
                        self._log.warning(
                            "scratch: skipping repo — no usable base branch",
                            id=item_id, repo=repo, base=base_branch,
                        )
                        continue
                    await self._git(["worktree", "add", "--detach", worktree, base_ref], src_repo)
                    worktreed.append(repo)
            if not worktreed:  # nothing isolated → fall back to the shared workspace
                self._log.warning("scratch: no repos worktreed — using shared workspace", id=item_id)
                await self.release_scratch(scratch)
                return None
            self._log.info("created agent scratch", id=item_id, path=scratch, repos=worktreed)
            return scratch
        except Exception as exc:  # noqa: BLE001 — never block the run on isolation failure
            self._log.warning(
                "agent scratch failed — falling back to shared workspace",
                id=item_id, error=str(exc),
            )
            await self.release_scratch(scratch)
            return None

    async def release_scratch(self, run_dir: str | None) -> None:
        """Tear down a scratch workspace (worktrees + dir). Best-effort, never raises.

        No-op when ``run_dir`` is the shared workspace or missing. Worktree source
        repos are derived from the scratch's subfolders (``<workspace>/<name>``).
        """
        workspace = self._config.workspace_directory
        if not run_dir or run_dir == workspace or not Path(run_dir).exists():
            return
        for sub in Path(run_dir).iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                src_repo = str(Path(workspace) / sub.name)
                async with self._repo_lock(src_repo):  # don't race a concurrent worktree add
                    await self._git(
                        ["worktree", "remove", "--force", str(sub)], src_repo, check=False
                    )
                    await self._git(["worktree", "prune"], src_repo, check=False)
        shutil.rmtree(run_dir, ignore_errors=True)  # noqa: ASYNC240

    # ── SDLC loop: one shared branch across many stage runs in a scratch ──────

    async def prepare_stage_branch(self, scratch: str, repos: list[str], branch: str) -> None:
        """Create/reset the item's single feature branch in each worktree.

        ``_acquire_agent_scratch`` adds worktrees ``--detach`` at their base ref; the
        SDLC loop needs ONE stable branch to accumulate every stage's commits and to
        push from the ``pr`` stage. Best-effort per repo (a repo that can't branch is
        skipped, not fatal)."""
        for repo in repos:
            worktree = Path(scratch) / repo
            if worktree.is_dir():
                await self._git(["checkout", "-B", branch], str(worktree), check=False)

    async def stage_commit(
        self, scratch: str, repos: list[str], message: str
    ) -> dict[str, list[str]]:
        """Commit any changes a stage produced in each worktree. Returns
        ``{repo: [changed files]}`` for the repos that actually committed."""
        committed: dict[str, list[str]] = {}
        for repo in repos:
            worktree = Path(scratch) / repo
            if not worktree.is_dir():
                continue
            files = await self._changed_files(str(worktree))
            if not files:
                continue
            await self._git("add -A", str(worktree), check=False)
            await self._git(["commit", "-m", message], str(worktree), check=False)
            committed[repo] = files
        return committed

    async def push_stage_branch(self, scratch: str, repos: list[str], branch: str) -> None:
        """Force-push the shared feature branch for the given repos (the autopilot
        owns these branches — see the legacy force-push rationale)."""
        for repo in repos:
            worktree = Path(scratch) / repo
            if worktree.is_dir():
                await self._git(
                    ["push", "-u", "origin", branch, "--force"], str(worktree), check=False
                )

    # ── Interactive mode: a real Remote-Control session per task ──────────────

    async def dispatch_interactive(
        self, item: WorkItemInfo, *, autonomy: str, draft_pr: bool
    ) -> tuple[bool, str, str]:
        """Launch a real, Remote-Control-enabled Claude Code session for this item.

        The session is interactive — the human can attach via Remote Control from
        claude.ai and steer it — and writes the same ``result.json`` contract when
        done, which the poller picks up to finalise. Fire-and-forget: returns
        ``(launched, session_name, run_dir)`` immediately. ``run_dir`` is the
        isolated worktree scratch (or the shared workspace) — the poller reads the
        result from it and releases it on finalise.
        """
        workspace = self._config.workspace_directory
        repos = self._allowed_repos(workspace)
        scratch = await self._acquire_agent_scratch(item.id, repos, stable=True)
        run_dir = scratch or workspace
        clear_result(run_dir, item.id)
        activity.clear(workspace, item.id)

        # Write the full brief to a file and seed the session with a short prompt
        # (avoids passing a long, multi-line prompt through the shell).
        brief = self._build_brief(item, repos, autonomy=autonomy, draft_pr=draft_pr)
        brief_rel = f".autopilot/runs/{item.id}.brief.md"
        brief_path = Path(run_dir) / brief_rel
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text(brief, encoding="utf-8")

        session = f"autopilot-{item.id}"
        prompt = f"Read and follow the instructions in {brief_rel}, then write the result JSON as instructed there."
        # NOTE: keep the trailing positional prompt clear of any *variadic* flag
        # (e.g. --mcp-config <configs...>, --add-dir <dirs...>) — a variadic option
        # greedily swallows the prompt as one of its values. The interactive CLI
        # auto-discovers the workspace's .claude config from cwd, so MCP/skills load
        # without an explicit flag.
        cli_args = [
            "--remote-control", session,
            "--permission-mode", self._config.claude_permission_mode,
            prompt,
        ]

        try:
            if sys.platform == "win32":
                # `cmd /k` keeps the console window open (so any startup error is
                # visible instead of vanishing) and lets cmd resolve claude.cmd via
                # PATHEXT — passing the bare `claude` shim to Popen does not work.
                subprocess.Popen(  # noqa: ASYNC220 — fire-and-forget launch
                    ["cmd", "/k", "claude", *cli_args], cwd=run_dir,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            else:
                claude = shutil.which("claude") or "claude"
                subprocess.Popen([claude, *cli_args], cwd=run_dir, start_new_session=True)  # noqa: ASYNC220
            self._log.info(
                "launched interactive session", id=item.id, session=session,
                cwd=run_dir, isolated=bool(scratch),
            )
            return True, session, run_dir
        except Exception as exc:  # noqa: BLE001
            self._log.error("failed to launch interactive session", id=item.id, error=str(exc))
            await self.release_scratch(scratch)
            return False, session, workspace

    def finalize_interactive(self, item: WorkItemInfo, run_dir: str) -> ExecutionResult | None:
        """Map a live session's result if it has finished; ``None`` while running."""
        agent = read_result(run_dir, item.id)
        if agent is None:
            return None
        return self._result_from_agent(item, agent, self._config.autonomy_level)

    # A scratch with no result file and younger than this is assumed to belong to a
    # live interactive session (its own process survives an autopilot restart), so
    # startup pruning leaves it alone rather than yanking files out from under it.
    _ORPHAN_AGE_LIMIT_SECONDS = 24 * 3600

    async def prune_orphans(self) -> None:
        """Best-effort cleanup of scratch dirs + worktree registrations left by a crash.

        Called once on poller startup. Only removes a scratch we are confident is
        NOT a live interactive session — it already wrote its result, or it is very
        old — then prunes each repo's worktree registrations.
        """
        workspace = self._config.workspace_directory
        if not (workspace and self._config.use_worktrees):
            return
        base = Path(self._scratch_base())
        now = time.time()
        if base.is_dir():
            for sub in base.iterdir():
                if not sub.name.startswith("agent-"):
                    continue
                runs = sub / ".autopilot" / "runs"
                finished = runs.is_dir() and any(runs.glob("*.json"))
                try:
                    old = (now - sub.stat().st_mtime) > self._ORPHAN_AGE_LIMIT_SECONDS
                except OSError:
                    old = False
                if finished or old:
                    await self.release_scratch(str(sub))  # remove worktrees + dir safely
                else:
                    self._log.info("prune_orphans: keeping live/recent scratch", path=str(sub))
        for repo in discover_repos(workspace):
            src_repo = str(Path(workspace) / repo)
            async with self._repo_lock(src_repo):
                await self._git(["worktree", "prune"], src_repo, check=False)

    def _result_from_agent(self, item, agent, autonomy: str) -> ExecutionResult:
        if agent is None:
            self._log.warning("agent wrote no result file", id=item.id)
            return ExecutionResult.fail(
                item.id, "agent", "Agent produced no result file (.autopilot/runs/<id>.json)"
            )
        if agent.needs_human:
            self._log.info("agent escalated to human", id=item.id, reason=agent.reason)
            result = ExecutionResult.fail(item.id, "agent", agent.reason or "needs human input")
            result.needs_human = True
            result.output = agent.summary
            return result
        # report mode completes without a PR; otherwise a PR URL is required.
        if agent.is_completed and (agent.pr_url or autonomy == "report"):
            result = ExecutionResult.ok(item.id, "agent", agent.summary)
            result.pr_urls = [a.pr_url for a in agent.artifacts if a.pr_url]
            result.pr_url = result.pr_urls[0] if result.pr_urls else None
            if agent.artifacts:
                result.branch_name = agent.artifacts[0].branch or None
            return result
        reason = agent.reason or agent.summary or "no PR produced"
        self._log.warning("agent incomplete", id=item.id, status=agent.status, reason=reason)
        return ExecutionResult.fail(item.id, "agent", f"Agent did not complete: {reason}")

    def _build_brief(
        self, item: WorkItemInfo, repos: list[str], *, autonomy: str, draft_pr: bool
    ) -> str:
        """High-level brief: let Claude reason, pick repo(s) + skill(s), implement,
        open the PR(s), and report back via the structured result file."""
        result_rel = f".autopilot/runs/{item.id}.json"
        descs = parse_repo_descriptions(self._config.repo_descriptions)
        if repos:
            repo_list = "\n".join(
                f"- ./{r}" + (f" — {descs[r.lower()]}" if r.lower() in descs else "") for r in repos
            )
        else:
            repo_list = "(none discovered)"

        if autonomy == "report":
            action = (
                "Do NOT change code or open a PR. Analyse the item and post a short plan as a "
                "comment on the work item describing what you WOULD do."
            )
        elif autonomy == "unattended":
            action = "Implement it, then open a normal (non-draft) PR for each repo you change."
        else:  # assisted
            action = "Implement it, then open a DRAFT PR for each repo you change (human review)."

        lines = [
            "You are an autonomous engineer working in this workspace. Handle the Azure DevOps "
            "work item below end-to-end.",
            "",
            f"# Work item #{item.id}: {item.title}",
            f"Type: {item.work_item_type} | Category: {item.category}",
        ]
        if item.description:
            lines.append(f"\n## Description\n{item.description}")
        if item.acceptance_criteria:
            lines.append(f"\n## Acceptance criteria\n{item.acceptance_criteria}")
        if item.pending_comment:
            lines.append(
                "\n## ⚠️ Latest human guidance (highest priority — respond to THIS)\n"
                "A human left this comment as the most recent direction. Treat it as the "
                "top-priority instruction and let it override earlier assumptions where "
                f"they conflict:\n\n{item.pending_comment}"
            )
        lines += [
            "",
            "# Repositories you may edit (subfolders of this workspace)",
            repo_list,
            "Use the description of each repo to pick ONLY the one(s) this work actually needs — "
            "it may touch one or several. Leave infra/docs repos alone unless the task requires them. "
            "Do NOT edit anything outside these repos.",
            "",
            "# How to proceed",
            "- Reason about what this needs. If it is a large requirement, use your planning / "
            "task-generation skill to break it down first.",
            "- Choose and run the most appropriate skill(s) in this workspace — do not assume one.",
            "- You may use the Azure DevOps MCP to fetch more detail on the work item.",
            "- For EACH repo you change: start from a clean base branch, create a feature branch, "
            "commit, push, and open a pull request with the pr-create skill.",
            f"- {action}",
            "- Run a self-review (e.g. security-review / review-pr skill) before opening the PR.",
            "- If the acceptance criteria are ambiguous or you lack the information to proceed "
            "safely, do NOT guess — stop and report needs_human.",
            f"- {BOT_COMMENT_INSTRUCTION}",
            "",
            "# Required output (the control plane reads ONLY this — you MUST write it)",
            f"When finished, write a JSON file at `{result_rel}` (relative to this workspace):",
            '  {"status":"completed|failed|needs_human","summary":"<what you did>",',
            '   "artifacts":[{"repo":"<name>","branch":"<branch>","pr_url":"<url>"}],',
            '   "needs_human":false,"reason":"<why, if failed/needs_human>"}',
            "List EVERY PR you opened in artifacts. Set status=completed only if at least one PR "
            "was opened (or, in report mode, the plan was commented). Use needs_human=true with a "
            "clear reason when you need a human.",
        ]
        return "\n".join(lines)

    async def revise(
        self, item: WorkItemInfo, branch: str, prompt: str,
        draft_pr: bool = False, repo: str = "", allow_no_changes: bool = False,
        read_only: bool = False,
    ) -> ExecutionResult:
        """Address PR feedback on an EXISTING branch (push updates the open PR).

        ``repo`` is the repo the PR lives in (supplied by the babysitter). In workspace mode
        the checkout is ``<workspace>/<repo>``; without it we fall back to the legacy category
        mapping — see ``_revise_repo``. ``allow_no_changes`` (review-only commands) makes a
        run that produced no file changes count as SUCCESS — the agent reported via comment.
        ``read_only`` (advisory commands like /review) skips the checkout/worktree lifecycle
        entirely — the agent inspects ``origin/<branch>`` and comments, changing nothing."""
        repo_path, base_branch = self._revise_repo(item, repo)
        if read_only:
            return await self._run_read_only(item.id, repo_path, branch, prompt)
        return await self._run_in_workspace(
            item_id=item.id,
            repo=repo_path,
            branch=branch,
            base_branch=base_branch,
            prompt=prompt,
            commit_msg=f"fix(autopilot): address PR feedback (#{item.id})",
            draft_pr=draft_pr,
            existing_branch=True,
            create_pr=False,
            allow_no_changes=allow_no_changes,
        )

    async def _run_read_only(
        self, item_id: int, repo: str, branch: str, prompt: str
    ) -> ExecutionResult:
        """Run an advisory command (e.g. /review) with no checkout at all.

        The full revise pipeline pays fetch → worktree add (materialise the whole
        tree) → run → worktree remove/prune for a run that by contract changes
        nothing — most of the feedback latency for /review. Instead: fetch the PR
        branch so ``origin/<branch>`` is diffable, run Claude in place, done. The
        working tree stays on whatever it was; the prompt directs the agent to
        inspect the fetched ref / ADO PR tools, never the files on disk.
        """
        started = time.monotonic()
        # Only the fetch touches .git — serialise it against worktree bookkeeping,
        # then run Claude outside the lock so reviews don't queue behind long runs.
        async with self._repo_lock(repo):
            await self._git(["fetch", "origin", branch], repo, check=False)
        claude_cwd = self._config.workspace_directory or repo
        dirty_before = await self._git("status --porcelain", repo, check=False)
        self._log.info(
            "running claude (read-only, no checkout)", id=item_id, branch=branch, cwd=claude_cwd
        )
        try:
            claude_run = await self._run_claude(prompt, claude_cwd, repo=repo)
            result = ExecutionResult.ok(item_id, prompt, claude_run.text)
            result.cost_tokens = claude_run.total_tokens
            result.cost_usd = claude_run.cost_usd
        except TimeoutError:
            minutes = self._config.task_timeout_minutes
            self._log.error("read-only run timed out", id=item_id, minutes=minutes)
            result = ExecutionResult.fail(item_id, prompt, f"Timed out after {minutes} minutes")
        except Exception as exc:  # noqa: BLE001
            self._log.error("read-only run failed", id=item_id, error=str(exc))
            result = ExecutionResult.fail(item_id, prompt, str(exc))
        # There's no isolation to throw away here, so a run that ignored the
        # "change nothing" contract would silently dirty the checkout — surface it.
        dirty_after = await self._git("status --porcelain", repo, check=False)
        if dirty_after.strip() != dirty_before.strip():
            self._log.warning(
                "read-only run modified the checkout — leaving files untouched",
                id=item_id, repo=repo,
            )
        result.branch_name = branch
        result.duration_seconds = time.monotonic() - started
        return result

    async def run_loop(
        self, name: str, prompt: str, repo: str, base_branch: str, branch: str, draft_pr: bool
    ) -> ExecutionResult:
        """Run a scheduled loop's prompt on a fresh branch and open a PR."""
        return await self._run_in_workspace(
            item_id=0,
            repo=repo,
            branch=branch,
            base_branch=base_branch,
            prompt=prompt,
            commit_msg=f"chore(autopilot): {name}",
            draft_pr=draft_pr,
            existing_branch=False,
            create_pr=True,
        )

    # ── shared core ─────────────────────────────────────────────────────────

    async def _run_in_workspace(
        self,
        *,
        item_id: int,
        repo: str,
        branch: str,
        base_branch: str,
        prompt: str,
        commit_msg: str,
        draft_pr: bool,
        existing_branch: bool,
        create_pr: bool,
        allow_no_changes: bool = False,
    ) -> ExecutionResult:
        # Workspace mode checks the branch out IN-PLACE in the shared repo working tree, so
        # two concurrent runs on the SAME repo would stomp each other and corrupt the index
        # ("fatal: unable to write new index file"). Serialise the whole run per-repo — the
        # same lock the worktree bookkeeping uses. Worktree mode already isolates each run in
        # its own worktree (own index) and needs no lock (lock stays None).
        lock = self._repo_lock(repo) if self._config.workspace_directory else None
        if lock is not None:
            await lock.acquire()
        started = time.monotonic()
        workspace: _Workspace | None = None
        try:
            workspace = await self._acquire_workspace(
                repo, branch, base_branch, item_id, existing_branch
            )
            work_dir = workspace.path  # git operates here (the repo checkout)
            claude_cwd = workspace.claude_cwd or work_dir  # Claude runs here

            self._log.info("running claude", id=item_id, branch=branch, cwd=claude_cwd)
            claude_run = await self._run_claude(prompt, claude_cwd, repo=work_dir)

            if not await self._has_changes(work_dir):
                if allow_no_changes:
                    # Review-only: the agent reported its findings via a comment; making no
                    # code change is the expected, successful outcome.
                    result = ExecutionResult.ok(item_id, prompt, claude_run.text)
                else:
                    self._log.warning("no file changes after run", id=item_id, branch=branch)
                    result = ExecutionResult.fail(item_id, prompt, "No file changes produced")
                result.branch_name = branch
                result.duration_seconds = time.monotonic() - started
                result.cost_tokens = claude_run.total_tokens
                result.cost_usd = claude_run.cost_usd
                return result

            changed_files = await self._changed_files(work_dir)
            await self._git("add -A", work_dir)
            await self._git(["commit", "-m", commit_msg], work_dir)
            # A fresh execution rebuilds the branch from base, so a stale remote
            # branch from a prior (failed/retried) run would reject a plain push as
            # non-fast-forward. The autopilot owns these feature branches and fully
            # regenerates them, so force-overwrite. Revising an existing PR branch
            # builds on top of the remote, so it pushes normally.
            push_args = ["push", "-u", "origin", branch]
            if not existing_branch:
                push_args.append("--force")
            await self._git(push_args, work_dir)

            review = await self._reviewer.review(work_dir)
            if not review.passed:
                self._log.warning(
                    "auto-review blocked PR", id=item_id, issues=len(review.critical_issues)
                )
                result = ExecutionResult.fail(
                    item_id,
                    prompt,
                    "Auto-review blocked: " + "; ".join(review.critical_issues[:3]),
                )
                result.branch_name = branch
                result.files_changed = changed_files
                result.duration_seconds = time.monotonic() - started
                result.cost_tokens = claude_run.total_tokens
                return result

            pr_run = None
            if create_pr:
                self._log.info("creating PR", id=item_id, draft=draft_pr)
                pr_flag = " --draft" if draft_pr else ""
                pr_run = await self._run_claude(f"/pr-create{pr_flag}", work_dir)

            runs = [claude_run] + ([pr_run] if pr_run else [])
            result = ExecutionResult.ok(item_id, prompt, claude_run.text)
            result.branch_name = branch
            result.files_changed = changed_files
            result.duration_seconds = time.monotonic() - started
            result.cost_tokens = sum(r.total_tokens for r in runs)
            result.cost_usd = _sum_cost(*runs)
            result.pr_url = _extract_pr_url(pr_run.text) if pr_run else None
            result.pr_urls = [result.pr_url] if result.pr_url else []
            return result

        except TimeoutError:
            minutes = self._config.task_timeout_minutes
            self._log.error("execution timed out", id=item_id, minutes=minutes)
            result = ExecutionResult.fail(item_id, prompt, f"Timed out after {minutes} minutes")
            result.duration_seconds = time.monotonic() - started
            return result
        except Exception as exc:  # noqa: BLE001
            self._log.error("execution failed", id=item_id, error=str(exc))
            result = ExecutionResult.fail(item_id, prompt, str(exc))
            result.duration_seconds = time.monotonic() - started
            return result
        finally:
            if workspace is not None:
                await self._release_workspace(workspace)
            if lock is not None:
                lock.release()

    # ── workspace lifecycle ─────────────────────────────────────────────────

    async def _acquire_workspace(
        self, repo: str, branch: str, base_branch: str, item_id: int, existing_branch: bool = False
    ) -> _Workspace:
        """Create an isolated checkout for one execution.

        With ``use_worktrees`` (default) each execution gets its own ``git
        worktree`` so concurrent items never collide on a shared checkout. When
        disabled, falls back to an in-place ``checkout`` in the repo.

        ``existing_branch=True`` checks out an already-pushed branch (used to
        revise an open PR) instead of branching from ``base_branch``.
        """
        if existing_branch:
            await self._git(["fetch", "origin", branch], repo, check=False)
        from_ref = f"origin/{branch}" if existing_branch else f"origin/{base_branch}"

        # Workspace mode: Claude runs from the workspace root (so it sees the
        # shared .claude skills/rules/MCP) and edits the repo subfolder in place.
        # Worktrees aren't usable here — they live outside the workspace tree, so
        # Claude (cwd=workspace) couldn't reach them as a subfolder.
        if self._config.workspace_directory:
            self._log.info(
                "creating branch in-place (workspace mode)",
                id=item_id,
                branch=branch,
                repo=repo,
                workspace=self._config.workspace_directory,
            )
            await self._git(["fetch", "origin", base_branch], repo, check=False)
            await self._git(["reset", "--hard"], repo, check=False)
            await self._git(["clean", "-fd"], repo, check=False)
            await self._git(["checkout", "-B", branch, from_ref], repo)
            return _Workspace(
                repo, repo, branch, base_branch,
                is_worktree=False, claude_cwd=self._config.workspace_directory,
            )

        if self._config.use_worktrees:
            base_dir = self._config.worktrees_dir or str(
                Path(tempfile.gettempdir()) / "ai-autopilot-worktrees"
            )
            Path(base_dir).mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - fast local mkdir
            if existing_branch and self._config.revise_worktree_reuse:
                return await self._acquire_revise_worktree(
                    repo, branch, base_branch, item_id, base_dir, from_ref
                )
            # A cached revise worktree may still hold this branch checked out — a fresh
            # `worktree add -B` would then fail ("already checked out"). Evict it first.
            await self._evict_revise_worktree(repo, branch, item_id, base_dir)
            # Name the worktree dir by a short GUID (not the branch) to keep the
            # path short — long branch names otherwise blow past Windows MAX_PATH.
            # The real branch is still created via ``-B branch`` below.
            path = str(Path(base_dir) / f"{item_id}-{uuid.uuid4().hex[:8]}")
            self._log.info("creating worktree", id=item_id, branch=branch, path=path)
            await self._git(["worktree", "add", "-B", branch, path, from_ref], repo)
            return _Workspace(repo, path, branch, base_branch, is_worktree=True)

        self._log.info("creating branch in-place", id=item_id, branch=branch, repo=repo)
        await self._git(["checkout", "-B", branch, from_ref], repo)
        return _Workspace(repo, repo, branch, base_branch, is_worktree=False)

    def _revise_worktree_path(self, base_dir: str, item_id: int, repo: str, branch: str) -> str:
        """Deterministic cache path for one (item, branch) revise worktree — a follow-up
        command (or a restarted process) finds the previous checkout by recomputing it.
        Short hash name, not the branch, to stay inside Windows MAX_PATH."""
        digest = hashlib.sha1(f"{repo}|{branch}".encode()).hexdigest()[:8]
        return str(Path(base_dir) / f"r{item_id}-{digest}")

    async def _acquire_revise_worktree(
        self, repo: str, branch: str, base_branch: str, item_id: int,
        base_dir: str, from_ref: str,
    ) -> _Workspace:
        """Reuse ONE cached worktree per (item, branch) across /ai rounds.

        The full add→materialise→remove cycle is most of a follow-up's latency; the
        cache skips it and keeps ignored build artefacts (node_modules, bin/obj) warm.
        Each round hard-resets to the freshly fetched PR head and `clean -fd`s stray
        untracked files (ignored ones survive), so state never leaks between rounds."""
        await self._sweep_stale_revise_worktrees(base_dir, repo)
        path = self._revise_worktree_path(base_dir, item_id, repo, branch)
        if Path(path).is_dir():
            try:
                await self._git(["checkout", "-B", branch, from_ref], path)
                await self._git(["clean", "-fd"], path, check=False)
                self._log.info("reusing revise worktree", id=item_id, branch=branch, path=path)
                return _Workspace(repo, path, branch, base_branch, is_worktree=True, keep=True)
            except GitError:
                # Stale/corrupt (pruned registration, half-deleted dir…) → rebuild.
                self._log.warning("cached worktree unusable — recreating", id=item_id, path=path)
                await self._evict_revise_worktree(repo, branch, item_id, base_dir)
        self._log.info("creating revise worktree", id=item_id, branch=branch, path=path)
        async with self._repo_lock(repo):
            await self._git(["worktree", "prune"], repo, check=False)  # heal stale registrations
            await self._git(["worktree", "add", "-B", branch, path, from_ref], repo)
        return _Workspace(repo, path, branch, base_branch, is_worktree=True, keep=True)

    async def _evict_revise_worktree(
        self, repo: str, branch: str, item_id: int, base_dir: str
    ) -> None:
        """Drop the cached worktree for (item, branch) if present (best-effort)."""
        path = self._revise_worktree_path(base_dir, item_id, repo, branch)
        if not Path(path).exists():
            return
        async with self._repo_lock(repo):
            await self._git(["worktree", "remove", "--force", path], repo, check=False)
            shutil.rmtree(path, ignore_errors=True)
            await self._git(["worktree", "prune"], repo, check=False)

    async def _sweep_stale_revise_worktrees(self, base_dir: str, repo: str) -> None:
        """Remove cached revise worktrees idle past the TTL (best-effort).

        Cached dirs are ``r<item>-<hash>`` (touched on every release); fresh-execution
        worktrees are ``<item>-<uuid>`` and never linger, so only ``r*`` is swept."""
        cutoff = time.time() - self._config.revise_worktree_ttl_hours * 3600
        try:
            stale = [
                p for p in Path(base_dir).iterdir()
                if p.is_dir() and p.name.startswith("r") and p.stat().st_mtime < cutoff
            ]
        except OSError:
            return
        for p in stale:
            self._log.info("sweeping stale revise worktree", path=str(p))
            async with self._repo_lock(repo):
                await self._git(["worktree", "remove", "--force", str(p)], repo, check=False)
            shutil.rmtree(p, ignore_errors=True)
        if stale:
            async with self._repo_lock(repo):
                await self._git(["worktree", "prune"], repo, check=False)

    async def _release_workspace(self, ws: _Workspace) -> None:
        """Tear down a workspace (best-effort; never raises)."""
        if ws.is_worktree:
            if ws.keep:
                # Cached for the next /ai round on this branch; the branch stays
                # checked out here. Touch the dir so the TTL sweep sees it as live.
                try:
                    os.utime(ws.path)
                except OSError:
                    pass
                return
            await self._git(["worktree", "remove", "--force", ws.path], ws.repo, check=False)
            shutil.rmtree(ws.path, ignore_errors=True)
            # The pushed branch lives on origin for the PR; drop the local copy.
            await self._git(["branch", "-D", ws.branch], ws.repo, check=False)
            await self._git(["worktree", "prune"], ws.repo, check=False)
        else:
            # Restore the repo to its base branch for the next run.
            await self._git(f"checkout {ws.base_branch}", ws.repo, check=False)

    async def _run_claude(
        self, prompt: str, work_dir: str, repo: str | None = None, on_event=None
    ) -> ClaudeRun:
        setting_sources: list[str] | None = None
        mcp_servers: dict | None = None
        add_dirs: list[str] | None = None
        if self._config.workspace_directory:
            # Opt into loading the workspace's .claude skills/rules/settings, its
            # MCP servers, and grant access to the target repo subfolder.
            setting_sources = ["user", "project", "local"]
            mcp_servers = _load_mcp_servers(self._config.workspace_directory)
            if repo:
                add_dirs = [repo]
        return await run_claude(
            prompt,
            work_dir,
            timeout_seconds=self._config.task_timeout_minutes * 60,
            model=self._config.claude_model or None,
            max_turns=self._config.claude_max_turns or None,
            permission_mode=self._config.claude_permission_mode,  # type: ignore[arg-type]
            allowed_tools=self._config.claude_allowed_tools or None,
            setting_sources=setting_sources,
            mcp_servers=mcp_servers,
            add_dirs=add_dirs,
            on_event=on_event,
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

    def _revise_repo(self, item: WorkItemInfo, repo_name: str) -> tuple[str, str]:
        """Resolve the checkout dir for a PR-feedback revise.

        In workspace mode (multi-repo) the PR's repo lives at ``<workspace>/<repo_name>``
        — use it directly. This is what makes revise work when several repos share one
        workspace; the legacy ``_resolve_repo`` can't tell which repo a PR belongs to and
        falls back to the single (often unset) ``repo_working_directory``, crashing git
        with a bad cwd. Fall back to the legacy mapping only when no repo name is given
        or its folder is missing."""
        ws = self._config.workspace_directory
        if ws and repo_name:
            path = Path(ws) / repo_name
            if path.is_dir():
                return str(path), self._config.base_branch
            self._log.warning(
                "revise: repo folder not found in workspace — falling back",
                repo=repo_name, workspace=ws,
            )
        return self._resolve_repo(item)


# Cap the title slug so branch names (and thus the Windows worktree path) stay
# short — long titles otherwise blow past the 260-char MAX_PATH limit on Windows.
_MAX_SLUG_LEN = 40


def _repo_name(repo: str, workspace: str) -> str:
    """Repo directory name relative to the workspace (e.g. 'Backend-Fresh')."""
    try:
        return Path(repo).relative_to(Path(workspace)).as_posix()
    except ValueError:
        return Path(repo).name


def _load_mcp_servers(workspace: str) -> dict | None:
    """Read MCP server config from ``<workspace>/.claude/mcp.json`` if present.

    Returned as a dict the Agent SDK can consume. Best-effort: any error (missing
    file, bad JSON) yields ``None`` and the run proceeds without MCP.
    """
    path = Path(workspace) / ".claude" / "mcp.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers or None


def _branch_name(item: WorkItemInfo) -> str:
    prefix = _BRANCH_PREFIX.get(item.category, "feature")
    return f"{prefix}/{item.id}-{_slugify(item.title)}"


def _slugify(text: str) -> str:
    # Transliterate Vietnamese/Unicode diacritics to ASCII (e.g. "Ràng buộc" →
    # "rang buoc") so the slug — and the Windows worktree path — stays short and
    # path-safe, then cap the length to stay well under the 260-char MAX_PATH.
    decomposed = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    ascii_text = ascii_text.replace("đ", "d")
    cleaned = "".join(c if (c.isalnum() and c.isascii()) or c in " -" else " " for c in ascii_text)
    slug = re.sub(r"-+", "-", re.sub(r"\s+", "-", cleaned)).strip("-")
    return slug[:_MAX_SLUG_LEN].rstrip("-")


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
