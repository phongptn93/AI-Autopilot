"""Thin wrapper around the Claude Agent SDK.

Isolating all SDK interaction here means the rest of the codebase depends on a
small, stable surface (``ClaudeRun``) instead of the SDK's message stream — and,
crucially, we read structured token usage / cost from ``ResultMessage`` instead
of scraping stdout the way the legacy .NET CLI shell-out did.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    query,
)

from ai_autopilot import activity
from ai_autopilot.logging_config import describe_exc, get_logger

# How often a run in flight says it is alive. Long enough not to crowd a log that is
# already busy, short enough that somebody waiting gets an answer.
_HEARTBEAT_SECONDS = 60

# How much of the CLI's stderr to keep. Enough to carry a stack tail or an auth refusal,
# short enough that it can be shown to a person without burying the point.
_STDERR_TAIL_LINES = 40

# The SDK's placeholder for output it did not keep. Recognised by text because it is the
# only handle there is: ProcessError sets it unconditionally, so a message carrying it
# is one the SDK could not explain, not one that had nothing to say.
_SDK_STDERR_PLACEHOLDER = "Check stderr output for details"


def _attach_stderr(exc: BaseException, tail) -> None:
    """Put the CLI's real stderr on the exception, replacing the SDK's placeholder.

    ``ProcessError`` builds its message once in ``__init__`` and hardcodes
    ``stderr="Check stderr output for details"`` — the output itself is discarded. The
    result is a failure report that names no cause and directs the reader to something
    unreachable, and this project posts ``result.error`` straight onto a pull request,
    where colleagues and customers read it.

    Best-effort in every direction: an exception with no ``stderr`` attribute, or one we
    cannot write to, is left exactly as it was. Explaining a failure must never become a
    second failure.
    """
    if not tail or not hasattr(exc, "stderr"):
        return
    captured = "\n".join(tail)
    try:
        current = getattr(exc, "stderr", None) or ""
        if current and _SDK_STDERR_PLACEHOLDER not in current:
            return                              # the SDK kept something real — leave it
        exc.stderr = captured
        message = str(exc)
        if _SDK_STDERR_PLACEHOLDER in message:
            exc.args = (message.replace(_SDK_STDERR_PLACEHOLDER, captured),)
    except Exception:  # noqa: BLE001 — never fail while explaining a failure
        pass


_log = get_logger("execution.claude_client")

PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"]
# Mirrors the SDK's EffortLevel. Kept as a plain set so a bad config value can be
# rejected with a warning instead of a crash (see run_claude).
_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

# Transient stream/connection failures a run can survive by simply re-running —
# NOT real task failures. Windows periodically resets the Anthropic streaming
# connection (``ConnectionResetError [WinError 10054]``); when that lands
# mid-turn the CLI exits non-zero with a *paradoxical* result envelope
# (``is_error=True`` but ``subtype="success"`` and no ``errors``), which the SDK
# surfaces as a bare ``Exception("Claude Code returned an error result: success")``.
# Absorbing these here means one network blip no longer burns a whole
# poller-level retry — which re-runs the entire (often 10-30 min) stage from
# scratch and throws the finished work away.
_TRANSIENT_MARKERS = (
    "error result: success",
    "error result: error",
    "10054",
    "forcibly closed",
    "connection reset",
    "connection lost",
    "connection aborted",
    "peer closed",
    "broken pipe",
    "server disconnected",
    "remote host",
)
_TRANSIENT_RETRIES = 2  # extra FRESH attempts after the first
_TRANSIENT_BACKOFF = 3.0  # seconds, doubled per retry (3s, 6s)


def _is_transient(exc: BaseException) -> bool:
    """True for a network/stream drop that a re-run will likely survive."""
    if isinstance(exc, (ConnectionResetError, ConnectionError, BrokenPipeError)):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


@dataclass
class ClaudeRun:
    """Structured result of a single Claude Agent SDK invocation."""

    text: str = ""
    is_error: bool = False
    num_turns: int = 0
    duration_ms: int = 0
    session_id: str | None = None
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    # Which model actually served the run, as the CLI reports it. Read from the result
    # rather than from what we ASKED for: a blank ``claude_model`` means "SDK default",
    # and an alias like "sonnet" resolves to a dated id — recording the request would
    # make the history unable to answer "what did this cost us, on what".
    # A run can legitimately touch more than one (a sub-agent on a cheaper tier), so the
    # breakdown is kept and ``model`` is the one that did the most work.
    models: dict[str, int] = field(default_factory=dict)   # model -> tokens
    transcript: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    @property
    def model(self) -> str:
        """The model that did most of the work, or "" when the CLI reported none."""
        return max(self.models, key=lambda k: self.models[k]) if self.models else ""

    @property
    def model_label(self) -> str:
        """What to show a human: the main model, and how many others chipped in."""
        if not self.models:
            return ""
        main = self.model
        extra = len(self.models) - 1
        return f"{main} +{extra}" if extra > 0 else main


@dataclass
class Usage:
    """Token/cost totals across every Claude call that made up ONE work item's result.

    A work item is rarely one call — a task run, a retry, a ``/pr-create``, or a whole
    SDLC profile of stages. History reports per ITEM, so the numbers have to be summed
    somewhere, and doing it here means every caller sums them the same way instead of
    each one remembering which four fields exist.

    ``models`` is a token count per model rather than a set of names, so "which model
    did this work item actually run on" has a defensible answer when more than one was
    involved: the one that burned the most.
    """

    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None
    models: dict[str, int] = field(default_factory=dict)

    def add(self, *runs: ClaudeRun | None) -> Usage:
        for run in runs:
            if run is None:
                continue
            self.tokens += run.total_tokens
            self.input_tokens += run.input_tokens
            self.output_tokens += run.output_tokens
            self.cache_read_tokens += run.cache_read_tokens
            self.cache_creation_tokens += run.cache_creation_tokens
            if run.cost_usd is not None:
                # None means "the CLI did not report a cost", which is NOT zero. Only
                # start summing once there is at least one real figure, so a run whose
                # cost is unknown stays unknown instead of being reported as free.
                self.cost_usd = (self.cost_usd or 0.0) + run.cost_usd
            for name, count in run.models.items():
                self.models[name] = self.models.get(name, 0) + count
        return self

    @property
    def model(self) -> str:
        return max(self.models, key=lambda k: self.models[k]) if self.models else ""

    @property
    def model_label(self) -> str:
        if not self.models:
            return ""
        extra = len(self.models) - 1
        return f"{self.model} +{extra}" if extra > 0 else self.model

    def apply(self, result) -> None:
        """Write the totals onto an ``ExecutionResult``. The one place that mapping lives."""
        result.cost_tokens = self.tokens
        result.cost_usd = self.cost_usd
        result.model_used = self.model_label
        result.input_tokens = self.input_tokens
        result.output_tokens = self.output_tokens
        result.cache_read_tokens = self.cache_read_tokens
        result.cache_creation_tokens = self.cache_creation_tokens


def apply_usage(result, *runs: ClaudeRun | None) -> None:
    """Shorthand for the common case: one or more runs, straight onto the result."""
    Usage().add(*runs).apply(result)


async def run_claude(
    prompt: str,
    work_dir: str,
    *,
    timeout_seconds: float,
    model: str | None = None,
    max_turns: int | None = None,
    permission_mode: PermissionMode = "acceptEdits",
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    setting_sources: list[str] | None = None,
    mcp_servers: dict | None = None,
    add_dirs: list[str] | None = None,
    resume: str | None = None,
    effort: str | None = None,
    on_event: Callable[[str], None] | None = None,
) -> ClaudeRun:
    """Run Claude Code once in ``work_dir`` and return a structured result.

    Raises ``asyncio.TimeoutError`` if the run exceeds ``timeout_seconds``.

    ``resume`` continues a prior Agent session (its id from an earlier ``ClaudeRun``)
    so the agent keeps that conversation's context. Best-effort: if resuming fails
    (session gone / not on this host) the run retries once from a FRESH session, so a
    stale id never blocks work. A timeout is never retried (it propagates).

    Note: ``permission_mode="bypassPermissions"`` maps to
    ``--dangerously-skip-permissions``, which the Claude CLI refuses to run under
    root. Use ``"acceptEdits"`` (the default) for containerised/root deployments.

    ``setting_sources`` opts into loading filesystem configuration (project
    ``.claude/`` skills, rules, settings, CLAUDE.md) — the SDK loads *none* by
    default, so without it ``/skill`` commands are inert. ``mcp_servers`` and
    ``add_dirs`` let the run reach the workspace's MCP servers and extra repo
    directories.

    ``effort`` ("low"|"medium"|"high"|"xhigh"|"max") caps how much the model reasons.
    ``None`` leaves the model's own default, which is where every call sat before this
    parameter existed — including the trivial ones (classify a message into one of a
    dozen intents, reword data Python already fetched). Those get strong results at a
    fraction of the tokens and latency on a lower setting, which matters most on the
    chat path where a person is waiting. An unrecognised value is ignored rather than
    raising: a typo in config must not take the autopilot down.
    """
    effort_level = effort if effort in _EFFORT_LEVELS else None
    if effort and effort_level is None:
        _log.warning("ignoring unknown effort level", effort=effort,
                     allowed=sorted(_EFFORT_LEVELS))
    # The CLI's own stderr, kept because the SDK throws it away. On a non-zero exit it
    # raises ProcessError(stderr="Check stderr output for details") — a placeholder, not
    # the output — so the only account of WHY the CLI died was a sentence telling the
    # reader to go and read something that no longer exists anywhere. That text has been
    # posted verbatim onto pull requests.
    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)

    def _capture_stderr(line: str) -> None:
        text = (line or "").strip()
        if text:
            stderr_tail.append(text)

    def _build_options(resume_id: str | None) -> ClaudeAgentOptions:
        options = ClaudeAgentOptions(
            cwd=work_dir, permission_mode=permission_mode, stderr=_capture_stderr,
        )
        if allowed_tools is not None:
            # `[]` must mean "no tools" (e.g. pure-text classification callers) —
            # `if allowed_tools:` treated an empty list as falsy and silently left the
            # SDK default (all tools) in effect, which could burn the caller's single
            # max_turns on a tool call instead of returning the expected text/JSON.
            options.allowed_tools = allowed_tools
        if disallowed_tools:
            # A DENY list, not a narrower allow list: an advisory run still needs the
            # workspace's skills, MCP servers and subagents, and enumerating all of those
            # would break the moment one is added. Naming the mutators is precise about
            # what is being prevented and survives new tools appearing.
            options.disallowed_tools = disallowed_tools
        if model:
            options.model = model
        if max_turns and max_turns > 0:
            options.max_turns = max_turns
        if setting_sources is not None:
            options.setting_sources = setting_sources
        if mcp_servers:
            options.mcp_servers = mcp_servers
        if add_dirs:
            options.add_dirs = add_dirs
        if resume_id:
            options.resume = resume_id
        if effort_level:
            options.effort = effort_level
        return options

    # What the heartbeat reports on. A run is a single log line, then minutes of
    # silence, then a result — so an operator watching the terminal cannot tell a long
    # run from a wedged one, and the only honest answer to "is it still going?" was to
    # wait and see. `quiet_for` is the number that actually answers it: a run producing
    # events is working, a run that has produced none for minutes is not.
    pulse = {"last": "", "at": time.monotonic(), "events": 0}

    def _emit(line: str) -> None:
        text = line.strip()
        if not text:
            return
        pulse["last"] = text[:140]
        pulse["at"] = time.monotonic()
        pulse["events"] += 1
        if on_event:
            # Activity must never break the run: the feed is a convenience, the run is not.
            with contextlib.suppress(Exception):
                on_event(text)

    async def _heartbeat() -> None:
        """Say the run is alive, and how long since it last did anything."""
        started_at = time.monotonic()
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            _log.info(
                "claude run in flight",
                elapsed_s=int(time.monotonic() - started_at),
                quiet_for_s=int(time.monotonic() - pulse["at"]),
                events=pulse["events"],
                last=pulse["last"] or "(nothing yet)",
            )

    async def _attempt(resume_id: str | None) -> ClaudeRun:
        run = ClaudeRun()
        options = _build_options(resume_id)

        async def _drive() -> None:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            run.transcript.append(block.text)
                            _emit("💬 " + block.text[:400])
                        elif isinstance(block, ThinkingBlock):
                            _emit("🤔 " + (getattr(block, "thinking", "") or "")[:200])
                        elif isinstance(block, ToolUseBlock):
                            _emit("🔧 " + activity.tool_summary(block.name, block.input))
                elif isinstance(message, ResultMessage):
                    run.is_error = bool(message.is_error)
                    run.num_turns = message.num_turns
                    run.duration_ms = message.duration_ms
                    run.session_id = message.session_id
                    run.cost_usd = message.total_cost_usd
                    usage = message.usage or {}
                    run.input_tokens = int(usage.get("input_tokens", 0) or 0)
                    run.output_tokens = int(usage.get("output_tokens", 0) or 0)
                    run.cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
                    run.cache_creation_tokens = int(
                        usage.get("cache_creation_input_tokens", 0) or 0
                    )
                    # ``model_usage`` is keyed by model id with a camelCase payload
                    # passed through verbatim from the CLI. Read defensively: it is
                    # absent on older CLIs, and losing the token TOTAL because the
                    # breakdown changed shape would be a bad trade.
                    for name, mu in (getattr(message, "model_usage", None) or {}).items():
                        try:
                            run.models[str(name)] = (
                                int(mu.get("inputTokens", 0) or 0)
                                + int(mu.get("outputTokens", 0) or 0)
                                + int(mu.get("cacheReadInputTokens", 0) or 0)
                                + int(mu.get("cacheCreationInputTokens", 0) or 0)
                            )
                        except (AttributeError, TypeError, ValueError):
                            run.models[str(name)] = 0
                    if message.result:
                        run.text = message.result

        await asyncio.wait_for(_drive(), timeout=timeout_seconds)
        return run

    # One run, but resilient to two recoverable failure modes:
    #   • a stale ``resume`` id  → drop it, retry FRESH once (as before);
    #   • a transient network drop (WinError 10054 / "error result: success")
    #                             → back off and re-run the whole thing.
    # A timeout is never retried here — it propagates as a real failure.
    resume_id = resume
    run: ClaudeRun | None = None
    # The heartbeat covers the retries too: a run that is quietly backing off and
    # re-running is exactly as invisible as one that is working.
    beat = asyncio.create_task(_heartbeat())
    try:
        for attempt_no in range(_TRANSIENT_RETRIES + 1):
            try:
                run = await _attempt(resume_id)
                break
            except TimeoutError:
                raise  # a timeout is a real failure — never silently re-run
            except Exception as exc:  # noqa: BLE001
                if resume_id:
                    _log.warning(
                        "resume failed — retrying from a fresh session", error=describe_exc(exc)
                    )
                    resume_id = None
                    continue
                _attach_stderr(exc, stderr_tail)
                if attempt_no < _TRANSIENT_RETRIES and _is_transient(exc):
                    delay = _TRANSIENT_BACKOFF * (2**attempt_no)
                    _log.warning(
                        "transient claude error — retrying run",
                        attempt=attempt_no + 1,
                        of=_TRANSIENT_RETRIES,
                        delay=delay,
                        error=describe_exc(exc),
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
    finally:
        # Every way out — success, timeout, or giving up after the retries — has to
        # stop the heartbeat, or the task outlives the run it was reporting on and a
        # finished run goes on announcing itself.
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
    assert run is not None  # loop only exits via break (success) or raise

    if not run.text and run.transcript:
        run.text = "\n".join(run.transcript)
    _log.debug(
        "claude run finished",
        turns=run.num_turns,
        tokens=run.total_tokens,
        cost=run.cost_usd,
        is_error=run.is_error,
    )
    return run
