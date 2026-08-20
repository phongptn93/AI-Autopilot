"""Tests for pure helpers in the executor / notifications."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ai_autopilot.config import Settings
from ai_autopilot.execution.claude_executor import (
    ClaudeExecutor,
    _branch_name,
    _extract_pr_url,
    _slugify,
)
from ai_autopilot.models import TaskCategory, WorkItemInfo
from ai_autopilot.notifications.base import (
    NotificationMessage,
    NotificationType,
)


def test_slugify():
    assert _slugify("Add Login API!") == "add-login-api"
    assert _slugify("  Trim  Spaces  ") == "trim-spaces"


def test_branch_name_uses_category_prefix():
    item = WorkItemInfo(id=42, title="Fix crash", category=TaskCategory.BUG)
    assert _branch_name(item) == "fix/42-fix-crash"

    be = WorkItemInfo(id=7, title="Orders API", category=TaskCategory.BACKEND_TASK)
    assert _branch_name(be) == "feature/be/7-orders-api"


def test_extract_pr_url_from_output():
    output = "Created PR\nhttps://dev.azure.com/org/proj/_git/repo/pullrequest/123 done"
    assert _extract_pr_url(output) == "https://dev.azure.com/org/proj/_git/repo/pullrequest/123"


def test_extract_pr_url_none_when_absent():
    assert _extract_pr_url("nothing here") is None


def test_notification_titles():
    item = WorkItemInfo(id=5, title="x")
    started = NotificationMessage(work_item=item, type=NotificationType.STARTED)
    assert started.title == "🤖 Processing #5"
    err = NotificationMessage(work_item=item, type=NotificationType.ERROR, error="bad")
    assert err.title == "⚠️ Error #5"


# ── PR-feedback revise: resolve the PR's repo in workspace mode ─────────────────

def test_revise_repo_uses_workspace_subfolder(tmp_path):
    # In workspace mode the PR's repo lives at <workspace>/<repo_name>.
    (tmp_path / "Backend-Fresh").mkdir()
    cfg = Settings(
        workspace_directory=str(tmp_path), base_branch="dxfac/development",
        repo_working_directory="/nonexistent/placeholder",  # the legacy trap
    )
    ex = ClaudeExecutor(cfg, None)
    path, base = ex._revise_repo(WorkItemInfo(id=1, title="t"), "Backend-Fresh")
    assert path == str(tmp_path / "Backend-Fresh")           # NOT the placeholder
    assert base == "dxfac/development"


def test_revise_repo_falls_back_when_folder_missing(tmp_path):
    cfg = Settings(workspace_directory=str(tmp_path), repo_working_directory="/legacy/repo")
    ex = ClaudeExecutor(cfg, None)
    # No "Micro-Frontend" subfolder created → fall back to legacy mapping.
    path, _ = ex._revise_repo(WorkItemInfo(id=1, title="t"), "Micro-Frontend")
    assert path == "/legacy/repo"


def test_revise_repo_falls_back_without_repo_name(tmp_path):
    cfg = Settings(workspace_directory=str(tmp_path), repo_working_directory="/legacy/repo")
    ex = ClaudeExecutor(cfg, None)
    path, _ = ex._revise_repo(WorkItemInfo(id=1, title="t"), "")   # no repo name given
    assert path == "/legacy/repo"


async def test_close_interactive_kills_the_launched_console(tmp_path):
    """The whole point of recording a pid: a finished session must actually die.

    Spawns a real sleeper carrying the session name in its command line (which is
    also what the pid-reuse guard matches on), then asserts close_interactive ends
    it and clears the handle.
    """
    import subprocess
    import sys

    session = "autopilot-91234"
    proc = subprocess.Popen(  # noqa: ASYNC220 — a real OS process is the point here
        [sys.executable, "-c", f"import time; time.sleep(120)  # {session}"]
    )
    try:
        cfg = Settings(workspace_directory=str(tmp_path / "ws"))
        ex = ClaudeExecutor(cfg, None)
        run_dir = str(tmp_path / "scratch")
        ex._write_session_handle(run_dir, 91234, proc.pid, session)
        assert await ex.close_interactive(run_dir, 91234) is True
        assert proc.wait(timeout=30) is not None                    # really gone
        # The handle stays (minus the pid): it also marks the scratch as reserved for
        # this item's rework — it is the scratch's own teardown that removes it.
        assert "pid" not in ex._read_session_handle(run_dir, 91234)
        # Second call is a no-op, not an error (finalise can run twice).
        assert await ex.close_interactive(run_dir, 91234) is False
    finally:
        if proc.poll() is None:
            proc.kill()


def test_list_open_sessions_ignores_headless_scratches(tmp_path):
    cfg = Settings(workspace_directory=str(tmp_path / "ws"), worktrees_dir=str(tmp_path / "wt"))
    ex = ClaudeExecutor(cfg, None)
    base = Path(ex._scratch_base())
    for name, item_id in (("agent-42", 42), ("agent-77-a1b2c3d4", 77)):
        runs = base / name / ".autopilot" / "runs"
        runs.mkdir(parents=True)
        (runs / f"{item_id}.session.json").write_text("{}", encoding="utf-8")
    assert ex.list_open_sessions() == {42: str(base / "agent-42")}


def _rework_exec(tmp_path, **overrides):
    cfg = Settings(
        workspace_directory=str(tmp_path / "ws"), worktrees_dir=str(tmp_path / "wt"), **overrides
    )
    return ClaudeExecutor(cfg, None)


def _seed_scratch(ex, item_id: int, repo_name: str = "api", *, worktree: bool = True):
    scratch = Path(ex.interactive_scratch_dir(item_id))
    (scratch / ".autopilot" / "runs").mkdir(parents=True, exist_ok=True)
    ex._write_session_handle(str(scratch), item_id, 4321, f"autopilot-{item_id}")
    if worktree:
        (scratch / repo_name / ".git").mkdir(parents=True, exist_ok=True)
    return scratch


def test_rework_reuses_the_scratch_the_session_worked_in(tmp_path):
    ex = _rework_exec(tmp_path)
    scratch = _seed_scratch(ex, 42)
    assert ex.rework_scratch(42, "api") == str(scratch)
    assert ex.rework_scratch(42, "other-repo") is None   # that repo isn't in the scratch
    assert ex.rework_scratch(43, "api") is None          # no session held item 43


def test_rework_scratch_is_none_when_the_feature_is_off(tmp_path):
    ex = _rework_exec(tmp_path, interactive_resume_on_rework=False)
    _seed_scratch(ex, 42)
    assert ex.rework_scratch(42, "api") is None


def test_transcript_session_id_picks_the_newest_conversation(tmp_path, monkeypatch):
    ex = _rework_exec(tmp_path)
    cwd = tmp_path / "scratch"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    project = tmp_path / "cfg" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    project.mkdir(parents=True)
    (project / "older.jsonl").write_text("{}", encoding="utf-8")
    newest = project / "11111111-2222-3333-4444-555555555555.jsonl"
    newest.write_text("{}", encoding="utf-8")
    os.utime(project / "older.jsonl", (1_000, 1_000))
    assert ex._transcript_session_id(str(cwd)) == newest.stem
    # Nothing recorded for this cwd → start fresh rather than guess.
    assert ex._transcript_session_id(str(tmp_path / "never-used")) is None


def test_reentry_reuses_the_session_scratch(tmp_path):
    """A human comment on an item the bot already worked must land in that session's
    scratch — wiping it (the old behaviour) threw the conversation away with it."""
    ex = _rework_exec(tmp_path)
    scratch = _seed_scratch(ex, 42)
    assert ex._reusable_session_scratch(str(scratch), 42, ["api"]) is True
    # A repo the original scratch skipped doesn't block reuse; a broken one does.
    assert ex._reusable_session_scratch(str(scratch), 42, ["api", "not-worktreed"]) is True
    (scratch / "web").mkdir()
    assert ex._reusable_session_scratch(str(scratch), 42, ["api", "web"]) is False


def test_reentry_does_not_reuse_a_scratch_no_session_reserved(tmp_path):
    ex = _rework_exec(tmp_path)
    scratch = Path(ex.interactive_scratch_dir(43))
    (scratch / "api" / ".git").mkdir(parents=True)
    assert ex._reusable_session_scratch(str(scratch), 43, ["api"]) is False
