"""`/ai` on a PR as an interactive session — real git, fake console.

Same fixture as the conflict tests (an origin with a PR branch ``feature``). The console
launch is replaced by a stand-in; the test plays the session's part (edit, maybe commit,
write the result). What must hold is what the headless revise guarantees, and more:
nothing reaches origin unless the branch did not move, the change is real, and the test
gate + auto-review pass FIRST; the thread is answered exactly as for a headless run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from ai_autopilot.data import Database, PrSessionRepository
from ai_autopilot.execution.claude_executor import ClaudeExecutor
from ai_autopilot.execution.revise_session import ReviseSessions
from tests.test_pr_conflicts import (  # noqa: F401 — _git_identity is an autouse fixture
    _executor,
    _git,
    _git_identity,
    _origin_head,
    _setup,
)


def _ex(ws: Path, **kw) -> ClaudeExecutor:
    return _executor(ws, use_worktrees=True, base_branch="main",
                     execution_mode="interactive", **kw)


def _no_console(ex: ClaudeExecutor) -> list[str]:
    launched: list[str] = []

    def launch(cwd, session, prompt, *, resuming=False):
        launched.append(session)
        return 424242
    ex._launch_console = launch            # type: ignore[method-assign]
    return launched


def _result(run_dir: str, key: str, **data) -> None:
    runs = Path(run_dir) / ".autopilot" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")


async def _open(ex, key="pr-7"):
    return await ReviseSessions(ex, ex._config).prepare(
        item_id=42, repo_name="app", branch="feature", prompt="Add a docstring.", key=key)


async def test_session_commit_is_pushed_after_the_gates(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _ex(ws)
    launched = _no_console(ex)
    s = await _open(ex)
    assert not isinstance(s, str), s
    assert launched == ["autopilot-pr-7"]
    brief = (Path(s.run_dir) / ".autopilot/runs/pr-7.brief.md").read_text(encoding="utf-8")
    assert "Add a docstring." in brief and "Do NOT push" in brief
    sessions = ReviseSessions(ex, ex._config)
    assert await sessions.finalize(s.run_dir, "pr-7") is None          # still working

    wt = Path(s.run_dir) / "app"
    (wt / "app.py").write_text('"""Doc."""\nx = 10  # feature\ny = 2\n', encoding="utf-8")
    _git(wt, "commit", "-am", "docs: add module docstring")             # a local commit
    (wt / "other.py").write_text("keep = True  # tidy\n", encoding="utf-8")  # left uncommitted
    _result(s.run_dir, "pr-7", status="completed", summary="added a docstring")
    res = await sessions.finalize(s.run_dir, "pr-7")
    assert res.success, res.error
    after = _origin_head(origin, "feature")
    assert after != before
    assert _git(origin, "merge-base", "--is-ancestor", before, after) == ""   # fast-forward
    assert sorted(res.files_changed) == ["app.py", "other.py"]
    assert not Path(s.run_dir).exists()


async def test_no_change_is_the_same_failure_as_headless(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _ex(ws)
    _no_console(ex)
    s = await _open(ex)
    _result(s.run_dir, "pr-7", status="completed", summary="nothing to do")
    res = await ReviseSessions(ex, ex._config).finalize(s.run_dir, "pr-7")
    assert not res.success and res.error == "No file changes produced"
    assert _origin_head(origin, "feature") == before


async def test_branch_moved_during_session_is_not_overwritten(tmp_path):
    origin, ws = _setup(tmp_path)
    ex = _ex(ws)
    _no_console(ex)
    s = await _open(ex)
    seed = tmp_path / "seed"
    _git(seed, "checkout", "feature")
    (seed / "new.py").write_text("n = 1\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "teammate")
    _git(seed, "push", "origin", "feature")
    theirs = _origin_head(origin, "feature")
    (Path(s.run_dir) / "app" / "app.py").write_text("x = 11\ny = 2\n", encoding="utf-8")
    _result(s.run_dir, "pr-7", status="completed", summary="changed x")
    res = await ReviseSessions(ex, ex._config).finalize(s.run_dir, "pr-7")
    assert not res.success and "commit mới" in res.error
    assert _origin_head(origin, "feature") == theirs


async def test_red_test_gate_means_nothing_is_pushed(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _ex(ws)
    ex._config.test_gate_enabled = True
    ex._config.test_command = "exit 1"
    _no_console(ex)
    s = await _open(ex)
    (Path(s.run_dir) / "app" / "app.py").write_text("x = 11\ny = 2\n", encoding="utf-8")
    _result(s.run_dir, "pr-7", status="completed", summary="changed x")
    res = await ReviseSessions(ex, ex._config).finalize(s.run_dir, "pr-7")
    assert not res.success and res.error.startswith("Tests failed")
    assert _origin_head(origin, "feature") == before


async def test_blocking_auto_review_means_nothing_is_pushed(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _ex(ws)

    async def review(wd, base=""):
        return SimpleNamespace(passed=False, critical_issues=["[High] SQL built by concat"])

    ex._reviewer = SimpleNamespace(review=review)
    _no_console(ex)
    s = await _open(ex)
    (Path(s.run_dir) / "app" / "app.py").write_text("x = 11\ny = 2\n", encoding="utf-8")
    _result(s.run_dir, "pr-7", status="completed", summary="changed x")
    res = await ReviseSessions(ex, ex._config).finalize(s.run_dir, "pr-7")
    assert not res.success and "Auto-review blocked" in res.error
    assert _origin_head(origin, "feature") == before


# ── the babysitter: /ai opens a session, the scan answers the thread ────────

class _Ado:
    def __init__(self):
        self.replies: list[str] = []
        self.statuses: list[str] = []
        self.item_comments: list[str] = []

    async def reply_to_pull_request_thread(self, repo_id, pr_id, tid, text):
        self.replies.append(text)
        return True

    async def set_pull_request_thread_status(self, repo_id, pr_id, tid, status):
        self.statuses.append(status)
        return True

    async def add_comment(self, wid, text):
        self.item_comments.append(text)
        return True

    async def get_work_item(self, wid):
        return None

    async def get_repositories(self):
        return []


async def _monitor(tmp_path, ex):
    from ai_autopilot.services.pr_monitor import PrMonitorService

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'mon.sqlite'}")
    await db.create_all()

    class _Feedback:
        def build_prompt(self, item, branch, feedback, review_only=False):
            return f"Do what the reviewer asked: {feedback}"

    c = SimpleNamespace(config=ex._config, executor=ex, ado=_Ado(), feedback=_Feedback(),
                        pr_session_repo=PrSessionRepository(db))
    mon = PrMonitorService(c)
    mon._mark_hot = lambda *a, **k: None          # type: ignore[method-assign]
    mon._adjust_related_drafts = _noop            # type: ignore[method-assign]
    return mon, c, db


async def _noop(*a, **k):
    return None


def _cmd(text="/ai rename x to value"):
    return {"thread_id": 5, "comment_id": 9, "instruction": text, "author_name": "Phong"}


async def test_babysitter_runs_ai_as_a_session_and_reports_on_scan(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _ex(ws)
    _no_console(ex)
    mon, c, db = await _monitor(tmp_path, ex)
    try:
        await mon._handle_command("r1", "app", 7, 42, "feature", None, _cmd(), 1)
        assert "Đã nhận" in c.ado.replies[0] and "interactive" in c.ado.replies[0]
        assert "autopilot-pr-7" in c.ado.replies[-1]
        row = (await c.pr_session_repo.open_sessions())[0]
        assert row.pr_id == 7 and row.thread_id == 5 and row.branch == "feature"
        # A second /ai on the same PR while the session is open is told to wait.
        await mon._handle_command("r1", "app", 7, 42, "feature", None, _cmd("/ai more"), 2)
        assert "đang có một phiên interactive mở" in c.ado.replies[-1]
        assert await mon._finalize_sessions() == 0                # still working

        (Path(row.run_dir) / "app" / "app.py").write_text("value = 10\ny = 2\n",
                                                          encoding="utf-8")
        _result(row.run_dir, row.key, status="completed", summary="renamed x")
        assert await mon._finalize_sessions() == 1
        assert "✅ Đã xử lý xong" in c.ado.replies[-1] and c.ado.statuses[-1] == "fixed"
        assert _origin_head(origin, "feature") != before
        assert await c.pr_session_repo.open_sessions() == []
    finally:
        await db.dispose()


async def test_babysitter_closes_a_session_that_ran_too_long(tmp_path):
    origin, ws = _setup(tmp_path)
    before = _origin_head(origin, "feature")
    ex = _ex(ws)
    _no_console(ex)
    mon, c, db = await _monitor(tmp_path, ex)
    try:
        await mon._handle_command("r1", "app", 7, 42, "feature", None, _cmd(), 1)
        row = (await c.pr_session_repo.open_sessions())[0]
        async with db.session() as session:
            from ai_autopilot.data import PrSession
            r = await session.get(PrSession, row.id)
            r.started = datetime.now(UTC) - timedelta(hours=9)
            await session.commit()
        assert await mon._finalize_sessions() == 1
        assert "quá" in c.ado.replies[-1] and c.ado.statuses[-1] == "active"
        assert _origin_head(origin, "feature") == before
        assert not Path(row.run_dir).exists()
    finally:
        await db.dispose()


async def test_advisory_command_stays_headless_in_interactive_mode(tmp_path):
    _, ws = _setup(tmp_path)
    ex = _ex(ws)
    launched = _no_console(ex)
    mon, c, db = await _monitor(tmp_path, ex)
    ran: list[str] = []

    async def handle_feedback(item, branch, instruction, revision, **kw):
        ran.append(instruction)
        from ai_autopilot.models import ExecutionResult
        return ExecutionResult.ok(42, "review", "looks fine")

    c.feedback.handle_feedback = handle_feedback
    mon._bot_comment_ids = lambda *a: _set()               # type: ignore[method-assign]
    try:
        await mon._handle_command("r1", "app", 7, 42, "feature", None,
                                  _cmd("/review please"), 1)
        assert ran == ["/review please"] and launched == []   # no console for a review
        assert await c.pr_session_repo.open_sessions() == []
    finally:
        await db.dispose()


async def _set():
    return set()
