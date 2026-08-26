"""Orchestrate a work item end-to-end with Claude (ported from ``ClaudeExecutor``).

Flow: create feature branch → run the routed skill via the Claude Agent SDK →
verify file changes → commit + push → auto-review → create PR. Unlike the legacy
.NET version this no longer parses CLI stdout for token counts; it reads them
from the SDK's structured ``ResultMessage``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_autopilot import activity, lessons, policy
from ai_autopilot.config import (
    AGENT_CONDUCT_INSTRUCTION,
    BOT_COMMENT_INSTRUCTION,
    Settings,
)
from ai_autopilot.execution.auto_reviewer import AutoReviewer
from ai_autopilot.execution.test_gate import TestGate
from ai_autopilot.execution.claude_client import ClaudeRun, apply_usage, run_claude
from ai_autopilot.execution.result_contract import (
    batch_key,
    clear_result,
    find_result,
    parse_result_text,
)
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, TaskCategory, WorkItemInfo
from ai_autopilot.workspace import discover_repos, parse_repo_descriptions

_log = get_logger("execution.claude_executor")

# Tools an advisory run (/review, /qc, /security, auto-review) must not have. These runs
# have no worktree to discard, so they execute against the SHARED workspace checkout — a
# stray edit would land in whatever branch happens to be checked out, for a run whose whole
# contract is to change nothing. Denying the mutators makes that structural instead of a
# prompt instruction, on the path that runs unattended.
#
# Deliberately NOT here: Bash (the review needs `git diff`) and the MCP calls that post the
# findings to the PR — those ARE the job. So this narrows the blast radius; it is not a
# sandbox, which is why the caller still diffs `git status` around the run.
_READ_ONLY_DENY = ("Write", "Edit", "MultiEdit", "NotebookEdit")

_BRANCH_PREFIX = {
    TaskCategory.BUG: "fix",
    TaskCategory.FRONTEND_TASK: "feature/fe",
    TaskCategory.BACKEND_TASK: "feature/be",
}


class GitError(RuntimeError):
    """Raised when a git command exits non-zero."""


def pretrust_claude_dir(path: str) -> bool:
    """Pre-accept Claude Code's workspace-trust dialog for ``path``.

    Claude Code keys trust by *absolute path* in ``~/.claude.json``
    (``projects["<dir>"].hasTrustDialogAccepted``) and it does **not** inherit
    from a parent directory. Every scratch we build is a brand-new path, so:

    * an interactive session stops at "Do you trust the files in this folder?"
      before it does anything — a prompt ``--permission-mode bypassPermissions``
      does *not* answer, because workspace trust is a separate gate evaluated
      first. An unattended Remote-Control session just sits there;
    * a headless run silently treats the scratch as untrusted origin, which
      drops the hooks/settings of the ``.claude`` we copied into it.

    Seeding the flag is the alternative Claude Code itself documents ("...or set
    projects[...].hasTrustDialogAccepted: true"). We only ever do this for a
    directory we just created ourselves out of the operator's own workspace.

    Best-effort: never raises — worst case is the old behaviour. Returns
    ``True`` when the config now marks ``path`` as trusted.
    """
    cfg = Path.home() / ".claude.json"
    try:
        key = str(Path(path).resolve()).replace("\\", "/")
        data = json.loads(cfg.read_text(encoding="utf-8"))
        entry = data.setdefault("projects", {}).setdefault(key, {})
        if entry.get("hasTrustDialogAccepted") is True:
            return True
        entry["hasTrustDialogAccepted"] = True
        entry.setdefault("hasCompletedProjectOnboarding", True)
        # Temp file + atomic replace: a live Claude Code session rewrites this
        # same config, so it must never be observed half-written. UTF-8 with no
        # BOM — a BOM makes the CLI's JSON.parse fail.
        tmp = cfg.with_name(f".claude.json.autopilot-{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, cfg)
        _log.info("pre-trusted Claude workspace", path=key)
        return True
    except Exception as exc:  # noqa: BLE001 — never block a launch on this
        _log.warning("could not pre-trust Claude workspace", path=path, error=str(exc))
        return False


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
    resume_from: str = ""  # conversation to continue (interactive session's transcript)


# The settings the CURRENT run sees — the root config with its work item's workspace
# overrides applied (``Settings.scoped_for_project``). A ContextVar rather than an
# argument because ~40 places inside this class read ``self._config``, and threading a
# config parameter through all of them would be a large, error-prone diff whose failure
# mode is silent: ONE missed site reads the root workspace and the run edits the wrong
# repository. Scoping at the entry points instead means every read is correct by
# construction. Per-task isolation is what makes this safe under ``max_concurrent`` > 1:
# ``asyncio.create_task`` copies the context, so two runs in different workspaces cannot
# observe each other's scope.
_run_settings: ContextVar[Settings | None] = ContextVar("autopilot_run_settings", default=None)


def _project_of(target: Any) -> str:
    """The ADO project an entry point's first argument belongs to ("" if unknown)."""
    if isinstance(target, WorkItemInfo):
        return target.project
    if isinstance(target, list):
        return next((i.project for i in target if isinstance(i, WorkItemInfo) and i.project), "")
    return str(target or "")


def _scoped(method):
    """Entry point decorator: run ``method`` under its work item's workspace settings.

    Applied to every public method that starts a piece of agent work, so callers
    (poller, SDLC engine, PR babysitter) need to know nothing about workspaces."""

    @functools.wraps(method)
    async def wrapper(self, first, *args, **kwargs):
        with self.workspace_scope(_project_of(first)):
            return await method(self, first, *args, **kwargs)

    return wrapper


def _scoped_sync(method):
    """:func:`_scoped` for a synchronous entry point."""

    @functools.wraps(method)
    def wrapper(self, first, *args, **kwargs):
        with self.workspace_scope(_project_of(first)):
            return method(self, first, *args, **kwargs)

    return wrapper


class ClaudeExecutor:
    def __init__(
        self, config: Settings, reviewer: AutoReviewer, session_repo=None
    ) -> None:
        self._root_config = config
        self._reviewer = reviewer
        self._test_gate = TestGate(config)  # runs the repo's tests before a PR (opt-in)
        self._session_repo = session_repo  # ClaudeSessionRepository | None
        self._log = get_logger("execution.claude_executor")
        # Serialise git worktree bookkeeping per source repo: two concurrent tasks
        # touching the SAME repo must not run `worktree add`/`prune` at once (they
        # write the same .git and would collide on locks). Keyed by repo path.
        self._repo_locks: dict[str, asyncio.Lock] = {}

    @property
    def _config(self) -> Settings:
        """Settings for the run in progress — the current workspace's view, or the root
        config outside any run. Every read in this class goes through here."""
        return _run_settings.get() or self._root_config

    @contextlib.contextmanager
    def workspace_scope(self, project: str):
        """Bind the settings for work in ``project`` for the duration of the block.

        No-op when the project has no workspace of its own (``scoped_for_project``
        hands back the same object), which is the single-workspace case."""
        scoped = self._root_config.scoped_for_project(project)
        if scoped is self._root_config:
            yield
            return
        token = _run_settings.set(scoped)
        self._log.info(
            "run scoped to workspace", project=project,
            workspace=scoped.workspace_directory, base_branch=scoped.base_branch,
        )
        try:
            yield
        finally:
            _run_settings.reset(token)

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

    @_scoped
    async def execute(
        self, item: WorkItemInfo, skill_command: str, draft_pr: bool = False
    ) -> ExecutionResult:
        """Implement a work item on a fresh branch and open a PR."""
        repo, base_branch = self._resolve_repo(item)
        prompt = self._build_prompt(item, skill_command, repo)
        workspace = self._config.workspace_directory
        injected = self._lessons_injected([_repo_name(repo, workspace)] if workspace else [])
        result = await self._run_in_workspace(
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
        result.lessons_injected = injected
        return result

    def _lessons_injected(self, repos: list[str]) -> int:
        """How many past lessons the brief just carried.

        Counts the SAME list the brief was built from, so History's 🧠 badge can never
        claim the agent was warned about something it never saw.
        """
        workspace = self._config.workspace_directory
        if not self._config.learning_loop_enabled or not workspace:
            return 0
        return len(
            lessons.recent(workspace, repos, limit=self._config.lessons_max_injected)
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
        if self._config.learning_loop_enabled:
            past = lessons.lessons_brief(
                self._config.workspace_directory, [repo_name],
                limit=self._config.lessons_max_injected,
            )
            if past:
                parts.append(past)
        parts.append(f"\nNow run this skill: {skill_command}")
        return "\n".join(parts)

    # ── AI-native flow (Phase 1): control plane + agent + structured contract ──

    @_scoped
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

        # Preflight. Without a workspace there is no repo to edit and no `.claude`
        # to load (setting_sources stays off), so the run cannot succeed — and it
        # fails LATE and confusingly, as "agent produced no result file" after a
        # full timeout. Say what is actually wrong, before spending a run on it.
        misconfig = self._preflight(autonomy)
        if misconfig:
            self._log.error("agent preflight failed", id=item.id, error=misconfig)
            result = ExecutionResult.fail(item.id, "agent", misconfig)
            result.duration_seconds = time.monotonic() - started
            return result

        repos = self._allowed_repos(workspace)
        injected = self._lessons_injected(repos)
        # Isolate this task in its own worktree scratch so it never touches the
        # user's main checkout; None → run in the shared workspace (disabled / no repos).
        scratch = await self._acquire_agent_scratch(item.id, repos)
        run_dir = scratch or workspace
        # Trust the scratch so the .claude config we copied into it (hooks,
        # settings) is actually honoured instead of skipped as untrusted origin.
        pretrust_claude_dir(run_dir)
        try:
            clear_result(run_dir, item.id)
            activity.clear(workspace, item.id)  # activity stays in the real workspace (dashboard reads it)
            isolated = " (isolated worktree)" if scratch else ""
            activity.append(
                workspace, item.id, f"🚀 agent started{isolated} — repos: {', '.join(repos) or '-'}"
            )
            brief = self._build_brief(item, repos, autonomy=autonomy, draft_pr=draft_pr)
            if injected:
                activity.append(
                    workspace, item.id, f"🧠 {injected} lesson(s) from past runs injected"
                )
            self._log.info("running agent", id=item.id, cwd=run_dir, repos=repos, isolated=bool(scratch))
            claude_run = await self._run_claude(
                brief, run_dir, on_event=lambda line: activity.append(workspace, item.id, line)
            )
            activity.append(workspace, item.id, "✅ agent run finished")
            agent = find_result(run_dir, item.id)
            if agent is None:
                # The work may well be done (branch pushed, PR open) and only the
                # bookkeeping missed — recover the envelope from the run's output
                # rather than discarding the whole run.
                agent = parse_result_text(claude_run.text)
                if agent is not None:
                    self._log.warning("recovered result from agent output", id=item.id)
                    activity.append(
                        workspace, item.id, "⚠️ result file missing — recovered from agent output"
                    )
            result = self._result_from_agent(item, agent, autonomy, run_text=claude_run.text)
            apply_usage(result, claude_run)
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
        result.lessons_injected = injected
        return result

    @_scoped
    async def run_agent_batch(
        self, items: list[WorkItemInfo], *, autonomy: str, draft_pr: bool
    ) -> dict[int, ExecutionResult]:
        """Handle a cluster of linked work items in ONE agent run, one PR each.

        The scheduler's alternative is to serialise the cluster into separate
        waves: each run then re-reads the same code from scratch, and the second
        run lands on a base that no longer matches what it read. Here a single
        run holds all of them in context and produces one branch + one PR **per
        work item**, so the review and merge units stay per-item — which is what
        the board, the retry budget and the ADO states are keyed on.

        Returns ``{work_item_id: ExecutionResult}`` — one per input item, always,
        so the caller's per-item bookkeeping is unchanged. ``items`` is taken in
        the order the PRs should stack (predecessors first).
        """
        workspace = self._config.workspace_directory
        started = time.monotonic()
        lead = items[0]
        ids = [i.id for i in items]

        misconfig = self._preflight(autonomy)
        if misconfig:
            self._log.error("batch preflight failed", ids=ids, error=misconfig)
            return self._batch_fail(items, misconfig, started)

        repos = self._allowed_repos(workspace)
        injected = self._lessons_injected(repos)
        scratch = await self._acquire_agent_scratch(lead.id, repos)
        run_dir = scratch or workspace
        pretrust_claude_dir(run_dir)
        key = batch_key(lead.id)
        # The dashboard's activity feed is per item, and a batch is one run — mirror
        # every line onto ALL members so none of them looks stalled while it runs.
        def _feed(line: str) -> None:
            for iid in ids:
                activity.append(workspace, iid, line)

        try:
            clear_result(run_dir, key)
            for iid in ids:
                activity.clear(workspace, iid)
            isolated = " (isolated worktree)" if scratch else ""
            _feed(
                f"🧺 batch started{isolated} — {len(items)} linked items: "
                + ", ".join(f"#{i}" for i in ids)
            )
            brief = self._build_batch_brief(items, repos, autonomy=autonomy, draft_pr=draft_pr)
            if injected:
                _feed(f"🧠 {injected} lesson(s) from past runs injected")
            self._log.info(
                "running agent batch", ids=ids, cwd=run_dir, repos=repos, isolated=bool(scratch)
            )
            claude_run = await self._run_claude(brief, run_dir, on_event=_feed)
            _feed("✅ batch run finished")
            agent = find_result(run_dir, key) or parse_result_text(claude_run.text)
            results = self._results_from_batch(
                items, agent, autonomy, run_text=claude_run.text
            )
            # One run, one bill — charge it to the lead so the totals stay honest
            # instead of being multiplied by the number of items.
            apply_usage(results[lead.id], claude_run)
        except TimeoutError:
            mins = self._config.task_timeout_minutes
            self._log.error("agent batch timed out", ids=ids, minutes=mins)
            return self._batch_fail(items, f"Timed out after {mins} minutes", started)
        except Exception as exc:  # noqa: BLE001 — never leave the items stuck IN_PROGRESS
            self._log.error("agent batch crashed", ids=ids, error=str(exc))
            return self._batch_fail(items, str(exc), started)
        finally:
            await self.release_scratch(scratch)

        elapsed = time.monotonic() - started
        for res in results.values():
            res.duration_seconds = elapsed
            res.lessons_injected = injected
        return results

    def _batch_fail(
        self, items: list[WorkItemInfo], error: str, started: float
    ) -> dict[int, ExecutionResult]:
        """Same failure on every member — the batch is one failure domain."""
        elapsed = time.monotonic() - started
        out: dict[int, ExecutionResult] = {}
        for item in items:
            res = ExecutionResult.fail(item.id, "agent-batch", error)
            res.duration_seconds = elapsed
            out[item.id] = res
        return out

    def _results_from_batch(
        self, items: list[WorkItemInfo], agent, autonomy: str, run_text: str = ""
    ) -> dict[int, ExecutionResult]:
        """Split ONE agent result into a per-work-item result, by ``work_item_id``.

        An item is only successful if a PR was attributed to *it* — a batch that
        opened two of three PRs must not mark the third done just because the run
        as a whole said "completed".
        """
        if agent is None:
            last = _last_words(run_text)
            path = f".autopilot/runs/{batch_key(items[0].id)}.json"
            reason = f"Batch produced no result file ({path})"
            if last:
                reason += f" — agent's last words: {last}"
            out = self._batch_fail(items, reason, time.monotonic())
            for res in out.values():
                res.output = run_text
            return out

        by_item: dict[int, list] = {i.id: [] for i in items}
        unattributed = []
        for art in agent.artifacts:
            if art.work_item_id in by_item:
                by_item[art.work_item_id].append(art)
            else:
                unattributed.append(art)
        # A single-item batch needs no attribution to be unambiguous.
        if len(items) == 1:
            by_item[items[0].id].extend(unattributed)
            unattributed = []
        if unattributed:
            self._log.warning(
                "batch artifacts with no work_item_id",
                ids=[i.id for i in items],
                count=len(unattributed),
            )

        out: dict[int, ExecutionResult] = {}
        for item in items:
            arts = [a for a in by_item[item.id] if a.pr_url]
            if agent.needs_human and not arts:
                res = ExecutionResult.fail(
                    item.id, "agent-batch", agent.reason or "needs human input"
                )
                res.needs_human = True
                res.output = agent.summary
            elif arts or (agent.is_completed and autonomy == "report"):
                res = ExecutionResult.ok(item.id, "agent-batch", agent.summary)
                res.pr_urls = [a.pr_url for a in arts]
                res.pr_url = res.pr_urls[0] if res.pr_urls else None
                res.branch_name = arts[0].branch if arts else None
            else:
                why = agent.reason or agent.summary or "no PR produced"
                res = ExecutionResult.fail(
                    item.id, "agent-batch", f"Batch opened no PR for #{item.id}: {why}"
                )
                res.output = agent.summary
            out[item.id] = res
        return out

    def _build_batch_brief(
        self, items: list[WorkItemInfo], repos: list[str], *, autonomy: str, draft_pr: bool
    ) -> str:
        """Brief for a batched run: N items, N branches, N PRs, one result file."""
        lead = items[0]
        key = batch_key(lead.id)
        result_rel = f".autopilot/runs/{key}.json"
        stacked = self._config.batch_stacked_prs
        base = self._config.base_branch or "main"
        draft = " Open every PR as a DRAFT." if draft_pr else ""

        if stacked:
            branching = (
                f"- STACKED branches. Work the items IN THE ORDER LISTED. The first item's "
                f"branch starts from `{base}` and its PR targets `{base}`. EACH later item "
                "branches off the PREVIOUS item's branch and its PR TARGETS that previous "
                "branch — never the base. This is what keeps items that touch the same files "
                "from conflicting, and it means the PRs must be merged in the listed order."
            )
        else:
            branching = (
                f"- INDEPENDENT branches. Every item's branch starts from `{base}` and its PR "
                f"targets `{base}`. Keep each item's diff to its own files so the PRs can be "
                "merged in any order; if two items genuinely need the same edit, put it in the "
                "FIRST item and say so in the second PR's description."
            )

        listing = []
        for n, it in enumerate(items, 1):
            listing.append(f"\n## {n}. Work item #{it.id}: {it.title}")
            listing.append(f"Type: {it.work_item_type} | Category: {it.category}")
            if it.description:
                listing.append(f"Description:\n{it.description}")
            if it.acceptance_criteria:
                listing.append(f"Acceptance criteria:\n{it.acceptance_criteria}")
            if it.pending_comment:
                listing.append(
                    "⚠️ Latest human guidance (highest priority for this item):\n"
                    f"{it.pending_comment}"
                )

        descs = parse_repo_descriptions(self._config.repo_descriptions)
        repo_list = "\n".join(
            f"- ./{r}" + (f" — {descs[r.lower()]}" if r.lower() in descs else "") for r in repos
        ) or "(none discovered)"

        lines = [
            "You are an autonomous engineer working in this workspace. These "
            f"{len(items)} Azure DevOps work items are LINKED — they touch the same area, "
            "which is why they are given to you together instead of one at a time.",
            "",
            "Read them as a set and decide the shared design ONCE. But deliver them "
            "SEPARATELY: each work item gets its OWN branch and its OWN pull request, "
            "because each is reviewed, merged and tracked on its own.",
            "\n".join(listing),
        ]
        if self._config.learning_loop_enabled:
            past = lessons.lessons_brief(
                self._config.workspace_directory, repos,
                limit=self._config.lessons_max_injected,
            )
            if past:
                lines.append(past)
        lines += [
            "",
            "# Repositories you may edit (subfolders of this workspace)",
            repo_list,
            "Do NOT edit anything outside these repos.",
            "",
            "# How to deliver",
            "- ONE branch and ONE pull request PER WORK ITEM. Never put two work items in one "
            "PR, and never open a PR that leaves an item half-done.",
            branching,
            "- Name each branch after its own item (e.g. `feature/be/<id>-<slug>`), and put the "
            "item id in the PR title so a reviewer can tell them apart.",
            "- Commit each item's work separately, even while they share this one worktree: "
            "stage only that item's files before committing to its branch.",
            "- If an item turns out to need NO change (already covered by another item in this "
            "set), open no PR for it and say so in its `reason` — do not invent a change.",
            f"-{draft}" if draft else "- Open normal (non-draft) PRs.",
            "- Run a self-review (security-review / review-pr skill) before opening each PR.",
            "- Work autonomously and DECISIVELY: do not ask clarifying questions. Choose the "
            "most reasonable reading, note the assumption in the PR, keep going.",
            f"- {BOT_COMMENT_INSTRUCTION}",
            AGENT_CONDUCT_INSTRUCTION,
            "",
            "# Required output (the control plane reads ONLY this — you MUST write it)",
            f"When finished, write ONE JSON file at `{result_rel}` (relative to this "
            "workspace, NOT inside a repo — this file is the exception to the repo boundary "
            "above, and must never be committed):",
            '  {"status":"completed|failed|needs_human","summary":"<what you did overall>",',
            '   "artifacts":[{"work_item_id":<id>,"repo":"<name>","branch":"<branch>",'
            '"pr_url":"<url>"}],',
            '   "needs_human":false,"reason":"<why, if failed/needs_human>"}',
            "**`work_item_id` on every artifact is mandatory** — it is how each PR is reported "
            f"back on its own work item. Cover all {len(items)} items ("
            + ", ".join(f"#{i.id}" for i in items)
            + "): an item with no artifact is treated as FAILED, so if you deliberately opened "
            "no PR for one, say why in `reason`. Write this file even if the run failed or you "
            "are blocked — without it the whole batch is lost.",
        ]
        return "\n".join(lines)

    async def review_pr(
        self, repo_name: str, pr_id: int, pr_url: str = "", project: str = ""
    ) -> str:
        """:meth:`_review_pr` under the workspace owning ``project``.

        ``project`` is the linked work item's ADO project when the caller knows it (the
        PR babysitter usually does). Blank → the root workspace, which is correct
        whenever only one is configured."""
        with self.workspace_scope(project):
            return await self._review_pr(repo_name, pr_id, pr_url)

    async def _review_pr(self, repo_name: str, pr_id: int, pr_url: str = "") -> str:
        """Run the real code-review SKILL on a PR, evaluating the diff against the
        codebase (not just PR metadata). Claude runs the ``review-pr`` skill in an
        isolated worktree with the workspace's .claude skills + ADO MCP, fetches the
        diff, reviews per the project's checklist, and posts findings to the PR
        thread. Returns a short summary for the chat. Does NOT cast a vote."""
        workspace = self._config.workspace_directory
        scratch = await self._acquire_agent_scratch(pr_id, [repo_name])
        if scratch:
            run_dir = str(Path(scratch) / repo_name)
        else:  # no isolation available → run in the repo inside the shared workspace
            cand = Path(workspace or ".") / repo_name
            run_dir = str(cand) if cand.is_dir() else (workspace or ".")
        skill = self._config.teams_review_skill or "review-pr"
        ref = pr_url or f"PR !{pr_id} (repo {repo_name})"
        prompt = (
            f"/{skill} {ref}\n\n"
            f"Review pull request: {ref}. Phân tích diff SO VỚI codebase theo checklist "
            "của skill (Logic, Architecture, Security, Performance, Data). Đăng review "
            "findings (kèm severity + verdict) LÊN PR thread. TUYỆT ĐỐI KHÔNG cast vote. "
            "Cuối cùng trả về một tóm tắt NGẮN (verdict + số lượng finding theo severity)."
        )
        try:
            # Same deny list as the advisory path. The scratch worktree keeps a stray edit
            # out of YOUR checkout, but not out of the remote: this run has Bash, so an
            # edit it decided to "just fix" could be committed and pushed to the PR branch.
            run = await self._run_claude(
                prompt, run_dir, repo=run_dir, disallowed_tools=list(_READ_ONLY_DENY)
            )
            return (run.text or "").strip() or f"Đã review PR !{pr_id}."
        except TimeoutError:
            return f"Review PR !{pr_id} quá thời gian ({self._config.task_timeout_minutes} phút)."
        except Exception as exc:  # noqa: BLE001 — a review failure must not crash the caller
            self._log.warning("review_pr failed", pr=pr_id, error=str(exc))
            return f"Không review được PR !{pr_id}: {exc}"
        finally:
            await self.release_scratch(scratch)

    def _preflight(self, autonomy: str) -> str:
        """Config that makes an agent run impossible. Returns "" when it can run.

        Both cases below used to surface as "Agent produced no result file" —
        after a full task timeout — because the run started anyway against an
        empty cwd with no skills, no MCP and nothing it was allowed to edit.
        """
        workspace = self._config.workspace_directory
        if not workspace:
            return (
                "workspace_directory is not configured — the agent has no repos to work in "
                "and the workspace's .claude skills/MCP are not loaded. Set workspace_directory "
                "to the folder that holds your repo checkouts."
            )
        if not Path(workspace).is_dir():
            return f"workspace_directory does not exist: {workspace}"
        # report mode only reads + comments, so it needs no writable repo.
        if autonomy != "report" and not self._allowed_repos(workspace):
            allow = self._config.allowed_repos
            detail = (
                f"allowed_repos={list(allow)} matched nothing" if allow else "no git repos found"
            )
            return f"no repository available in workspace_directory '{workspace}' ({detail})"
        return ""

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
        if stable:  # interactive: deterministic path (reused across restarts)
            scratch = str(Path(base_dir) / f"agent-{item_id}")
            # A scratch this item's own session left behind is not leftover junk — it
            # holds that session's worktree AND (via cwd) its conversation. Re-entering
            # the item (a human comment, an @mention) must land there, or the follow-up
            # starts from zero. Anything else is cleaned as before.
            if self._reusable_session_scratch(scratch, item_id, repos):
                self._log.info("reusing interactive scratch", id=item_id, path=scratch)
                return scratch
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
                    # On Windows `worktree remove` often fails with "Permission
                    # denied": git's own object/pack files are read-only, and a
                    # just-exited child (Claude CLI, editor, AV scanner) can still
                    # hold handles for a moment. Delete the tree ourselves — clearing
                    # the read-only bit and retrying — then let `prune` drop git's
                    # now-dangling registration, so scratch dirs never pile up.
                    if sub.exists():
                        await _force_rmtree(sub)
                    await self._git(["worktree", "prune"], src_repo, check=False)
        await _force_rmtree(Path(run_dir))

    # ── SDLC loop: one shared branch across many stage runs in a scratch ──────

    async def prepare_stage_branch(self, scratch: str, repos: list[str], branch: str) -> None:
        """Create/reset the item's single feature branch in each worktree.

        ``_acquire_agent_scratch`` adds worktrees ``--detach`` at their base ref; the
        SDLC loop needs ONE stable branch to accumulate every stage's commits and to
        push from the ``pr`` stage. Best-effort per repo (a repo that can't branch is
        skipped, not fatal). A stale worktree still holding the branch is evicted
        first: otherwise this checkout silently fails and every stage commits onto a
        detached HEAD, so the ``pr`` stage finds nothing to push."""
        workspace = self._config.workspace_directory
        for repo in repos:
            worktree = Path(scratch) / repo
            if not worktree.is_dir():
                continue
            if workspace:
                try:
                    await self._free_branch_for_checkout(
                        str(Path(workspace) / repo), branch, 0, exclude=str(worktree)
                    )
                except GitError as exc:  # someone else's / dirty worktree — log, try anyway
                    self._log.warning("stage branch blocked by a worktree", error=str(exc))
            await self._git(["checkout", "-B", branch], str(worktree), check=False)
            current = (
                await self._git(["branch", "--show-current"], str(worktree), check=False)
            ).strip()
            if current != branch:
                self._log.error(
                    "stage branch not checked out — commits would be lost on a detached HEAD",
                    repo=repo, branch=branch, current=current or "(detached)",
                )

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

    @_scoped
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
        # Re-entry (human comment / @mention on an item this bot already worked): the
        # scratch above was REUSED, so the previous conversation is still on disk for
        # this cwd. Continue it instead of re-reading the codebase from scratch — and
        # close the old console first, or two processes share one transcript.
        resuming = bool(scratch) and self._read_session_handle(run_dir, item.id) is not None
        if resuming:
            await self.close_interactive(run_dir, item.id)
        # A fresh scratch is an unknown path to Claude Code — accept the workspace
        # trust dialog up front, or this session blocks on it and never starts.
        pretrust_claude_dir(run_dir)
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
        # Remote-Control sessions run locally as a normal (non-root) user and are
        # meant to proceed UNATTENDED — the human *attaches* to steer when they want
        # to, they shouldn't have to answer a permission prompt for every Bash/MCP
        # tool call (which is what stalls a rework). So default this path to
        # `bypassPermissions` (no prompts); only honour a different configured mode
        # if the operator explicitly pinned one (i.e. changed it away from the
        # `acceptEdits` default). The root-only restriction on bypassPermissions
        # applies to headless container runs, not this local interactive path.
        perm = self._config.claude_permission_mode
        interactive_perm = "bypassPermissions" if perm == "acceptEdits" else perm
        cli_args = [
            "--remote-control", session,
            "--permission-mode", interactive_perm,
            # Boolean flag, so it can't swallow the positional prompt (see NOTE above).
            *(["--continue"] if resuming else []),
            prompt,
        ]

        try:
            if sys.platform == "win32":
                # `cmd /c` lets cmd resolve claude.cmd via PATHEXT (passing the bare
                # `claude` shim to Popen does not work) and — unlike `/k` — lets the
                # console close once the CLI exits. `|| pause` keeps the window up
                # ONLY on a non-zero exit, so a startup error is still readable.
                proc = subprocess.Popen(  # noqa: ASYNC220 — fire-and-forget launch
                    ["cmd", "/c", "claude", *cli_args, "||", "pause"], cwd=run_dir,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            else:
                claude = shutil.which("claude") or "claude"
                proc = subprocess.Popen(  # noqa: ASYNC220
                    [claude, *cli_args], cwd=run_dir, start_new_session=True
                )
            # Remember the console's pid so `close_interactive` can shut the whole
            # tree down when the item finalises. Persisted (not just in memory) so an
            # autopilot restart can still close a session it did not launch itself.
            self._write_session_handle(run_dir, item.id, proc.pid, session)
            self._log.info(
                "launched interactive session", id=item.id, session=session,
                pid=proc.pid, cwd=run_dir, isolated=bool(scratch), resumed=resuming,
            )
            return True, session, run_dir
        except Exception as exc:  # noqa: BLE001
            self._log.error("failed to launch interactive session", id=item.id, error=str(exc))
            await self.release_scratch(scratch)
            return False, session, workspace

    @_scoped_sync
    def finalize_interactive(self, item: WorkItemInfo, run_dir: str) -> ExecutionResult | None:
        """Map a live session's result if it has finished; ``None`` while running."""
        agent = find_result(run_dir, item.id)
        if agent is None:
            return None
        return self._result_from_agent(item, agent, self._config.autonomy_level)

    # ── Interactive session lifetime: close the console when the item is done ──

    def _session_handle_path(self, run_dir: str, item_id: int) -> Path:
        """Sidecar holding the launched console's pid — survives an autopilot restart."""
        return Path(run_dir) / ".autopilot" / "runs" / f"{item_id}.session.json"

    def _write_session_handle(self, run_dir: str, item_id: int, pid: int, session: str) -> None:
        self._save_session_handle(run_dir, item_id, {
            "pid": pid, "session": session, "started": time.time(),
        })

    def _save_session_handle(self, run_dir: str, item_id: int, handle: dict) -> None:
        path = self._session_handle_path(run_dir, item_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(handle), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 — never fail a launch on bookkeeping
            self._log.warning("could not record session handle", id=item_id, error=str(exc))

    def _read_session_handle(self, run_dir: str, item_id: int) -> dict | None:
        try:
            handle = json.loads(
                self._session_handle_path(run_dir, item_id).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        return handle if isinstance(handle, dict) else None

    async def _process_matches(self, pid: int, session: str) -> bool:
        """True when ``pid`` is still OUR session — guards against pid reuse, which on
        a long-running box would otherwise make us kill an innocent process."""
        if sys.platform == "win32":
            ps = (
                f'$p = Get-CimInstance Win32_Process -Filter "ProcessId={pid}" '
                "-ErrorAction SilentlyContinue; if ($p) { $p.CommandLine }"
            )
            out = await self._run_capture(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]
            )
            if out is None:  # PowerShell unavailable → fall back to a liveness check
                out = await self._run_capture(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
                )
                return bool(out) and "cmd.exe" in out.lower()
            return session in out
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        try:  # Linux: confirm identity from the cmdline; elsewhere liveness is all we get
            cmdline = Path(  # noqa: ASYNC240 — /proc read is memory-backed, never blocks
                f"/proc/{pid}/cmdline"
            ).read_bytes().decode("utf-8", "replace")
        except OSError:
            return True
        return session in cmdline

    async def _run_capture(self, cmd: list[str]) -> str | None:
        """Run a short helper command, returning stdout (``None`` if it can't run)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except (OSError, TimeoutError):
            return None
        return out.decode("utf-8", "replace")

    def rework_scratch(self, item_id: int, repo_name: str) -> str | None:
        """The interactive scratch to run this item's rework in, or ``None``.

        Reusing it is what makes PR feedback cheap: the worktree already has the
        branch and the build state, and — because Claude Code keys its transcripts by
        cwd — running there is the ONLY way to resume the conversation the live
        session had. A rework from a fresh worktree re-reads the whole codebase and
        re-derives everything the session already knew.
        """
        if not self._config.interactive_resume_on_rework or not repo_name:
            return None
        scratch = self.interactive_scratch_dir(item_id)
        if self._read_session_handle(scratch, item_id) is None:
            return None  # no session held this item (headless run, or already released)
        return scratch if (Path(scratch) / repo_name / ".git").exists() else None

    def _reusable_session_scratch(self, scratch: str, item_id: int, repos: list[str]) -> bool:
        """True when ``scratch`` is this item's own session scratch and still usable —
        i.e. it was reserved by a session and the repos it holds are still real
        worktrees (a half-torn-down scratch is rebuilt from clean instead). Repos the
        original scratch skipped (no usable base branch there) don't block reuse —
        they were not in it the first time either."""
        if not self._config.interactive_resume_on_rework:
            return False
        if self._read_session_handle(scratch, item_id) is None:
            return False
        present = [r for r in repos if (Path(scratch) / r).is_dir()]
        return bool(present) and all((Path(scratch) / r / ".git").exists() for r in present)

    def _transcript_session_id(self, cwd: str) -> str | None:
        """Session id of the newest Claude Code conversation recorded for ``cwd``.

        Claude Code files transcripts under ``<config>/projects/<cwd-with-separators-
        dashed>/<session-id>.jsonl``; the interactive CLI never tells us its session
        id, so this is how a headless rework picks the conversation back up. Returns
        None when the directory doesn't exist — then the rework simply starts fresh.
        """
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
        project = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd)))
        try:
            newest = max(project.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, default=None)
        except OSError:
            return None
        return newest.stem if newest else None

    def session_finished(self, run_dir: str, item_id: int) -> bool:
        """True when the interactive session for ``item_id`` already wrote its result
        (so the console is idle and only being kept around for possible rework)."""
        return find_result(run_dir, item_id) is not None

    def list_open_sessions(self) -> dict[int, str]:
        """``{work_item_id: scratch}`` for every interactive session still holding a
        console (i.e. one we launched and have not closed). Read off disk, so it is
        correct after a restart too."""
        base = Path(self._scratch_base())
        sessions: dict[int, str] = {}
        if not base.is_dir():
            return sessions
        for sub in base.iterdir():
            if not sub.name.startswith("agent-"):
                continue
            suffix = sub.name.removeprefix("agent-")
            if not suffix.isdigit():  # headless scratches carry a -<uuid> suffix
                continue
            item_id = int(suffix)
            if self._session_handle_path(str(sub), item_id).is_file():
                sessions[item_id] = str(sub)
        return sessions

    async def close_interactive(self, run_dir: str | None, item_id: int) -> bool:
        """Close the console/session launched for ``item_id``. Best-effort, never raises.

        Called when the item finalises: the Remote-Control CLI is a REPL, so it sits
        idle forever after writing its result and the console never goes away on its
        own. Killing the tree first also unblocks ``release_scratch`` on Windows,
        where a live CLI holding the scratch as cwd makes ``worktree remove`` fail.
        """
        if not run_dir:
            return False
        handle = self._read_session_handle(run_dir, item_id)
        try:
            pid, session = int(handle["pid"]), str(handle.get("session", ""))  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            return False  # no handle, or already closed (pid dropped)
        closed = False
        if await self._process_matches(pid, session):
            if sys.platform == "win32":
                # /T kills the whole tree (cmd → claude → its node children); without
                # it the console dies but the CLI keeps holding the worktree.
                await self._run_capture(["taskkill", "/PID", str(pid), "/T", "/F"])
            else:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    with contextlib.suppress(OSError):
                        os.killpg(pid, sig)
                    await asyncio.sleep(1)
                    if not await self._process_matches(pid, session):
                        break
            closed = True
            self._log.info("closed interactive session", id=item_id, pid=pid, session=session)
        else:
            self._log.info("interactive session already gone", id=item_id, pid=pid)
        # Drop the pid but KEEP the handle: it also marks the scratch as this item's
        # (kept for rework), which is what the PR-close sweep releases on. The file
        # goes away with the scratch itself.
        self._save_session_handle(
            run_dir, item_id, {"session": session, "closed": time.time()}
        )
        return closed

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
                # A scratch kept alive for review (console still open, PR still open)
                # is NOT an orphan — its worktree is where the rework happens.
                if runs.is_dir() and any(runs.glob("*.session.json")):
                    self._log.info("prune_orphans: keeping open session scratch", path=str(sub))
                    continue
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

    def _result_from_agent(
        self, item, agent, autonomy: str, run_text: str = ""
    ) -> ExecutionResult:
        if agent is None:
            # "No result file" describes OUR bookkeeping, not what went wrong — and it
            # is what the human sees on the work item. The agent almost always said
            # why it stopped ("#123 chưa implement nên chưa có API để gọi"), so carry
            # its closing words into the error and the full output into the record.
            self._log.warning("agent wrote no result file", id=item.id)
            last = _last_words(run_text)
            reason = "Agent produced no result file (.autopilot/runs/<id>.json)"
            if last:
                reason += f" — agent's last words: {last}"
            result = ExecutionResult.fail(item.id, "agent", reason)
            result.output = run_text
            return result
        if agent.needs_human:
            self._log.info("agent escalated to human", id=item.id, reason=agent.reason)
            result = ExecutionResult.fail(item.id, "agent", agent.reason or "needs human input")
            result.needs_human = True
            result.output = agent.summary
            result.deviations = list(agent.deviations)
            return result
        # report mode completes without a PR; otherwise a PR URL is required.
        if agent.is_completed and (agent.pr_url or autonomy == "report"):
            result = ExecutionResult.ok(item.id, "agent", agent.summary)
            result.deviations = list(agent.deviations)
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

        # Autonomy directive — the single biggest cause of a stalled rework is the
        # agent stopping to ask the human a clarifying question instead of deciding.
        # Tell it explicitly to proceed. Report mode stays advisory (no code), so it
        # only needs the "don't ask, state assumptions" half.
        if autonomy == "report":
            ambiguity = (
                "- Work autonomously — do NOT ask the human clarifying questions. If something is "
                "ambiguous, state your assumption in the plan and proceed with the most reasonable "
                "interpretation."
            )
        else:
            ambiguity = (
                "- Work autonomously and DECISIVELY. Do NOT ask the human clarifying questions, and "
                "do NOT wait for confirmation before acting — you will not get an answer. When "
                "something is ambiguous, choose the most reasonable interpretation from the work "
                "item, its acceptance criteria, the latest human comment, and the existing "
                "code/conventions, note the assumption briefly in the commit/PR, and keep going.\n"
                "- Report needs_human ONLY for a genuine HARD blocker you cannot resolve yourself: "
                "missing credentials or repo access, an irreversible destructive action that "
                "needs sign-off, or a DEPENDENCY THAT HAS NOT LANDED YET (the API / table / "
                "component this item builds on does not exist in the repo, because the work item "
                "that delivers it has not run). In that last case say WHICH work item you are "
                "waiting on. Ambiguity, naming/style choices, and minor gaps are NOT blockers — "
                "decide and move on.\n"
                "- Being blocked is a RESULT, not a reason to stop silently: write the result "
                "file with needs_human=true and the reason. Ending the run without that file "
                "throws away everything you found out."
            )

        # Enforce the skill allowlist in the AI-native path too (the legacy router
        # path checks RbacPolicy.is_skill_allowed, but here the agent picks skills
        # itself, so the only place to constrain it is the brief).
        if self._config.allowed_skills:
            skill_rule = (
                "- You may ONLY use these skills — a hard allowlist; do NOT run any other "
                "skill under any circumstance: " + ", ".join(self._config.allowed_skills)
            )
        else:
            skill_rule = (
                "- Choose and run the most appropriate skill(s) in this workspace — "
                "do not assume one."
            )

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
        if self._config.learning_loop_enabled:
            past = lessons.lessons_brief(
                self._config.workspace_directory, repos,
                limit=self._config.lessons_max_injected,
            )
            if past:
                lines.append(past)
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
            skill_rule,
            "- Fetch work-item detail with the Azure DevOps MCP tool directly (e.g. "
            "`wit_get_work_item`) — do NOT shell out to the `az` CLI (it mangles Vietnamese "
            "to cp1252) and do NOT spawn a sub-agent just to look the item up.",
            "- On Windows use PowerShell for shell commands; the Git Bash here is stripped-down "
            "(no `head`/`grep`/`find`/`printenv`), so don't retry the same command across shells.",
            "- For EACH repo you change: start from a clean base branch, create a feature branch, "
            "commit, push, and open a pull request with the pr-create skill.",
            f"- The PR MUST be linked to work item #{item.id} (the pr-create skill does this; "
            "branch names starting with the item id let ADO do it too). The control plane "
            "verifies the link afterwards and attaches it if it is missing.",
            f"- {action}",
            "- Run a self-review (e.g. security-review / review-pr skill) before opening the PR.",
            ambiguity,
            f"- {BOT_COMMENT_INSTRUCTION}",
            AGENT_CONDUCT_INSTRUCTION,
            "",
            "# Required output (the control plane reads ONLY this — you MUST write it)",
            f"When finished, write a JSON file at `{result_rel}` (relative to this workspace):",
            "This one file is the EXCEPTION to 'do not edit anything outside these repos': it "
            "belongs at the WORKSPACE ROOT, NOT inside a repo folder, and must never be "
            "committed. Write it even if the task failed or you are blocked — a missing file "
            "loses the whole run.",
            '  {"status":"completed|failed|needs_human","summary":"<what you did>",',
            '   "artifacts":[{"repo":"<name>","branch":"<branch>","pr_url":"<url>"}],',
            '   "needs_human":false,"reason":"<why, if failed/needs_human>",',
            '   "deviations":[{"kind":"spec_unclear|logic_differs|spec_gap|out_of_scope|assumption",',
            '                  "summary":"<one line: what differs from the item>",',
            '                  "detail":"<why you chose this>","where":"<AC id / file / endpoint>"}]}',
            "List EVERY PR you opened in artifacts. Set status=completed only if at least one PR "
            "was opened (or, in report mode, the plan was commented). Use needs_human=true with a "
            "clear reason when you need a human.",
            "",
            "## deviations — REQUIRED whenever you decided something the item did not settle",
            "You were told above to decide instead of asking. Every such decision is a place "
            "where the work item and the code you just wrote no longer say the same thing, and "
            "the person who keeps the specification true has no other way to find out. Record "
            "one entry for EACH of these:",
            "- the description or AC was unclear and you picked an interpretation (`spec_unclear`)",
            "- you deliberately implemented something differently from what was described, "
            "because the description was wrong, impossible, or inconsistent with the code "
            "(`logic_differs`)",
            "- you hit a case the item never covers and had to define the behaviour (`spec_gap`)",
            "- you found a real problem this item does not cover and left it alone (`out_of_scope`)",
            "- you proceeded on an assumption a human should confirm (`assumption`)",
            "Write what a BA needs to update the spec: the specific rule/field/case, not "
            "\"clarified requirements\". Leave the list EMPTY when the item described the work "
            "exactly and you followed it — do not invent entries, and do not use this for "
            "ordinary implementation detail (naming, file layout, refactors).",
        ]
        return "\n".join(lines)

    @_scoped
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
        # Feedback on a PR an interactive session opened: work it in that session's
        # own scratch so the conversation can be resumed (see ``rework_scratch``).
        # Its console is idle by now — close it first, or two processes would be
        # writing the same transcript.
        scratch = self.rework_scratch(item.id, Path(repo_path).name)
        if scratch:
            await self.close_interactive(scratch, item.id)
        return await self._run_in_workspace(
            item_id=item.id,
            scratch=scratch or "",
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

        Because there is no worktree to throw away, "changes nothing" is enforced by
        DENYING the file-mutating tools (``_READ_ONLY_DENY``) rather than by asking in
        the prompt. This is the same reasoning as the Teams chat path: safety belongs in
        the tool surface, not in an instruction the agent could reasonably misread —
        and this is the path that runs unattended, on every auto-review.

        It is not a sandbox: ``Bash`` stays available because the review needs
        ``git diff``, and a shell can write files. That is why the dirty-check below
        remains — the deny list removes the likely accident, not every possibility.
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
            resume = await self._resume_for(repo, branch)
            claude_run = await self._run_claude(
                prompt, claude_cwd, repo=repo, resume=resume,
                disallowed_tools=list(_READ_ONLY_DENY),
            )
            await self._save_session(repo, branch, claude_run)
            result = ExecutionResult.ok(item_id, prompt, claude_run.text)
            apply_usage(result, claude_run)
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
        self, name: str, prompt: str, repo: str, base_branch: str, branch: str,
        draft_pr: bool, project: str = "",
    ) -> ExecutionResult:
        """Run a scheduled loop's prompt on a fresh branch and open a PR.

        ``project`` picks the workspace the loop runs in (``ScheduledLoop.project``),
        so a sweeper can be pointed at a second workspace's repos."""
        with self.workspace_scope(project):
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
        scratch: str = "",
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
                repo, branch, base_branch, item_id, existing_branch, scratch=scratch
            )
            work_dir = workspace.path  # git operates here (the repo checkout)
            claude_cwd = workspace.claude_cwd or work_dir  # Claude runs here

            self._log.info("running claude", id=item_id, branch=branch, cwd=claude_cwd)
            resume = workspace.resume_from or await self._resume_for(repo, branch)
            claude_run = await self._run_claude(
                prompt, claude_cwd, repo=work_dir, resume=resume
            )
            await self._save_session(repo, branch, claude_run)

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
                apply_usage(result, claude_run)
                return result

            changed_files = await self._changed_files(work_dir)
            # Policy gate: protected paths / blast radius — a hard rail checked
            # before anything is pushed or reviewed. Violations block the PR.
            violations = policy.check_changes(
                changed_files,
                protected_paths=self._config.policy_protected_paths,
                max_files=self._config.policy_max_files_changed,
            )
            if violations:
                self._log.warning("policy blocked run", id=item_id, violations=violations)
                result = ExecutionResult.fail(
                    item_id, prompt, "Policy blocked: " + "; ".join(violations)
                )
                result.branch_name = branch
                result.files_changed = changed_files
                result.duration_seconds = time.monotonic() - started
                apply_usage(result, claude_run)
                return result
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
            if self._config.learning_loop_enabled and (review.critical_issues or review.warnings):
                # Remember what got flagged so the next run on this repo is warned.
                ws = self._config.workspace_directory
                repo_name = _repo_name(repo, ws) if ws else repo
                lessons.record_lessons(
                    ws or work_dir, repo_name,
                    review.critical_issues + review.warnings, now=datetime.now(),
                )
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
                apply_usage(result, claude_run)
                return result

            # Auto-test-gate: run the repo's tests in the worktree BEFORE opening a
            # PR (mirrors the auto-review block above). A red run blocks the PR; a
            # skip (gate off / no runner) passes through.
            tests = await self._test_gate.run(work_dir)
            if tests.ran and not tests.passed:
                self._log.warning("test gate blocked PR", id=item_id, summary=tests.summary)
                result = ExecutionResult.fail(item_id, prompt, "Tests failed: " + tests.summary)
                result.branch_name = branch
                result.files_changed = changed_files
                result.tests_passed = False
                result.output = tests.output_tail
                result.duration_seconds = time.monotonic() - started
                apply_usage(result, claude_run)
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
            result.tests_passed = tests.passed if tests.ran else None
            result.duration_seconds = time.monotonic() - started
            apply_usage(result, *runs)
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
        self, repo: str, branch: str, base_branch: str, item_id: int,
        existing_branch: bool = False, scratch: str = "",
    ) -> _Workspace:
        """Create an isolated checkout for one execution.

        With ``use_worktrees`` (default) each execution gets its own ``git
        worktree`` so concurrent items never collide on a shared checkout. When
        disabled, falls back to an in-place ``checkout`` in the repo.

        ``existing_branch=True`` checks out an already-pushed branch (used to
        revise an open PR) instead of branching from ``base_branch``.

        ``scratch`` reuses an interactive session's worktree (see ``rework_scratch``)
        — same cwd, so the run resumes that session's conversation.
        """
        if scratch:
            reused = await self._reuse_interactive_scratch(repo, branch, item_id, scratch)
            if reused is not None:
                return reused
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
            # `reset --hard` + `clean -fd` wipe the shared checkout — irrecoverably
            # discarding any uncommitted or untracked files a human may have left in
            # this repo. Surface exactly what is being destroyed instead of doing it
            # silently, so a lost edit is at least diagnosable from the log.
            dirty = (await self._git(["status", "--porcelain"], repo, check=False)).strip()
            if dirty:
                self._log.warning(
                    "workspace mode: discarding uncommitted changes in shared checkout",
                    id=item_id, repo=repo,
                    files=[ln[3:].strip() for ln in dirty.splitlines()][:50],
                    count=len(dirty.splitlines()),
                )
            await self._git(["reset", "--hard"], repo, check=False)
            await self._git(["clean", "-fd"], repo, check=False)
            # A leftover scratch worktree may still hold this branch — `checkout -B`
            # would then fail outright ("already used by worktree at …").
            await self._free_branch_for_checkout(repo, branch, item_id)
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
            # …and so may a scratch worktree from an unfinished run on this item.
            await self._free_branch_for_checkout(repo, branch, item_id)
            # Name the worktree dir by a short GUID (not the branch) to keep the
            # path short — long branch names otherwise blow past Windows MAX_PATH.
            # The real branch is still created via ``-B branch`` below.
            path = str(Path(base_dir) / f"{item_id}-{uuid.uuid4().hex[:8]}")
            self._log.info("creating worktree", id=item_id, branch=branch, path=path)
            await self._git(["worktree", "add", "-B", branch, path, from_ref], repo)
            return _Workspace(repo, path, branch, base_branch, is_worktree=True)

        self._log.info("creating branch in-place", id=item_id, branch=branch, repo=repo)
        await self._free_branch_for_checkout(repo, branch, item_id)
        await self._git(["checkout", "-B", branch, from_ref], repo)
        return _Workspace(repo, repo, branch, base_branch, is_worktree=False)

    async def _reuse_interactive_scratch(
        self, repo: str, branch: str, item_id: int, scratch: str
    ) -> _Workspace | None:
        """Check the PR branch out inside the interactive session's own worktree.

        Best-effort: any failure returns ``None`` so the caller falls back to a normal
        (fresh) checkout — a rework must never be blocked by an optimisation. The
        worktree is reset to ``origin/<branch>`` rather than merged into: everything
        the session did is already pushed (the PR exists), so the remote is the truth,
        and leftovers from a half-finished turn would otherwise ride along into the
        next commit. ``keep=True`` leaves the scratch standing for the next round —
        the PR-close sweep is what releases it.
        """
        worktree = str(Path(scratch) / Path(repo).name)
        try:
            await self._git(["fetch", "origin", branch], repo, check=False)
            # This worktree very likely holds the branch already (the session left it
            # checked out) — that is not a stale claim, so exclude it from eviction.
            await self._free_branch_for_checkout(repo, branch, item_id, exclude=worktree)
            await self._git(["reset", "--hard"], worktree, check=False)
            await self._git(["clean", "-fd"], worktree, check=False)
            await self._git(["checkout", "-B", branch, f"origin/{branch}"], worktree)
        except Exception as exc:  # noqa: BLE001 — fall back to a fresh checkout
            self._log.warning(
                "could not reuse interactive scratch — fresh checkout",
                id=item_id, scratch=scratch, error=str(exc),
            )
            return None
        resume = self._transcript_session_id(scratch) or ""
        self._log.info(
            "reusing interactive scratch for rework",
            id=item_id, branch=branch, path=worktree, resuming=bool(resume),
        )
        return _Workspace(
            repo, worktree, branch, base_branch=self._config.base_branch,
            is_worktree=True, claude_cwd=scratch, keep=True, resume_from=resume,
        )

    async def _worktree_holding_branch(
        self, repo: str, branch: str, exclude: str | None = None
    ) -> str | None:
        """Path of a *linked* worktree that currently has ``branch`` checked out.

        Git refuses ``checkout -B <branch>`` / ``worktree add -B <branch>`` while any
        other worktree holds it ("fatal: '<branch>' is already used by worktree at
        …"). Registrations also outlive their directory, so prune first — that alone
        clears the common case. Two holders are never reported: the repo's own
        checkout (``-B`` there is a plain branch reset, which git allows) and
        ``exclude`` — the worktree the caller is *about* to check out in, which on a
        resumed run already holds the branch from the previous round.
        """
        async with self._repo_lock(repo):
            await self._git(["worktree", "prune"], repo, check=False)
            listing = await self._git(["worktree", "list", "--porcelain"], repo, check=False)
        allowed = {Path(repo)} | ({Path(exclude)} if exclude else set())
        path: str | None = None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):].strip()
            elif path and line.strip() == f"branch refs/heads/{branch}":
                return None if Path(path) in allowed else path
        return None

    async def _free_branch_for_checkout(
        self, repo: str, branch: str, item_id: int, exclude: str | None = None
    ) -> None:
        """Clear a *stale* worktree claim on ``branch`` so the branch can be checked out.

        A scratch worktree that outlived its run — an interactive session that never
        finalised, a crashed or killed task — keeps its branch checked out forever,
        and every later run on that item then dies on git's "already used by worktree"
        instead of doing any work.

        Only worktrees the autopilot owns (under ``_scratch_base``) and holding
        nothing uncommitted are evicted. A human's worktree, or one with live work in
        it, is left alone and reported with a message that says what to do — losing
        someone's edits to unblock a robot is the worse failure.
        """
        held = await self._worktree_holding_branch(repo, branch, exclude)
        if held is None:
            return
        scratch_base, held_path = Path(self._scratch_base()), Path(held)
        if scratch_base != held_path and scratch_base not in held_path.parents:
            raise GitError(
                f"branch {branch!r} is checked out in a worktree AI-Autopilot does not "
                f"own: {held}. Finish or drop it (`git worktree remove {held}`), then retry."
            )
        dirty = (await self._git(["status", "--porcelain"], held, check=False)).strip()
        if dirty:
            raise GitError(
                f"branch {branch!r} is checked out in scratch worktree {held}, which has "
                f"{len(dirty.splitlines())} uncommitted change(s). Commit or discard them "
                f"(`git worktree remove --force {held}` drops them), then retry."
            )
        self._log.warning(
            "evicting stale worktree that held the branch",
            id=item_id, branch=branch, path=held, repo=repo,
        )
        async with self._repo_lock(repo):
            await self._git(["worktree", "remove", "--force", held], repo, check=False)
            if held_path.exists():
                await _force_rmtree(held_path)
            await self._git(["worktree", "prune"], repo, check=False)

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
        # Outside the lock: the helper takes it itself and asyncio locks aren't reentrant.
        await self._free_branch_for_checkout(repo, branch, item_id)
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
        self, prompt: str, work_dir: str, repo: str | None = None, on_event=None,
        resume: str | None = None, disallowed_tools: list[str] | None = None,
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
            disallowed_tools=disallowed_tools,
            setting_sources=setting_sources,
            mcp_servers=mcp_servers,
            add_dirs=add_dirs,
            resume=resume,
            # Blank by default → the model's own default, i.e. unchanged. Exposed so effort
            # can be swept on real evals (or raised to "xhigh") without touching code.
            effort=self._config.claude_effort_task or None,
            on_event=on_event,
        )

    async def _resume_for(self, repo: str, branch: str) -> str | None:
        """Stored session id to resume for this branch, or None (fresh). Best-effort —
        gated by config, TTL-bounded, and never raises into the run."""
        if not self._config.reuse_claude_session or self._session_repo is None or not branch:
            return None
        try:
            return await self._session_repo.get(
                repo or "", branch, self._config.claude_session_ttl_hours
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("session lookup failed", branch=branch, error=str(exc))
            return None

    async def _save_session(self, repo: str, branch: str, run: ClaudeRun) -> None:
        if (
            not self._config.reuse_claude_session
            or self._session_repo is None
            or not branch
            or not run.session_id
        ):
            return
        with contextlib.suppress(Exception):
            await self._session_repo.save(repo or "", branch, run.session_id)

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


async def _force_rmtree(path: Path, attempts: int = 4) -> bool:
    """Delete a directory tree that resists deletion. Returns True if it's gone.

    Windows needs this: git's object/pack files are marked read-only (so plain
    deletion raises ``Permission denied``), and a process that just exited — the
    Claude CLI, an editor, an AV scanner — can keep handles open for a short
    while. So we retry a few times, clearing the read-only bit across the tree
    between attempts and backing off to let stale handles close. Never raises;
    the caller only cares that scratch dirs don't accumulate.
    """
    for attempt in range(attempts):
        if not path.exists():
            return True
        try:
            shutil.rmtree(path)  # noqa: ASYNC240 - local fs delete, retried below
            return True
        except OSError:
            # Clear read-only bits (the usual cause) before the next attempt.
            for root, dirs, files in os.walk(path):
                for name in (*dirs, *files):
                    with contextlib.suppress(OSError):
                        os.chmod(os.path.join(root, name), stat.S_IWRITE)  # noqa: ASYNC240
            with contextlib.suppress(OSError):
                os.chmod(path, stat.S_IWRITE)  # noqa: ASYNC240
            await asyncio.sleep(0.4 * (attempt + 1))  # let stale handles close
    if path.exists():
        _log.warning("could not fully delete scratch dir — leaving it behind", path=str(path))
        return False
    return True


def _repo_name(repo: str, workspace: str) -> str:
    """Repo directory name relative to the workspace (e.g. 'Backend-Fresh')."""
    try:
        return Path(repo).relative_to(Path(workspace)).as_posix()
    except ValueError:
        return Path(repo).name


def _load_mcp_servers(workspace: str) -> dict | None:
    """Read MCP server config for the Agent SDK.

    Checks ``<workspace>/.claude/mcp.json`` then falls back to the Claude Code
    standard ``<workspace>/.mcp.json``. Returned as a dict the SDK can consume.

    A MISSING file is fine (returns ``None`` silently). But a file that EXISTS and
    fails to parse is logged as a WARNING rather than swallowed — a single JSON
    typo otherwise strips every MCP tool (e.g. the Azure DevOps ``wit_*`` tools),
    which sends the agent into minutes of blind fallback (az CLI / curl / spawning
    sub-agents) with no signal as to why. Loud failure > silent degradation.
    """
    for rel in (Path(".claude") / "mcp.json", Path(".mcp.json")):
        path = Path(workspace) / rel
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _log.warning(
                "mcp config present but unreadable — NO MCP tools will load for this "
                "run; agents lose ADO/DB tools and fall back blindly. Fix the file.",
                path=str(path), error=str(exc),
            )
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if servers:
            return servers
    return None


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


def _last_words(text: str, limit: int = 400) -> str:
    """The tail of the agent's output, flattened to one line for an error message."""
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    return flat if len(flat) <= limit else "…" + flat[-limit:]
