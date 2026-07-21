"""Tests for FeedbackHandler → executor wiring (/ai vs /review)."""

from __future__ import annotations

from ai_autopilot.config import Settings
from ai_autopilot.execution.feedback_handler import FeedbackHandler
from ai_autopilot.models import ExecutionResult, WorkItemInfo


class _SpyExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def revise(self, item, branch, prompt, **kwargs):
        self.calls.append({"item": item, "branch": branch, "prompt": prompt, **kwargs})
        return ExecutionResult.ok(item.id, prompt, "done")


def _handler() -> tuple[FeedbackHandler, _SpyExecutor]:
    executor = _SpyExecutor()
    return FeedbackHandler(executor, Settings(base_branch="development")), executor


async def test_review_only_runs_read_only():
    handler, executor = _handler()
    item = WorkItemInfo(id=7, title="t")

    result = await handler.handle_feedback(
        item, "feature/be/7-x", "/review check the null handling", revision=1,
        repo="repo-a", review_only=True,
    )

    assert result.success is True
    call = executor.calls[0]
    # /review is advisory: no worktree, and "no file changes" is the happy path.
    assert call["read_only"] is True
    assert call["allow_no_changes"] is True
    # The branch is not checked out — the prompt must say how to inspect it.
    assert "NOT checked" in call["prompt"]
    assert "origin/development...origin/feature/be/7-x" in call["prompt"]
    assert "Do NOT modify" in call["prompt"]


async def test_action_feedback_runs_full_revise():
    handler, executor = _handler()
    item = WorkItemInfo(id=7, title="t")

    await handler.handle_feedback(
        item, "feature/be/7-x", "/ai rename the field", revision=1,
        repo="repo-a", review_only=False,
    )

    call = executor.calls[0]
    assert call["read_only"] is False       # /ai edits code → needs the real workspace
    assert call["allow_no_changes"] is False
    assert "commit and push" in call["prompt"]
