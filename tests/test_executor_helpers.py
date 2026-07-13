"""Tests for pure helpers in the executor / notifications."""

from __future__ import annotations

from ai_autopilot.execution.claude_executor import (
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
