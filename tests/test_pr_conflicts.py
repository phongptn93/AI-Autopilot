"""PR merge conflicts — detection, tracking, and resolution against REAL git repos.

The resolver tests build an origin with a PR branch and a target that changed the same
line, then drive ``ConflictResolver`` with a fake model run. What is under test is what
matters when a bot writes to someone's branch: a good resolution is merged and pushed as
a merge commit (two parents, no force), and every bad one — markers left, other files
touched, the agent asking for a human, a lock file — leaves origin EXACTLY as it was.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_autopilot import delivery, pr_conflicts
from ai_autopilot.config import Settings
from ai_autopilot.data import Database, PrConflictRepository
from ai_autopilot.execution.claude_executor import ClaudeExecutor
from ai_autopilot.execution.conflict_resolver import ConflictResolver
from ai_autopilot.pr_conflicts import ResolveContext

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for k, v in _GIT_ENV.items():
        monkeypatch.setenv(k, v)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _setup(tmp_path: Path, *, conflict: bool = True, extra_target: dict | None = None):
    """origin (bare) + a workspace clone at ws/app with branches main and feature."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(origin), str(seed))
    (seed / "app.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (seed / "other.py").write_text("keep = True\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "init")
    _git(seed, "push", "origin", "main")
    _git(seed, "checkout", "-b", "feature")
    (seed / "app.py").write_text("x = 10  # feature\ny = 2\n", encoding="utf-8")
    _git(seed, "commit", "-am", "feature change")
    _git(seed, "push", "origin", "feature")
    _git(seed, "checkout", "main")
    if conflict:
        (seed / "app.py").write_text("x = 100  # main\ny = 2\n", encoding="utf-8")
    else:
        (seed / "other.py").write_text("keep = False\n", encoding="utf-8")
    for name, body in (extra_target or {}).items():
        (seed / name).write_text(body, encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "main change")
    _git(seed, "push", "origin", "main")
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "clone", str(origin), "app")
    return origin, ws


def _executor(ws: Path, **overrides) -> ClaudeExecutor:
    cfg = Settings(workspace_directory=str(ws), test_gate_enabled=False, **overrides)
    return ClaudeExecutor(cfg, reviewer=None, session_repo=None)


def _fake_claude(ex: ClaudeExecutor, write, text: str = "RESOLUTION: done — kept both"):
    calls: list[str] = []

    async def run(prompt, cwd, repo=None, **kw):
        calls.append(prompt)
        write(Path(repo))
        return SimpleNamespace(text=text, input_tokens=100, output_tokens=20)

    ex._run_claude = run  # type: ignore[method-assign]
    return calls


def _ctx() -> ResolveContext:
    return ResolveContext(pr_id=7, title="feature", source_branch="feature",
                          target_branch="main")


def _origin_head(origin: Path, branch: str) -> str:
    return _git(origin, "rev-parse", branch)


