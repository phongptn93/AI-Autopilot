"""Handle PR review feedback by re-running Claude (ported from ``FeedbackHandler``).

Commands are role-aware (BA / QC / DEV). Each recognised ``/command`` maps to tailored
guidance that steers the agent to the right skill; unknown text falls back to the generic
``/ai`` (act) or ``/review`` (report) behaviour based on ``review_only``.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass

from ai_autopilot.config import (
    AGENT_CONDUCT_INSTRUCTION,
    BOT_COMMENT_INSTRUCTION,
    EVIDENCE_BOUNDARY_INSTRUCTION,
    Settings,
    match_command,
)
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
            "modify files, check out branches, or push commits.\n"
            # One `git diff` of a large PR is bigger than the context, and the run then
            # dies with "Prompt is too long" — the reviewer gets an apology instead of a
            # review. Read the shape first, then the files that matter.
            "Read in stages: `--stat` first, then one file at a time; never dump the "
            "whole diff, and skip generated or vendored files by name. If the reviewer "
            "named a path, review THAT and say so."
        )
    # Fallback action.
    return (
        f"A reviewer commented on the pull request (branch `{branch}`) for work item "
        f"#{wid}:\n\n{feedback}\n\nInterpret their intent and act on this branch: make the "
        "requested change, commit and push. Choose the most appropriate skill(s)."
    )


# ── Inferring what a command-less message to the bot meant ──────────────────

_INFER_PROMPT = """A teammate is talking to the autopilot in a code-review / work-item \
thread without naming a command. Read the WHOLE conversation like a colleague would, \
then decide what they want now.

Conversation so far, oldest first ("Autopilot:" lines are YOUR earlier replies; may be \
Vietnamese):
\"\"\"{context}\"\"\"

Their newest message — the one to act on:
\"\"\"{text}\"\"\"

Available commands:
{catalog}
- /none — nothing to do: thanks, "ok" with nothing pending, or a remark meant for a \
colleague{none_note}

How to decide:
1. ACCEPTING YOUR OFFER. If your last reply offered to change something ("Muốn tôi sửa \
luôn?") and they accept ("ok", "sửa đi", "làm đi", "đồng ý", "yes", "chỉ sửa mục 2"), \
pick the ACTION command that does it. They already said yes — never ask again.
2. CORRECTING YOU. If they say your last reply or change was wrong ("bạn sai rồi", "không \
phải vậy", "vẫn lỗi", "chưa đúng", "wrong"):
   - your last turn CHANGED code/spec/tests ("Đã xử lý xong", "branch đã được cập nhật") \
→ the ACTION command that redoes it: they asked for that change and are telling you it \
is not right yet;
   - they also say what the right result is AND ask for it or accept your earlier offer \
→ the ACTION command;
   - otherwise (your last turn was a review / an answer) → the ADVISORY command that \
re-examines it.
   Mark a correction by adding the word "correction" after the command.
3. A NEW REQUEST. An ACTION command ONLY if it unmistakably instructs you to CHANGE \
the code, spec or tests ("sửa lại chỗ này", "thêm test cho case X", "cập nhật spec"). A \
question, or an observation about code nobody asked you to change ("sao chậm vậy?"), is \
ADVISORY.
4. Unsure → the advisory command that fits best.

