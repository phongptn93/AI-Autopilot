"""PR conflict resolution in INTERACTIVE mode — a session a person can attach to.

Same real-git fixture as ``test_pr_conflicts``. The console launch is replaced by a
"session" that edits files and writes the result contract, which is exactly what a real
Remote-Control session hands back. What must hold: the session's verdict is only an
input — the same objective checks run before anything is pushed, a branch that moved
during the session is never overwritten, and the scratch is always cleaned up.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from ai_autopilot import pr_conflicts
from ai_autopilot.data import Database, PrConflictRepository
from ai_autopilot.execution.claude_executor import ClaudeExecutor
from ai_autopilot.execution.conflict_resolver import ConflictResolver
from tests.test_pr_conflicts import (  # noqa: F401 — _git_identity is an autouse fixture
    _ctx,
    _executor,
    _git,
    _git_identity,
    _origin_head,
    _setup,
)


def _interactive_executor(ws: Path) -> ClaudeExecutor:
    return _executor(ws, use_worktrees=True, base_branch="main",
                     execution_mode="interactive")


def _session(ex: ClaudeExecutor) -> list[tuple[str, str]]:
    """Replace the console launch; the test then plays the session's part."""
    launched: list[tuple[str, str]] = []

    def launch(cwd, session, prompt, *, resuming=False):
        launched.append((cwd, session))
        return 424242                      # a pid that is not running

    ex._launch_console = launch            # type: ignore[method-assign]
    return launched


def _write_result(scratch: Path, key: str, **data) -> None:
    runs = scratch / ".autopilot" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")


async def _prepare(ex, key):
    return await ConflictResolver(ex, ex._config).prepare_session(
        repo_name="app", branch="feature", target_branch="main", ctx=_ctx(), key=key)


