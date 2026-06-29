"""Handle PR review feedback by re-running Claude (ported from ``FeedbackHandler``)."""

from __future__ import annotations

from ai_autopilot.config import Settings
from ai_autopilot.execution.claude_executor import ClaudeExecutor
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo


class FeedbackHandler:
    def __init__(self, executor: ClaudeExecutor, config: Settings) -> None:
        self._executor = executor
        self._config = config
        self._log = get_logger("execution.feedback_handler")

    async def handle_feedback(
        self, item: WorkItemInfo, branch_name: str, feedback: str, revision: int
    ) -> ExecutionResult:
        self._log.info(
            "handling feedback", id=item.id, revision=revision, feedback=feedback[:200]
        )
        prompt = f"/bugfix-workflow {item.id} — PR feedback to address: {feedback}"
        result = await self._executor.revise(
            item, branch_name, prompt, draft_pr=self._config.pr_is_draft
        )
        if result.success:
            self._log.info("feedback addressed", id=item.id, revision=revision)
        else:
            self._log.warning(
                "failed to address feedback", id=item.id, revision=revision, error=result.error
            )
        return result