Answer with EXACTLY one line: the command token, optionally followed by the word \
correction. Examples: /security   |   /ai correction   |   /review correction   |   /none"""

# Prepended to the instruction of a run that answers "you got it wrong", so the agent
# re-checks its OWN previous turn instead of producing a fresh, generic one.
_CORRECTION_BRIEF = (
    "[CORRECTION] The person says your previous reply/change in this thread is WRONG. "
    "The conversation below includes your earlier turns (\"Autopilot:\"). Re-check that "
    "turn against the actual code — do not repeat it. If they are right: say so in one "
    "line, then deliver the corrected result directly (the fix, or the corrected "
    "answer) — do not ask them to confirm or to send another command. If you still "
    "believe you were right: explain why with concrete evidence (file:line, what the "
    "code does), and say what would change your mind."
)

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


@dataclass(frozen=True)
class MentionIntent:
    """What a command-less message to the bot asks for.

    ``skip`` — nothing to do (thanks, a note to a colleague); the caller drops it.
    ``correction`` — it says the bot's previous turn was wrong; the run re-checks that
    turn instead of starting over."""

    command: str
    advisory: bool
    correction: bool = False
    skip: bool = False


async def infer_intent(
    config: Settings, text: str, *, context: str = "", followup: bool = False,
) -> MentionIntent:
    """Map a command-less message to the bot onto a :class:`MentionIntent`.

    An @mention — or a plain reply under the bot's answer — is how a human talks to a
    teammate, so it carries no command, but everything downstream (``_guidance``,
    ``review_only``) is keyed on one. The command is inferred from the WHOLE
    conversation, the bot's own turns included: "bạn sai rồi" right after the bot pushed
    a change asks to redo that change, and "ok sửa đi" right after it offered a fix is
    the yes to that offer. Read alone, both looked like remarks and were answered with a
    generic review plus "reply /ai" — the person had to say it twice.

    SAFETY: ``advisory`` is DERIVED from the chosen command, never decided separately,
    and every failure mode (timeout, unparseable answer, a command that isn't configured)
    lands on the advisory default — or, for a ``followup`` that did not address the bot
    explicitly, on doing nothing. An action needs the model to name one."""
    catalog = config.command_catalog
    if not catalog:
        return MentionIntent("", True, skip=followup)
    default = _advisory_default(config)
    fallback = MentionIntent(default, True, skip=followup)
    advisory_set = {cmd.lower() for cmd, adv, _ in catalog if adv}
    known = {c.lower(): c for c, _, _ in catalog}

    from ai_autopilot.execution.claude_client import run_claude

    log = get_logger("execution.feedback_handler")
    prompt = _INFER_PROMPT.format(
        context=(context or "(no earlier messages)")[-3000:],
        text=(text or "")[:1500],
        catalog=_format_catalog(catalog),
        none_note=(
            " — they did not @mention you, so pick /none unless it is clearly for you"
            if followup else ""
        ),
    )
    try:
        run = await run_claude(
            prompt,
            tempfile.gettempdir(),  # tool-less → cwd only has to exist
            timeout_seconds=45,
            model=config.claude_model or None,
            max_turns=1,
            allowed_tools=[],
            effort=config.claude_effort_chat,   # picks one command from a menu
        )
    except Exception as exc:  # noqa: BLE001 — never let inference decide by failing open
        log.warning("mention intent inference failed — falling back",
                    error=f"{type(exc).__name__}: {exc}", default=default, followup=followup)
        return fallback
    reply = run.text or ""
    m = re.search(r"/\w+", reply)
    if not m:
        log.warning("mention intent unparseable — falling back",
                    reply=reply[:120], default=default, followup=followup)
        return fallback
    choice = m.group(0).lower()
    if choice == "/none":
        log.info("mention intent inferred", command="/none", followup=followup)
        return MentionIntent("", True, skip=True)
    if choice not in known:
        return MentionIntent(default, True)
    correction = "correction" in reply[m.end():].lower()
    intent = MentionIntent(known[choice], choice in advisory_set, correction=correction)
    log.info("mention intent inferred", command=intent.command,
             advisory=intent.advisory, correction=correction, followup=followup)
    return intent


async def infer_mention_command(config: Settings, text: str) -> tuple[str, bool]:
    """``(command, advisory)`` for a bare @mention with no thread around it."""
    intent = await infer_intent(config, text)
    return intent.command, intent.advisory


async def resolve_command(config: Settings, cmd: dict) -> bool:
    """Whether ``cmd`` is ADVISORY (review-only), inferring the command first when it came
    from a bare @mention or a follow-up reply — in which case ``cmd["instruction"]`` is
    rewritten to carry the inferred command so ``_guidance`` / ``_AGENT_FOR`` route it
    like any other.

    A mention is read WITH the thread it sits in, the bot's own replies included. The
    mention itself is usually the shortest comment there — "@bot mày có nghe không",
    "bạn sai rồi" — while what it refers to is above it. The conversation is passed on
    in the instruction too, so the run works on the real request; a correction is
    flagged so the run re-checks its previous turn rather than starting over.

    Sets ``cmd["skip"]`` when there is nothing to do; callers must then drop it.

    One helper for every caller (PR babysitter, reviewer tracker, work-item poller) so
    the advisory default for mentions can't be honoured in one place and forgotten in
    another.
    """
    if cmd.get("via_mention"):
        asked = str(cmd.get("instruction") or "").strip()
        context = str(cmd.get("thread_context") or "").strip()
        intent = await infer_intent(
            config, asked, context=context, followup=bool(cmd.get("followup")),
        )
        if intent.skip:
            cmd["skip"] = True
            return True
        body = asked
        if context:
            body = f"Conversation so far:\n{context}\n\nLatest message:\n{asked}"
        if intent.correction:
            body = f"{_CORRECTION_BRIEF}\n\n{body}"
        cmd["instruction"] = " ".join(x for x in (intent.command, body) if x)
        cmd["correction"] = intent.correction
        return intent.advisory
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

    def build_prompt(
        self, item: WorkItemInfo, branch_name: str, feedback: str, *, review_only: bool = False,
    ) -> str:
        """The instruction a PR command runs under — shared by the headless revise and
        the interactive session, so steering a session starts from the same brief."""
        command = self._command_verb(feedback)
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
        return (
            f"{body}\n\n{BOT_COMMENT_INSTRUCTION}\n\n{AGENT_CONDUCT_INSTRUCTION}"
            f"\n\n{EVIDENCE_BOUNDARY_INSTRUCTION}"
        )

    async def handle_feedback(
        self, item: WorkItemInfo, branch_name: str, feedback: str, revision: int,
        repo: str = "", review_only: bool = False, pr_id: int = 0,
    ) -> ExecutionResult:
        command = self._command_verb(feedback)
        self._log.info(
            "handling feedback", id=item.id, revision=revision, command=command or "(none)",
            review_only=review_only, feedback=feedback[:200],
        )
        prompt = self.build_prompt(item, branch_name, feedback, review_only=review_only)
        result = await self._executor.revise(
            item, branch_name, prompt, draft_pr=self._config.pr_is_draft,
            repo=repo, allow_no_changes=review_only, read_only=review_only,
            pr_id=pr_id,
        )
        if result.success:
            self._log.info("feedback addressed", id=item.id, revision=revision, command=command)
        else:
            self._log.warning(
                "failed to address feedback", id=item.id, revision=revision,
                command=command, error=result.error,
            )
        return result
