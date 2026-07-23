"""Handle PR review feedback by re-running Claude (ported from ``FeedbackHandler``).

Commands are role-aware (BA / QC / DEV). Each recognised ``/command`` maps to tailored
guidance that steers the agent to the right skill; unknown text falls back to the generic
``/ai`` (act) or ``/review`` (report) behaviour based on ``review_only``.
"""

from __future__ import annotations

from ai_autopilot.config import BOT_COMMENT_INSTRUCTION, Settings
from ai_autopilot.execution.claude_executor import ClaudeExecutor
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo

# Command → purpose-built subagent (in .claude/agents). Routing to these gives
# expert, consistent results; /ai and /summary stay generic (no single best agent).
_AGENT_FOR = {
    "/spec": "agent-spec-updater",
    "/test": "agent-test-writer",
    "/qc": "agent-qc-manual",
    "/security": "agent-security-reviewer",
    "/review": "agent-pr-reviewer",
    "/impact": "agent-requirement-analyst",
}


def _guidance(command: str, item: WorkItemInfo, branch: str, feedback: str, base: str) -> str:
    """Per-command instruction body. ``command`` is the leading verb (e.g. ``/spec``);
    empty when none matched. ``feedback`` is the full comment text the reviewer wrote."""
    wid = item.id
    # DEV — make the requested code change on the branch.
    if command == "/ai":
        return (
            f"A reviewer commented on the pull request (branch `{branch}`) for work item "
            f"#{wid}:\n\n{feedback}\n\nInterpret their intent and act on this branch: make "
            "the requested change, commit and push. Choose the most appropriate skill(s)."
        )
    # BA — keep the spec source of truth in sync with the code.
    if command == "/spec":
        return (
            f"A BA asked you to update the SPECIFICATION for the change in this pull request "
            f"(branch `{branch}`, work item #{wid}):\n\n{feedback}\n\nUse the `update-spec` "
            "skill. Refresh the relevant spec file(s) to match what the code now does AND "
            f"sync the linked ADO work item #{wid} (description / acceptance criteria) so the "
            "spec, the work item, and the code agree. Record the source (PR + commit) in the "
            "spec so the change is traceable. Commit and push the spec changes."
        )
    # DEV/QC — write or adjust automated tests for the change.
    if command == "/test":
        return (
            f"Write or adjust the AUTOMATED TESTS covering the change in this pull request "
            f"(branch `{branch}`, work item #{wid}):\n\n{feedback}\n\nUse `write-tests` for "
            "backend or `write-fe-tests` for frontend. Cover the acceptance criteria plus "
            "edge/negative cases, commit and push."
        )
    # QC — analyse test scope and propose cases (no code change).
    if command == "/qc":
        return (
            f"Act as QC for this pull request (branch `{branch}`, work item #{wid}):\n\n"
            f"{feedback}\n\nAnalyse the test scope against the acceptance criteria and the "
            "diff (`analyze-test-scope`). Post a PR comment listing concrete test cases "
            "(positive / negative / edge), the data needed, and any coverage gaps or risks. "
            "Do NOT modify files or push commits."
        )
    # SA — OWASP-focused security review (no code change).
    if command == "/security":
        return (
            f"Perform a SECURITY review of the diff in this pull request (branch `{branch}`, "
            f"work item #{wid}):\n\n{feedback}\n\nUse the `security-review` skill (OWASP API "
            "Top 10 for backend, OWASP Top 10 Web for frontend). Post findings grouped by "
            "severity with file:line and a concrete fix. Do NOT modify files or push commits."
        )
    # BA — impact / blast-radius analysis (no code change).
    if command == "/impact":
        return (
            f"Analyse the IMPACT of the change in this pull request (branch `{branch}`, work "
            f"item #{wid}):\n\n{feedback}\n\nIdentify which other modules, APIs, DB objects, "
            "and tenants are affected, backward-compatibility risks, and required follow-ups. "
            "Post the analysis as a PR comment. Do NOT modify files or push commits."
        )
    # Everyone — plain-language summary (no code change).
    if command == "/summary":
        return (
            f"Summarise this pull request (branch `{branch}`, work item #{wid}) for a "
            f"non-technical reviewer:\n\n{feedback}\n\nCover what changed, why, the "
            "user-facing effect, and the risk — in a few short paragraphs. Post it as a PR "
            "comment. Do NOT modify files or push commits."
        )
    # /review or anything else advisory — general code review.
    if base == "review":
        return (
            f"A reviewer asked you to REVIEW the pull request (branch `{branch}`) for work "
            f"item #{wid}:\n\n{feedback}\n\nThe PR branch is NOT checked out locally. Inspect "
            f"it read-only via `git diff origin/{{base}}...origin/{branch}` (already fetched) "
            "or the Azure DevOps PR tools, then post your findings as a PR comment. Do NOT "
            "modify files, check out branches, or push commits."
        )
    # Fallback action.
    return (
        f"A reviewer commented on the pull request (branch `{branch}`) for work item "
        f"#{wid}:\n\n{feedback}\n\nInterpret their intent and act on this branch: make the "
        "requested change, commit and push. Choose the most appropriate skill(s)."
    )


class FeedbackHandler:
    def __init__(self, executor: ClaudeExecutor, config: Settings) -> None:
        self._executor = executor
        self._config = config
        self._log = get_logger("execution.feedback_handler")

    def _command_verb(self, feedback: str) -> str:
        """The leading ``/command`` in the comment, lower-cased, or '' if none."""
        stripped = (feedback or "").lstrip()
        for cmd in self._config.comment_commands:
            if cmd.startswith("/") and stripped.lower().startswith(cmd.lower()):
                return cmd.lower()
        return ""

    async def handle_feedback(
        self, item: WorkItemInfo, branch_name: str, feedback: str, revision: int,
        repo: str = "", review_only: bool = False,
    ) -> ExecutionResult:
        command = self._command_verb(feedback)
        self._log.info(
            "handling feedback", id=item.id, revision=revision, command=command or "(none)",
            review_only=review_only, feedback=feedback[:200],
        )
        body = _guidance(
            command, item, branch_name, feedback,
            base="review" if review_only else "action",
        ).replace("{base}", self._config.base_branch)
        agent = _AGENT_FOR.get(command) if self._config.use_specialized_agents else None
        if agent:
            body += (
                f"\n\nPrefer delegating this to the `{agent}` subagent via the Task tool — "
                "it is purpose-built for exactly this. If that subagent is unavailable, do "
                "the task directly with the relevant skill."
            )
        prompt = f"{body}\n\n{BOT_COMMENT_INSTRUCTION}"
        result = await self._executor.revise(
            item, branch_name, prompt, draft_pr=self._config.pr_is_draft,
            repo=repo, allow_no_changes=review_only, read_only=review_only,
        )
        if result.success:
            self._log.info("feedback addressed", id=item.id, revision=revision, command=command)
        else:
            self._log.warning(
                "failed to address feedback", id=item.id, revision=revision,
                command=command, error=result.error,
            )
        return result
