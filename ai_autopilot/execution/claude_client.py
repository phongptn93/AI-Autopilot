"""Thin wrapper around the Claude Agent SDK.

Isolating all SDK interaction here means the rest of the codebase depends on a
small, stable surface (``ClaudeRun``) instead of the SDK's message stream — and,
crucially, we read structured token usage / cost from ``ResultMessage`` instead
of scraping stdout the way the legacy .NET CLI shell-out did.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from ai_autopilot.logging_config import get_logger

_log = get_logger("execution.claude_client")

PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"]


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
    transcript: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


async def run_claude(
    prompt: str,
    work_dir: str,
    *,
    timeout_seconds: float,
    model: str | None = None,
    max_turns: int | None = None,
    permission_mode: PermissionMode = "bypassPermissions",
) -> ClaudeRun:
    """Run Claude Code once in ``work_dir`` and return a structured result.

    Raises ``asyncio.TimeoutError`` if the run exceeds ``timeout_seconds``.
    """
    options = ClaudeAgentOptions(
        cwd=work_dir,
        permission_mode=permission_mode,
    )
    if model:
        options.model = model
    if max_turns and max_turns > 0:
        options.max_turns = max_turns

    run = ClaudeRun()

    async def _drive() -> None:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        run.transcript.append(block.text)
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
                run.cache_creation_tokens = int(usage.get("cache_creation_input_tokens", 0) or 0)
                if message.result:
                    run.text = message.result

    await asyncio.wait_for(_drive(), timeout=timeout_seconds)

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
