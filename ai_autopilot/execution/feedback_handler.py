"""Handle PR review feedback by re-running Claude (ported from ``FeedbackHandler``)."""

from __future__ import annotations

from ai_autopilot.config import BOT_COMMENT_INSTRUCTION, Settings
from ai_autopilot.execution.claude_executor import ClaudeExecutor
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo


class FeedbackHandler:
    def __init__(self, executor: ClaudeExecutor, config: Settings) -> None:
        self._executor = executor
        self._config = config
        self._log = get_logger("execution.feedback_handler")

    async def handle_feedback(
        self, item: WorkItemInfo, branch_name: str, feedback: str, revision: int,
        repo: str = "", review_only: bool = False,
    ) -> ExecutionResult:
        self._log.info(
            "handling feedback", id=item.id, revision=revision,
            review_only=review_only, feedback=feedback[:200],
        )
        if review_only:
            # /review: assess and REPORT — post findings as a comment, change nothing.
            # Runs read-only (no worktree/checkout), so the agent must inspect the
            # fetched ref or the ADO PR — the files on disk are NOT the PR branch.
            prompt = (
                f"A reviewer asked you to REVIEW the pull request (branch `{branch_name}`) "
                f"for work item #{item.id}:\n\n{feedback}\n\nThe PR branch is NOT checked "
                f"out locally. Inspect it read-only via `git diff origin/"
                f"{self._config.base_branch}...origin/{branch_name}` (already fetched) or "
                "the Azure DevOps PR tools, then post your findings as a PR comment. "
                "Do NOT modify files, check out branches, or push commits."
            )
        else:
            # /ai (action): interpret and make the change on the branch.
            prompt = (
                f"A reviewer commented on the pull request (branch `{branch_name}`) for work "
                f"item #{item.id}:\n\n{feedback}\n\nInterpret their intent and act on this "
                "branch: make the requested change, commit and push. Choose the most "
                "appropriate skill(s)."
            )
        prompt = f"{prompt}\n\n{BOT_COMMENT_INSTRUCTION}"
        result = await self._executor.revise(
            item, branch_name, prompt, draft_pr=self._config.pr_is_draft,
            repo=repo, allow_no_changes=review_only, read_only=review_only,
        )
        if result.success:
            self._log.info("feedback addressed", id=item.id, revision=revision)
        else:
            self._log.warning(
                "failed to address feedback", id=item.id, revision=revision, error=result.error
            )
        return result
