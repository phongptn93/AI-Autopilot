"""Everything about ONE work item, gathered once.

Answering "what happened with #4021?" currently means opening six places: the board for
its state, the activity page for what the agent did, History for its runs, ADO for the
comments, the PR for the code, and now the spec-drift list. Each is correct and none of
them is the answer; the reader assembles it by hand, every time, and mostly gives up
and just reads the PR.

This gathers all of it in one pass so a single page can show it, in the order a person
actually asks: what is it, what did the agent decide, what did it change, and what has
happened to it since.

Every section is independent and best-effort. A page that renders with the diff missing
because a repo was not fetchable is far more useful than a 500 that hides the six
sections that were fine — so each gather step catches its own failure and reports the
gap in place of the data.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ai_autopilot import activity
from ai_autopilot.container import Container
from ai_autopilot.diffs import Diff, parse_unified_diff
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.pr_feedback import parse_pr_url

_log = get_logger("services.task_room")

# A task page is read while someone waits. Anything slower than this is not worth the
# wait — the PR link is right there.
_GIT_TIMEOUT_SECONDS = 25


@dataclass
class PrView:
    """One PR of this task, with its diff if we could produce one."""

    url: str = ""
    repo: str = ""
    pr_id: int = 0
    branch: str = ""
    status: str = ""
    title: str = ""
    diff: Diff | None = None
    diff_error: str = ""      # why there is no diff — never left blank silently


@dataclass
class Artifact:
    """An HTML file the run produced (mockup, brief, report) — previewable in place."""

    name: str = ""
    rel_path: str = ""        # relative to the workspace root; what the viewer route takes
    size: int = 0


@dataclass
class TaskRoom:
    work_item_id: int = 0
    item: WorkItemInfo | None = None
    error: str = ""                      # the item itself could not be read
    activity_log: str = ""
    brief: str = ""
    comments: list[dict] = field(default_factory=list)
    timeline: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    drifts: list = field(default_factory=list)
    audit: list = field(default_factory=list)
    prs: list[PrView] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)

    @property
    def open_drifts(self) -> list:
        return [d for d in self.drifts if d.resolved_at is None]

    @property
    def files_changed(self) -> int:
        return sum(len(p.diff.files) for p in self.prs if p.diff)

    @property
    def lines_added(self) -> int:
        return sum(p.diff.added for p in self.prs if p.diff)

    @property
    def lines_removed(self) -> int:
        return sum(p.diff.removed for p in self.prs if p.diff)


async def _git(args: list[str], cwd: str) -> tuple[int, str]:
    """Run one git command. Returns ``(returncode, stdout)``; never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT_SECONDS)
        return proc.returncode or 0, out.decode("utf-8", "replace")
    except (OSError, TimeoutError) as exc:
        return 1, str(exc)


async def _diff_for_branch(repo_dir: str, branch: str, base_branch: str) -> tuple[Diff | None, str]:
    """``git diff base...branch`` for a branch that lives on the remote.

    Read from the local clone rather than from the ADO API on purpose: the API needs an
    iteration id, then a call per file, then the blob of each side — a dozen round trips
    to rebuild what one local command already knows. The clone may be stale, so the
    branch is fetched first.
    """
    if not Path(repo_dir).is_dir():  # noqa: ASYNC240 — one local stat, not a blocking read
        return None, f"repo không có ở máy này ({repo_dir})"
    code, out = await _git(["fetch", "--no-recurse-submodules", "origin", branch], repo_dir)
    if code != 0:
        return None, f"không fetch được nhánh {branch}"
    # Three dots: the changes the branch introduced since it diverged, NOT everything
    # that has since landed on the base. Two dots would show other people's commits as
    # this task's work.
    code, out = await _git(
        ["diff", "--no-color", f"origin/{base_branch}...FETCH_HEAD"], repo_dir
    )
    if code != 0:
        return None, f"không diff được với origin/{base_branch}"
    return parse_unified_diff(out), ""


