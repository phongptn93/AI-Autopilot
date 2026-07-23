"""Integration tests for the git-worktree workspace lifecycle.

Sets up a real bare "origin" + clone so ``origin/<base>`` exists, then exercises
``_acquire_workspace`` / ``_release_workspace`` directly (no Claude needed).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ai_autopilot.config import Settings
from ai_autopilot.execution.auto_reviewer import AutoReviewer
from ai_autopilot.execution.claude_executor import ClaudeExecutor

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone whose origin has a 'development' branch."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "development", str(origin)], check=True,
                   capture_output=True)

    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "t@t.test")
    _git(work, "config", "user.name", "Test")
    _git(work, "checkout", "-b", "development")
    (work / "README.md").write_text("seed\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "seed")
    _git(work, "push", "-u", "origin", "development")
    _git(work, "fetch", "origin")
    return work


def _executor(**overrides) -> ClaudeExecutor:
    config = Settings(base_branch="development", **overrides)
    return ClaudeExecutor(config, AutoReviewer(config))


def _rev(cwd: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True
    ).stdout.strip()


def _porcelain(cwd: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def agent_workspace(tmp_path: Path) -> Path:
    """A workspace with a shared .claude and two git repos (origin/development)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".claude").mkdir()
    (ws / ".claude" / "skills.md").write_text("shared skill\n")
    for name in ("repo-a", "repo-b"):
        origin = tmp_path / f"{name}.git"
        subprocess.run(["git", "init", "--bare", "-b", "development", str(origin)],
                       check=True, capture_output=True)
        work = ws / name
        subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
        _git(work, "config", "user.email", "t@t.test")
        _git(work, "config", "user.name", "Test")
        _git(work, "checkout", "-b", "development")
        (work / "README.md").write_text("seed\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", "seed")
        _git(work, "push", "-u", "origin", "development")
        _git(work, "fetch", "origin")
    return ws


async def test_worktree_acquire_and_release(repo: Path, tmp_path: Path):
    wt_dir = tmp_path / "wts"
    ex = _executor(use_worktrees=True, worktrees_dir=str(wt_dir))

    ws = await ex._acquire_workspace(str(repo), "feature/be/42-thing", "development", 42)
    try:
        assert ws.is_worktree is True
        assert Path(ws.path).is_dir()
        assert (Path(ws.path) / "README.md").exists()  # checked out from base
        assert ws.path != str(repo)  # isolated from the main checkout
    finally:
        await ex._release_workspace(ws)

    assert not Path(ws.path).exists()  # cleaned up


async def test_two_worktrees_are_isolated(repo: Path, tmp_path: Path):
    ex = _executor(use_worktrees=True, worktrees_dir=str(tmp_path / "wts"))

    a = await ex._acquire_workspace(str(repo), "feature/be/1-a", "development", 1)
    b = await ex._acquire_workspace(str(repo), "feature/be/2-b", "development", 2)
    try:
        assert a.path != b.path
        assert Path(a.path).is_dir() and Path(b.path).is_dir()
    finally:
        await ex._release_workspace(a)
        await ex._release_workspace(b)


async def test_in_place_fallback(repo: Path):
    ex = _executor(use_worktrees=False)
    ws = await ex._acquire_workspace(str(repo), "feature/be/9-x", "development", 9)
    assert ws.is_worktree is False
    assert ws.path == str(repo)
    await ex._release_workspace(ws)  # restores base branch; must not raise


async def test_workspace_mode_runs_from_workspace(repo: Path, tmp_path: Path):
    # When workspace_directory is set, Claude runs from the workspace (so it sees
    # the shared .claude) while git operates on the repo subfolder in place.
    workspace = repo.parent
    ex = _executor(use_worktrees=True, workspace_directory=str(workspace))

    ws = await ex._acquire_workspace(str(repo), "feature/be/7-w", "development", 7)
    try:
        assert ws.is_worktree is False           # not a separate worktree
        assert ws.path == str(repo)              # git operates in the repo
        assert ws.claude_cwd == str(workspace)   # Claude runs from the workspace
    finally:
        await ex._release_workspace(ws)