async def test_agent_resolution_is_merged_and_pushed(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _executor(ws)
    calls = _fake_claude(ex, lambda repo: (repo / "app.py").write_text(
        "x = 110  # main + feature\ny = 2\n", encoding="utf-8"))
    res = await ConflictResolver(ex, ex._config).resolve(
        repo_path=str(ws / "app"), branch="feature", target_branch="main", ctx=_ctx())
    assert res.success, res.error
    assert res.how == pr_conflicts.BY_AGENT and res.files == ["app.py"]
    assert res.checks["markers"] == "ok" and res.checks["scope"] == "ok"
    assert calls and "app.py" in calls[0] and "RESOLUTION" in calls[0]
    after = _origin_head(origin, "feature")
    assert after != before
    parents = _git(origin, "rev-list", "--parents", "-n", "1", after).split()
    assert len(parents) == 3                      # a merge commit: itself + two parents
    assert before in parents                       # the PR's own history is kept, not rewritten
    assert "110" in _git(origin, "show", f"{after}:app.py")


async def test_markers_left_aborts_and_origin_unchanged(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _executor(ws)
    _fake_claude(ex, lambda repo: None)            # agent did nothing: markers remain
    res = await ConflictResolver(ex, ex._config).resolve(
        repo_path=str(ws / "app"), branch="feature", target_branch="main", ctx=_ctx())
    assert not res.success and "marker" in res.error
    assert _origin_head(origin, "feature") == before
    assert not (ws / "app" / ".git" / "MERGE_HEAD").exists()   # merge aborted locally


async def test_touching_another_file_is_refused(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _executor(ws)

    def write(repo: Path):
        (repo / "app.py").write_text("x = 110\ny = 2\n", encoding="utf-8")
        (repo / "other.py").write_text("keep = 'sneaky'\n", encoding="utf-8")

    _fake_claude(ex, write)
    res = await ConflictResolver(ex, ex._config).resolve(
        repo_path=str(ws / "app"), branch="feature", target_branch="main", ctx=_ctx())
    assert not res.success and "other.py" in res.error
    assert _origin_head(origin, "feature") == before


async def test_agent_asking_for_a_human_is_escalated(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _executor(ws)
    _fake_claude(ex, lambda repo: (repo / "app.py").write_text("x = 1\ny = 2\n",
                                                               encoding="utf-8"),
                 text="RESOLUTION: needs-human — x is a pricing constant, pick one")
    res = await ConflictResolver(ex, ex._config).resolve(
        repo_path=str(ws / "app"), branch="feature", target_branch="main", ctx=_ctx())
    assert not res.success and "pricing constant" in res.error
    assert _origin_head(origin, "feature") == before


async def test_clean_merge_needs_no_model(tmp_path):
    origin, ws = _setup(tmp_path, conflict=False)
    before = _origin_head(origin, "feature")
    ex = _executor(ws)
    calls = _fake_claude(ex, lambda repo: None)
    res = await ConflictResolver(ex, ex._config).resolve(
        repo_path=str(ws / "app"), branch="feature", target_branch="main", ctx=_ctx())
    assert res.success and res.how == pr_conflicts.BY_CLEAN_MERGE
    assert calls == []                              # no tokens spent on a clean merge
    assert _origin_head(origin, "feature") != before


async def test_lock_file_conflict_escalates_before_any_model_call(tmp_path):
    origin, ws = _setup(tmp_path)
    seed = tmp_path / "seed"
    # Put a conflicting lock file on both sides.
    _git(seed, "checkout", "feature")
    (seed / "package-lock.json").write_text('{"v": "feature"}\n', encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "lock feature")
    _git(seed, "push", "origin", "feature")
    _git(seed, "checkout", "main")
    (seed / "package-lock.json").write_text('{"v": "main"}\n', encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "lock main")
    _git(seed, "push", "origin", "main")
    _git(ws / "app", "fetch", "origin")
    before = _origin_head(origin, "feature")
    ex = _executor(ws)
    calls = _fake_claude(ex, lambda repo: None)
    res = await ConflictResolver(ex, ex._config).resolve(
        repo_path=str(ws / "app"), branch="feature", target_branch="main", ctx=_ctx())
    assert not res.success and "package-lock.json" in res.error
    assert calls == [] and _origin_head(origin, "feature") == before


async def test_resolution_that_invents_a_secret_is_refused(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _executor(ws)
    key = "AKIA" + "IOSFODNN7QZXWVTR"
    _fake_claude(ex, lambda repo: (repo / "app.py").write_text(
        f"x = 110\ny = 2\naws = '{key}'\n", encoding="utf-8"))
    res = await ConflictResolver(ex, ex._config).resolve(
        repo_path=str(ws / "app"), branch="feature", target_branch="main", ctx=_ctx())
    assert not res.success and "secret-aws-access-key" in res.error
    assert _origin_head(origin, "feature") == before


# ── pure helpers ────────────────────────────────────────────────────────────

def test_is_conflicted_and_markers():
    assert pr_conflicts.is_conflicted({"mergeStatus": "conflicts"})
    assert pr_conflicts.is_conflicted({"mergeStatus": 2})
    assert not pr_conflicts.is_conflicted({"mergeStatus": "queued"})
    assert pr_conflicts.has_markers("a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> main\n")
    assert not pr_conflicts.has_markers("a = '<<<<<<<' inside a string\n")
    assert pr_conflicts.unresolvable(["src/a.cs", "package-lock.json", "img/logo.png"]) == [
        "package-lock.json", "img/logo.png"]
    assert pr_conflicts.parse_verdict("...\nRESOLUTION: done — ok") == ("done", "ok")


def test_conflicted_pr_is_not_merge_ready_and_leads_the_report():
    from datetime import UTC, datetime

    pr = delivery.PrView(id=1, approved=2, conflicts=True, created_at=datetime.now(UTC))
    assert not pr.is_ready_to_merge
    ok = delivery.PrView(id=2, approved=2, created_at=datetime.now(UTC))
    assert ok.is_ready_to_merge
    thr = SimpleNamespace(merge_hours=0, review_hours=0)
    kinds = [a.kind for a in delivery._pr_actions([pr], thr, datetime.now(UTC))]
    assert kinds == [delivery.KIND_CONFLICT_PR]


# ── repository: episodes and attempt claims ─────────────────────────────────

async def test_repository_episodes_and_attempt_budget(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    await db.create_all()
    repo = PrConflictRepository(db)
    try:
        row, fresh = await repo.observe("r1", 7, title="t", files=["a.cs"], target_commit="c1")
        assert fresh and json.loads(row.files_json) == ["a.cs"]
        row2, fresh2 = await repo.observe("r1", 7, target_commit="c1")
        assert not fresh2 and row2.id == row.id
        assert await repo.claim_attempt(row.id, "c1", max_attempts=1)
        assert not await repo.claim_attempt(row.id, "c1", 1)     # resolving: no second run
        await repo.update(row.id, status="escalated")
        assert not await repo.claim_attempt(row.id, "c1", 1)     # same inputs, budget spent
        assert await repo.claim_attempt(row.id, "c2", 1)         # target moved: new attempt
        await repo.update(row.id, status="resolved")
        _, fresh3 = await repo.observe("r1", 7, target_commit="c3")
        assert fresh3                                            # new episode after resolve
        assert await repo.reset_stuck() == 0
    finally:
        await db.dispose()


# ── regression: the per-repo lock must not deadlock its own holder ──────────

async def test_repo_lock_is_reentrant_for_its_holder_only():
    import asyncio

    from ai_autopilot.execution.claude_executor import _TaskReentrantLock

    lock = _TaskReentrantLock()
    async with lock:
        async with lock:                       # nested in the SAME task: no deadlock
            assert lock.locked()
        assert lock.locked()                   # still held by the outer section
        other = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0.01)
        assert not other.done()                # a DIFFERENT task still waits
    await asyncio.wait_for(other, 1)
    lock.release()
    assert not lock.locked()


async def test_workspace_mode_revise_path_no_longer_hangs(tmp_path):
    """The pre-existing bug: workspace mode held _repo_lock for the whole run and the
    checkout asked for it again — every PR revise in that mode waited forever."""
    import asyncio

    origin, ws = _setup(tmp_path, conflict=False)
    ex = _executor(ws)
    _fake_claude(ex, lambda repo: None)
    ws_obj = None

    async def run():
        nonlocal ws_obj
        lock = ex._repo_lock(str(ws / "app"))
        async with lock:
            ws_obj = await ex._acquire_workspace(str(ws / "app"), "feature", "main", 1,
                                                 existing_branch=True)

    await asyncio.wait_for(run(), timeout=30)
    assert ws_obj is not None and _git(ws / "app", "branch", "--show-current") == "feature"


# ── dashboard ───────────────────────────────────────────────────────────────

def test_conflicts_page_lists_tracked_conflicts(tmp_path):
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app

    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    with TestClient(create_app(settings)) as client:
        c = client.app.state.container
        client.portal.call(lambda: c.pr_conflict_repo.observe(
            "r1", 42, repo_name="Backend-Fresh", title="Add filter config",
            source_branch="feature/x", target_branch="development",
            files=["Plugins/A.cs"], owned=True))
        page = client.get("/dashboard/conflicts").text
        assert "!42" in page and "Add filter config" in page and "Plugins/A.cs" in page
        assert "feature/x" in page and "development" in page and "▶ Resolve" in page
        assert 'href="/dashboard/conflicts"' in page             # nav link