class TaskRoomService:
    """Gathers a :class:`TaskRoom`. Stateless — one call per page view."""

    def __init__(self, c: Container) -> None:
        self._c = c

    @property
    def _config(self):
        return self._c.config

    async def gather(self, work_item_id: int, *, with_diff: bool = True) -> TaskRoom:
        room = TaskRoom(work_item_id=work_item_id)
        cfg = self._config
        workspace = cfg.workspace_directory

        try:
            room.item = await self._c.ado.get_work_item(work_item_id)
        except Exception as exc:  # noqa: BLE001
            room.error = f"Không đọc được work item từ ADO: {exc}"
        if room.item is None and not room.error:
            room.error = "Không tìm thấy work item này trên ADO."

        scoped = cfg.scoped_for_project(getattr(room.item, "project", "") or "")
        ws = scoped.workspace_directory or workspace

        # Local, cheap, and independent of ADO — these still render when the API is down.
        room.activity_log = _safe(lambda: activity.read(ws, work_item_id), "")
        room.brief = _safe(lambda: _read_brief(ws, scoped, work_item_id), "")
        room.artifacts = _safe(lambda: _artifacts(ws, work_item_id), [])

        room.timeline = await _safe_async(
            self._c.state_history.timeline(work_item_id), [], "timeline"
        )
        room.runs = await _safe_async(
            self._c.execution_repo.for_item(work_item_id), [], "runs"
        )
        room.drifts = await _safe_async(
            self._c.spec_drift_repo.for_item(work_item_id), [], "drifts"
        )
        room.audit = await _safe_async(
            self._c.audit_repo.for_target(str(work_item_id)), [], "audit"
        )
        room.comments = await _safe_async(
            self._c.ado.get_work_item_comments(work_item_id), [], "comments"
        )
        room.prs = await self._pull_requests(room, scoped, with_diff=with_diff)
        return room

    async def _pull_requests(self, room: TaskRoom, scoped, *, with_diff: bool) -> list[PrView]:
        """Every PR this item produced, newest run first, each with its diff."""
        urls: list[str] = []
        for run in room.runs:
            for url in _run_pr_urls(run):
                if url not in urls:
                    urls.append(url)
        views: list[PrView] = []
        for url in urls:
            parsed = parse_pr_url(url)
            if parsed is None:
                continue
            repo_name, pr_id = parsed
            view = PrView(url=url, repo=repo_name, pr_id=pr_id)
            view.branch = next(
                (r.branch_name for r in room.runs if r.pr_url == url and r.branch_name), ""
            )
            await self._decorate_from_ado(view)
            if with_diff and view.branch:
                repo_dir = str(Path(scoped.workspace_directory or "") / repo_name)
                view.diff, view.diff_error = await _diff_for_branch(
                    repo_dir, view.branch, scoped.base_branch
                )
            elif with_diff:
                view.diff_error = "run này không ghi lại tên nhánh"
            views.append(view)
        return views

    async def _decorate_from_ado(self, view: PrView) -> None:
        """Status and title come from ADO; missing them costs a label, not the diff."""
        try:
            repos = {
                (r.get("name") or "").lower(): str(r.get("id") or "")
                for r in await self._c.ado.get_repositories()
            }
            repo_guid = repos.get(view.repo.lower())
            if not repo_guid:
                return
            pr = await self._c.ado.get_pull_request(repo_guid, view.pr_id)
            if pr:
                view.status = str(pr.get("status") or "")
                view.title = str(pr.get("title") or "")
                view.branch = view.branch or str(
                    pr.get("sourceRefName", "")
                ).removeprefix("refs/heads/")
        except Exception as exc:  # noqa: BLE001 — a label is not worth failing the page
            _log.info("task room: PR lookup failed", pr=view.pr_id, error=str(exc))


def _run_pr_urls(run) -> list[str]:
    import json

    urls = [run.pr_url] if run.pr_url else []
    # A malformed column costs the extra PRs, not the primary one.
    with contextlib.suppress(ValueError, TypeError):
        urls += [u for u in json.loads(run.pr_urls or "[]") if isinstance(u, str)]
    return [u for u in dict.fromkeys(urls) if u]


def _read_brief(workspace: str, scoped, item_id: int) -> str:
    """The instructions this run was given — the honest answer to "why did it do that".

    Looked for in the interactive scratch first: that is where an interactive session
    writes it, and it is the copy that matches the run the reader is looking at.
    """
    candidates = [
        Path(workspace) / ".aiwt" / f"agent-{item_id}" / ".autopilot" / "runs"
        / f"{item_id}.brief.md",
        Path(workspace) / ".autopilot" / "runs" / f"{item_id}.brief.md",
    ]
    base = (scoped.worktrees_dir or "").strip()
    if base:
        candidates.insert(
            0, Path(base) / f"agent-{item_id}" / ".autopilot" / "runs" / f"{item_id}.brief.md"
        )
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return ""


# HTML the agent produced for a human to LOOK at (spec mockups, dev briefs, technical
# notes, bug-fix reports). Everything else in a repo is code, and a code file rendered
# as a page is a security problem dressed as a feature.
_ARTIFACT_DIRS = ("specs", "docs", "specs-dxfac", ".autopilot")


def _artifacts(workspace: str, item_id: int, limit: int = 40) -> list[Artifact]:
    """Recent HTML artifacts under the workspace, newest first.

    Not filtered to this item: the generators name files after the feature slug, not the
    work-item id, so filtering by id would show an empty list for every task that has
    one. Recency is the honest proxy, and the list says what it is.
    """
    root = Path(workspace or "")
    if not root.is_dir():
        return []
    found: list[tuple[float, Artifact]] = []
    for sub in _ARTIFACT_DIRS:
        base = root / sub
        if not base.is_dir():
            continue
        try:
            for path in base.rglob("*.html"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rel = path.relative_to(root).as_posix()
                found.append((stat.st_mtime, Artifact(path.name, rel, stat.st_size)))
        except OSError:
            continue
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [a for _, a in found[:limit]]


def _safe(fn, default):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — one missing section must not blank the page
        _log.info("task room: section unavailable", error=str(exc))
        return default


async def _safe_async(coro, default, what: str):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        _log.info("task room: section unavailable", section=what, error=str(exc))
        return default
