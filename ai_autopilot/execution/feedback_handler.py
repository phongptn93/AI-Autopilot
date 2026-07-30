"""Handle PR review feedback by re-running Claude (ported from ``FeedbackHandler``).

Commands are role-aware (BA / QC / DEV). Each recognised ``/command`` maps to tailored
guidance that steers the agent to the right skill; unknown text falls back to the generic
``/ai`` (act) or ``/review`` (report) behaviour based on ``review_only``.
"""

from __future__ import annotations

import re
import tempfile

from ai_autopilot.config import BOT_COMMENT_INSTRUCTION, Settings, match_command
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


# ── Inferring what a bare @mention meant ─────────────────────────────────────

_INFER_PROMPT = """A teammate @mentioned the autopilot in a code-review comment without \
naming a command. Decide which ONE command they meant.

Their comment (may be Vietnamese):
\"\"\"{text}\"\"\"

Available commands:
{catalog}

Rules:
- DEFAULT TO AN ADVISORY command. Most mentions are questions, opinions, or requests to \
look at something.
- Pick an ACTION command ONLY if the comment unmistakably instructs you to CHANGE the \
code, spec or tests — e.g. "sửa lại chỗ này", "fix this", "thêm test cho case X", \
"cập nhật spec". A question, a complaint, or an observation that something looks wrong \
("chỗ này sai rồi", "sao chậm vậy?") is NOT an instruction to change anything: those are \
ADVISORY.
- If you are unsure at all, choose the advisory command that best fits.

Answer with EXACTLY the command token and nothing else, e.g. /security"""


_ADVISORY_NOTE = "ADVISORY (comment only, never changes code)"
_ACTION_NOTE = "ACTION (changes code and pushes)"


def _format_catalog(catalog: list[tuple[str, bool, str]]) -> str:
    return "\n".join(
        f"- {cmd} — {_ADVISORY_NOTE if adv else _ACTION_NOTE}"
        + (f" — {label}" if label else "")
        for cmd, adv, label in catalog
    )


def _advisory_default(config: Settings) -> str:
    """The command a mention falls back to: the first configured ADVISORY one."""
    for cmd, advisory, _ in config.command_catalog:
        if advisory:
            return cmd
    return ""


async def infer_mention_command(config: Settings, text: str) -> tuple[str, bool]:
    """Map a bare @mention comment onto ``(command, advisory)``.

    An @mention is how a human asks a teammate something, so it carries no command — but
    everything downstream (``_guidance``, ``review_only``) is keyed on one. Rather than
    special-casing mentions through that machinery, we infer the command they meant and let
    the existing paths run unchanged.

    SAFETY: ``advisory`` is DERIVED from the chosen command, never decided separately, and
    every failure mode (timeout, unparseable answer, a command that isn't configured) lands
    on the advisory default. That matters because the alternative — treating an unrecognised
    mention as an action, which is what the plain command path does — would let
    "@bot sao chỗ này chậm vậy?" rewrite the branch and push."""
    catalog = config.command_catalog
    if not catalog:
        return "", True
    default = _advisory_default(config)
    advisory_set = {cmd.lower() for cmd, adv, _ in catalog if adv}

    def _resolve(choice: str) -> tuple[str, bool]:
        low = choice.lower()
        if low not in {c.lower() for c, _, _ in catalog}:
            return default, True
        return choice, low in advisory_set

    from ai_autopilot.execution.claude_client import run_claude

    log = get_logger("execution.feedback_handler")
    try:
        run = await run_claude(
            _INFER_PROMPT.format(text=text[:800], catalog=_format_catalog(catalog)),
            tempfile.gettempdir(),  # tool-less → cwd only has to exist
            timeout_seconds=45,
            model=config.claude_model or None,
            max_turns=1,
            allowed_tools=[],
        )
    except Exception as exc:  # noqa: BLE001 — never let inference decide by failing open
        log.warning("mention intent inference failed — defaulting to advisory",
                    error=f"{type(exc).__name__}: {exc}", default=default)
        return default, True
    m = re.search(r"/\w+", run.text or "")
    if not m:
        log.warning("mention intent unparseable — defaulting to advisory",
                    reply=(run.text or "")[:120], default=default)
        return default, True
    command, advisory = _resolve(m.group(0))
    log.info("mention intent inferred", command=command, advisory=advisory)
    return command, advisory


async def resolve_command(config: Settings, cmd: dict) -> bool:
    """Whether ``cmd`` is ADVISORY (review-only), inferring the command first when it came
    from a bare @mention — in which case ``cmd["instruction"]`` is rewritten to carry the
    inferred command so ``_guidance`` / ``_AGENT_FOR`` route it like any other.

    One helper for every caller (PR babysitter, reviewer tracker) so the advisory default
    for mentions can't be honoured in one place and forgotten in another."""
    if cmd.get("via_mention"):
        command, advisory = await infer_mention_command(config, cmd["instruction"])
        if command:
            cmd["instruction"] = f"{command} {cmd['instruction']}"
        return advisory
    return match_command(cmd["instruction"], config.advisory_commands) is not None


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