async def test_session_resolves_and_pushes_after_checks(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _interactive_executor(ws)
    launched = _session(ex)
    r = ConflictResolver(ex, ex._config)
    prepared = await _prepare(ex, "conflict-1")
    assert not isinstance(prepared, pr_conflicts.ConflictResolution), prepared
    assert launched and launched[0][1] == "autopilot-conflict-1"
    scratch = Path(prepared.run_dir)
    brief = (scratch / ".autopilot" / "runs" / "conflict-1.brief.md").read_text(encoding="utf-8")
    assert "Do NOT `git add`, `git commit`" in brief and "app.py" in brief
    assert "<<<<<<<" in (scratch / "app" / "app.py").read_text(encoding="utf-8")
    assert await r.finalize_session(prepared.run_dir, "conflict-1") is None   # still working

    (scratch / "app" / "app.py").write_text("x = 110\ny = 2\n", encoding="utf-8")
    _write_result(scratch, "conflict-1", status="completed", summary="kept both")
    res = await r.finalize_session(prepared.run_dir, "conflict-1")
    assert res.success, res.error
    after = _origin_head(origin, "feature")
    parents = _git(origin, "rev-list", "--parents", "-n", "1", after).split()
    assert len(parents) == 3 and before in parents       # a merge commit, history kept
    assert not scratch.exists()                          # cleaned up


async def test_needs_human_leaves_origin_untouched(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _interactive_executor(ws)
    _session(ex)
    prepared = await _prepare(ex, "conflict-2")
    _write_result(Path(prepared.run_dir), "conflict-2", status="needs_human",
                  reason="pricing constant — product must choose")
    res = await ConflictResolver(ex, ex._config).finalize_session(prepared.run_dir, "conflict-2")
    assert not res.success and "pricing constant" in res.error
    assert _origin_head(origin, "feature") == before
    assert not Path(prepared.run_dir).exists()


async def test_refuses_when_branch_moved_during_session(tmp_path):
    origin, ws = _setup(tmp_path)
    ex = _interactive_executor(ws)
    _session(ex)
    prepared = await _prepare(ex, "conflict-3")
    # A teammate pushes to the PR branch while the session is open.
    seed = tmp_path / "seed"
    _git(seed, "checkout", "feature")
    (seed / "new.py").write_text("n = 1\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "teammate")
    _git(seed, "push", "origin", "feature")
    theirs = _origin_head(origin, "feature")
    scratch = Path(prepared.run_dir)
    (scratch / "app" / "app.py").write_text("x = 110\ny = 2\n", encoding="utf-8")
    _write_result(scratch, "conflict-3", status="completed", summary="ok")
    res = await ConflictResolver(ex, ex._config).finalize_session(prepared.run_dir, "conflict-3")
    assert not res.success and "commit mới" in res.error
    assert _origin_head(origin, "feature") == theirs     # their commit is not overwritten


async def test_scope_guard_still_applies_to_a_session(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _interactive_executor(ws)
    _session(ex)
    prepared = await _prepare(ex, "conflict-4")
    scratch = Path(prepared.run_dir)
    (scratch / "app" / "app.py").write_text("x = 110\ny = 2\n", encoding="utf-8")
    (scratch / "app" / "other.py").write_text("keep = 'edited in session'\n", encoding="utf-8")
    _write_result(scratch, "conflict-4", status="completed", summary="also tidied other.py")
    res = await ConflictResolver(ex, ex._config).finalize_session(prepared.run_dir, "conflict-4")
    assert not res.success and "other.py" in res.error
    assert _origin_head(origin, "feature") == before


async def test_clean_merge_opens_no_session(tmp_path):
    _, ws = _setup(tmp_path, conflict=False)
    ex = _interactive_executor(ws)
    launched = _session(ex)
    res = await _prepare(ex, "conflict-5")
    assert isinstance(res, pr_conflicts.ConflictResolution)
    assert res.success and res.how == pr_conflicts.BY_CLEAN_MERGE and launched == []


# ── the service: follows execution_mode, finalises on scan, times out ───────

class _AdoStub:
    def __init__(self):
        self.comments: list[str] = []

    async def get_pull_request(self, repo_id, pr_id):
        return {"description": "", "status": "active"}

    async def get_work_item(self, wid):
        return None

    async def add_pull_request_comment(self, repo_id, pr_id, text, active=False):
        self.comments.append(text)
        return True


async def _service(tmp_path, ex):
    from ai_autopilot.services.pr_conflicts import PrConflictService

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'svc.sqlite'}")
    await db.create_all()

    async def _noop(*a, **kw):
        return None

    c = SimpleNamespace(
        config=ex._config, executor=ex, pr_conflict_repo=PrConflictRepository(db),
        ado=_AdoStub(), audit_repo=SimpleNamespace(record=_noop),
        notifier=SimpleNamespace(notify=_noop),
    )
    return PrConflictService(c), c, db


async def _observe(c, pr_id):
    row, _ = await c.pr_conflict_repo.observe(
        "r1", pr_id, repo_name="app", source_branch="feature", target_branch="main",
        files=["app.py"], target_commit="t1")
    return row


async def test_service_opens_a_session_and_finalises_it_on_scan(tmp_path):
    _, ws = _setup(tmp_path)
    ex = _interactive_executor(ws)
    _session(ex)
    svc, c, db = await _service(tmp_path, ex)
    try:
        row = await _observe(c, 7)
        assert await svc.resolve(row.id, requested_by="dashboard") == "in_session"
        row = await c.pr_conflict_repo.get(row.id)
        assert row.status == "in_session" and row.session_name == f"autopilot-conflict-{row.id}"
        assert any("phiên interactive" in t for t in c.ado.comments)
        assert await svc._finalize_sessions() == 0            # session still working
        # A second request while the session is open is refused, not doubled.
        assert await svc.resolve(row.id, requested_by="dashboard") == "skipped"

        scratch = Path(row.session_dir)
        (scratch / "app" / "app.py").write_text("x = 110\ny = 2\n", encoding="utf-8")
        _write_result(scratch, f"conflict-{row.id}", status="completed", summary="both kept")
        assert await svc._finalize_sessions() == 1
        row = await c.pr_conflict_repo.get(row.id)
        assert row.status == "resolved" and row.resolved_by == pr_conflicts.BY_AGENT
        assert row.merge_commit and row.session_dir == ""
    finally:
        await db.dispose()


async def test_service_closes_a_session_that_ran_too_long(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _interactive_executor(ws)
    _session(ex)
    svc, c, db = await _service(tmp_path, ex)
    try:
        row = await _observe(c, 8)
        assert await svc.resolve(row.id, requested_by="dashboard") == "in_session"
        session_dir = Path((await c.pr_conflict_repo.get(row.id)).session_dir)
        await c.pr_conflict_repo.update(
            row.id, session_started=datetime.now(UTC) - timedelta(hours=9))
        assert await svc._finalize_sessions() == 1
        row = await c.pr_conflict_repo.get(row.id)
        assert row.status == "escalated" and "quá" in row.last_error
        assert _origin_head(origin, "feature") == before
        assert not session_dir.exists()
    finally:
        await db.dispose()


async def test_service_uses_headless_when_execution_mode_is_headless(tmp_path):
    _, ws = _setup(tmp_path)
    ex = _executor(ws, execution_mode="headless")
    launched = _session(ex)

    async def run(prompt, cwd, repo=None, **kw):
        (Path(repo) / "app.py").write_text("x = 110\ny = 2\n", encoding="utf-8")
        return SimpleNamespace(text="RESOLUTION: done — kept both", input_tokens=1,
                               output_tokens=1)

    ex._run_claude = run                   # type: ignore[method-assign]
    svc, c, db = await _service(tmp_path, ex)
    try:
        row = await _observe(c, 9)
        assert await svc.resolve(row.id, requested_by="dashboard") == "resolved"
        assert launched == []                                  # no console in headless mode
    finally:
        await db.dispose()