def test_build_prompt_names_target_repo(repo: Path):
    from ai_autopilot.models import TaskCategory, WorkItemInfo

    ex = _executor(workspace_directory=str(repo.parent))
    item = WorkItemInfo(id=42, title="Add orders endpoint", work_item_type="Task",
                        description="Build it", acceptance_criteria="Works")
    item.category = TaskCategory.BACKEND_TASK
    prompt = ex._build_prompt(item, "/api-controller 42", str(repo))
    assert repo.name in prompt          # tells Claude which subfolder to edit
    assert "/api-controller 42" in prompt
    assert "Add orders endpoint" in prompt


async def test_agent_scratch_isolates_main_checkout(agent_workspace: Path):
    ex = _executor(
        workspace_directory=str(agent_workspace), use_worktrees=True,
        worktrees_dir=str(agent_workspace.parent / "wts"),
    )
    repos = ["repo-a", "repo-b"]
    before = {r: _rev(agent_workspace / r) for r in repos}

    scratch = await ex._acquire_agent_scratch(99, repos)
    try:
        assert scratch is not None
        sp = Path(scratch)
        assert (sp / ".claude" / "skills.md").exists()        # .claude copied in
        for r in repos:
            assert (sp / r / "README.md").exists()            # worktree checked out from base

        # The agent works + commits INSIDE the worktree (its own branch).
        wt = sp / "repo-a"
        _git(wt, "checkout", "-b", "feature/x")
        (wt / "new.txt").write_text("agent work\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "agent work")

        # Main checkout is untouched: same HEAD, clean working tree.
        assert _rev(agent_workspace / "repo-a") == before["repo-a"]
        assert _porcelain(agent_workspace / "repo-a") == ""
    finally:
        await ex.release_scratch(scratch)

    assert not Path(scratch).exists()                          # scratch cleaned up
    assert (agent_workspace / ".claude" / "skills.md").exists()  # real .claude intact


async def test_concurrent_scratch_same_repo_no_collision(agent_workspace: Path):
    import asyncio

    ex = _executor(
        workspace_directory=str(agent_workspace), use_worktrees=True,
        worktrees_dir=str(agent_workspace.parent / "wts"),
    )
    # Two tasks acquiring worktrees of the SAME repo at once must both succeed —
    # the per-repo lock serialises the git bookkeeping so they don't collide.
    a, b = await asyncio.gather(
        ex._acquire_agent_scratch(1, ["repo-a"]),
        ex._acquire_agent_scratch(2, ["repo-a"]),
    )
    try:
        assert a is not None and b is not None and a != b
        assert (Path(a) / "repo-a" / "README.md").exists()
        assert (Path(b) / "repo-a" / "README.md").exists()
    finally:
        await ex.release_scratch(a)
        await ex.release_scratch(b)


async def test_run_in_workspace_serialises_same_repo(agent_workspace: Path, monkeypatch):
    # Workspace mode checks out IN-PLACE in the shared repo, so two concurrent runs on the
    # SAME repo must be serialised (per-repo lock) — otherwise they corrupt the git index.
    import asyncio
    from types import SimpleNamespace

    from ai_autopilot.models import WorkItemInfo

    ex = _executor(
        workspace_directory=str(agent_workspace), use_worktrees=True,
        worktrees_dir=str(agent_workspace.parent / "wts"),
    )
    active = max_active = 0

    async def fake_acquire(repo_, branch, base, item_id, existing=False):
        return SimpleNamespace(path=repo_, claude_cwd=repo_)

    async def fake_release(ws):
        pass

    async def fake_claude(prompt, cwd, repo=None, on_event=None, resume=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return SimpleNamespace(text="done", total_tokens=0, cost_usd=None, is_error=False)

    async def fake_has_changes(work_dir):
        return False  # → run returns right after _run_claude (where overlap is measured)

    monkeypatch.setattr(ex, "_acquire_workspace", fake_acquire)
    monkeypatch.setattr(ex, "_release_workspace", fake_release)
    monkeypatch.setattr(ex, "_run_claude", fake_claude)
    monkeypatch.setattr(ex, "_has_changes", fake_has_changes)

    item = WorkItemInfo(id=1, title="t")
    await asyncio.gather(
        ex.revise(item, "b1", "p", repo="repo-a"),
        ex.revise(item, "b2", "p", repo="repo-a"),  # SAME repo
    )
    assert max_active == 1  # never overlapped — the per-repo lock serialised them


async def test_revise_read_only_skips_workspace(repo: Path, monkeypatch):
    # Advisory commands (/review) change nothing, so revise(read_only=True) must
    # never pay the worktree add/remove lifecycle — no workspace is acquired and
    # the run succeeds purely on Claude's comment.
    from types import SimpleNamespace

    from ai_autopilot.models import WorkItemInfo

    ex = _executor(use_worktrees=True, worktrees_dir=str(repo.parent / "wts"),
                   repo_working_directory=str(repo))

    async def fail_acquire(*args, **kwargs):
        raise AssertionError("read-only revise must not acquire a workspace")

    ran = {}

    async def fake_claude(prompt, cwd, repo=None, on_event=None, resume=None):
        ran["cwd"] = cwd
        return SimpleNamespace(text="findings posted", total_tokens=7, cost_usd=0.01,
                               is_error=False)

    monkeypatch.setattr(ex, "_acquire_workspace", fail_acquire)
    monkeypatch.setattr(ex, "_run_claude", fake_claude)

    item = WorkItemInfo(id=42, title="t")
    result = await ex.revise(item, "feature/be/42-thing", "review it", read_only=True)

    assert result.success is True
    assert result.branch_name == "feature/be/42-thing"
    assert result.cost_tokens == 7
    assert ran["cwd"] == str(repo)          # ran in place, no worktree dir
    assert not (repo.parent / "wts").exists()  # no worktree was ever created


@pytest.fixture
def repo_with_pr_branch(repo: Path) -> Path:
    """The clone plus a pushed PR branch (what a /ai revise operates on)."""
    _git(repo, "checkout", "-b", "feature/be/42-x")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "wip")
    _git(repo, "push", "-u", "origin", "feature/be/42-x")
    _git(repo, "checkout", "development")
    return repo


async def test_revise_worktree_reused_across_rounds(repo_with_pr_branch: Path, tmp_path: Path):
    # /ai round 1 creates the worktree; round 2 must find the SAME checkout (no fresh
    # materialisation) with stray files from the previous round cleaned away.
    repo = repo_with_pr_branch
    ex = _executor(use_worktrees=True, worktrees_dir=str(tmp_path / "wts"))

    ws1 = await ex._acquire_workspace(
        str(repo), "feature/be/42-x", "development", 42, existing_branch=True
    )
    (Path(ws1.path) / "junk.txt").write_text("stray\n")   # leftovers from a run
    await ex._release_workspace(ws1)
    assert Path(ws1.path).is_dir()                         # kept, not torn down

    ws2 = await ex._acquire_workspace(
        str(repo), "feature/be/42-x", "development", 42, existing_branch=True
    )
    assert ws2.path == ws1.path                            # same cached checkout
    assert not (Path(ws2.path) / "junk.txt").exists()      # reset hard each round
    assert (Path(ws2.path) / "f.txt").exists()             # at the PR head
    await ex._release_workspace(ws2)


async def test_fresh_execution_evicts_cached_revise_worktree(
    repo_with_pr_branch: Path, tmp_path: Path
):
    # A fresh execute rebuilds the branch from base with `worktree add -B` — the cached
    # revise worktree still holds that branch checked out, so it must be evicted first.
    repo = repo_with_pr_branch
    ex = _executor(use_worktrees=True, worktrees_dir=str(tmp_path / "wts"))

    cached = await ex._acquire_workspace(
        str(repo), "feature/be/42-x", "development", 42, existing_branch=True
    )
    await ex._release_workspace(cached)                    # kept in the cache

    fresh = await ex._acquire_workspace(str(repo), "feature/be/42-x", "development", 42)
    assert fresh.path != cached.path
    assert not Path(cached.path).exists()                  # evicted, no branch conflict
    await ex._release_workspace(fresh)


async def test_stale_revise_worktrees_swept(repo_with_pr_branch: Path, tmp_path: Path):
    import os

    repo = repo_with_pr_branch
    wts = tmp_path / "wts"
    ex = _executor(use_worktrees=True, worktrees_dir=str(wts), revise_worktree_ttl_hours=1)
    stale = wts / "r99-deadbeef"
    stale.mkdir(parents=True)
    os.utime(stale, (1, 1))                                # idle far past the TTL

    ws = await ex._acquire_workspace(
        str(repo), "feature/be/42-x", "development", 42, existing_branch=True
    )
    await ex._release_workspace(ws)
    assert not stale.exists()                              # swept on acquire


async def test_resolve_base_ref_falls_back_to_default(repo: Path):
    ex = _executor()  # base_branch="development" is set by the helper
    # the configured base branch exists → used directly
    assert await ex._resolve_base_ref(str(repo), "development") == "origin/development"
    # a base branch this repo doesn't have → falls back to its default branch
    ref = await ex._resolve_base_ref(str(repo), "no-such-branch")
    assert ref is not None and ref.startswith("origin/")


def test_scratch_base_defaults_inside_workspace(tmp_path: Path):
    ws = tmp_path / "workspace"
    ex = _executor(workspace_directory=str(ws))
    # A dotfolder inside the workspace (discover_repos skips it), not system-temp.
    assert ex._scratch_base() == str(ws / ".aiwt")


def test_scratch_base_honours_override(tmp_path: Path):
    ex = _executor(
        workspace_directory=str(tmp_path / "ws"), worktrees_dir=str(tmp_path / "custom")
    )
    assert ex._scratch_base() == str(tmp_path / "custom")


async def test_agent_scratch_disabled_returns_none(agent_workspace: Path):
    ex = _executor(workspace_directory=str(agent_workspace), use_worktrees=False)
    assert await ex._acquire_agent_scratch(1, ["repo-a"]) is None


async def test_release_scratch_noop_on_workspace(agent_workspace: Path):
    ex = _executor(workspace_directory=str(agent_workspace), use_worktrees=True)
    await ex.release_scratch(str(agent_workspace))            # must never delete the workspace
    assert (agent_workspace / ".claude").exists()
    assert (agent_workspace / "repo-a").exists()


async def test_prune_orphans_removes_finished_keeps_live(agent_workspace: Path):
    wts = agent_workspace.parent / "wts"
    ex = _executor(
        workspace_directory=str(agent_workspace), use_worktrees=True, worktrees_dir=str(wts)
    )
    # A finished session wrote its result → safe to remove.
    finished = wts / "agent-5-deadbeef"
    (finished / ".autopilot" / "runs").mkdir(parents=True)
    (finished / ".autopilot" / "runs" / "5.json").write_text('{"status":"completed"}')
    # A recent session with no result → assume a live interactive session → keep.
    live = wts / "agent-6-feedface"
    live.mkdir(parents=True)
    (live / "working.txt").write_text("in progress")

    await ex.prune_orphans()

    assert not finished.exists()   # done → cleaned up
    assert live.exists()           # recent + no result → preserved (don't kill a live session)


def test_build_prompt_legacy_is_skill_only(repo: Path):
    from ai_autopilot.models import TaskCategory, WorkItemInfo

    ex = _executor()  # no workspace_directory → legacy behaviour
    item = WorkItemInfo(id=42, title="x", work_item_type="Task")
    item.category = TaskCategory.BACKEND_TASK
    assert ex._build_prompt(item, "/api-controller 42", str(repo)) == "/api-controller 42"
