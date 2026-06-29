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
