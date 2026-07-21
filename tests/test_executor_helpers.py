"""Tests for pure helpers in the executor / notifications."""

from __future__ import annotations

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
