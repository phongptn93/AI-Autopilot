"""Tests for the Claude SDK wrapper — reasoning-effort plumbing.

Every call used to run at the model's default effort, including trivial ones (classify a
message into one of a dozen intents, reword data Python already fetched). ``effort`` lets
those run cheaper and faster, which is felt most on the chat path where someone waits.
"""

from __future__ import annotations

import ai_autopilot.execution.claude_client as cc
from ai_autopilot.config import Settings


def _capture_query(monkeypatch) -> dict:
    """Patch the SDK's ``query`` so we can inspect the options built for it."""
    seen: dict = {}

    async def fake_query(*, prompt, options):
        seen["prompt"] = prompt
        seen["options"] = options
        if False:  # pragma: no cover - makes this an async generator
            yield None

    monkeypatch.setattr(cc, "query", fake_query)
    return seen


async def _run(monkeypatch, **kwargs):
    seen = _capture_query(monkeypatch)
    await cc.run_claude("hi", ".", timeout_seconds=5, **kwargs)
    return seen["options"]


async def test_effort_is_passed_through(monkeypatch):
    for level in ("low", "medium", "high", "xhigh", "max"):
        options = await _run(monkeypatch, effort=level)
        assert options.effort == level


async def test_no_effort_leaves_the_model_default(monkeypatch):
    """Omitting it must not pin a level — that was the behaviour before this existed."""
    assert (await _run(monkeypatch)).effort is None
    assert (await _run(monkeypatch, effort=None)).effort is None
    assert (await _run(monkeypatch, effort="")).effort is None


async def test_unknown_effort_is_ignored_not_raised(monkeypatch):
    """A typo in config must not take the autopilot down mid-run."""
    options = await _run(monkeypatch, effort="verylow")
    assert options.effort is None


async def test_effort_defaults_split_by_workload():
    """Cheap chat calls run low; the agentic turn medium; real code work is left at the
    model's default until it has been swept on real evals."""
    s = Settings()
    assert s.claude_effort_chat == "low"
    assert s.claude_effort_agentic == "medium"
    assert s.claude_effort_task == ""     # blank → unchanged


async def test_disallowed_tools_reaches_the_sdk(monkeypatch):
    """The deny list IS the advisory path's safety. If it silently failed to reach
    ClaudeAgentOptions, "read-only" would quietly be prompt-only again."""
    options = await _run(monkeypatch, disallowed_tools=["Write", "Edit"])
    assert options.disallowed_tools == ["Write", "Edit"]


async def test_no_deny_list_leaves_the_sdk_default(monkeypatch):
    options = await _run(monkeypatch)
    assert not options.disallowed_tools


async def test_deny_list_is_independent_of_the_allow_list(monkeypatch):
    """An advisory run still needs the workspace's skills, MCP servers and subagents, so it
    passes NO allow list — the deny list must not depend on one being set."""
    options = await _run(monkeypatch, disallowed_tools=["Write"])
    assert options.disallowed_tools == ["Write"]
    assert not options.allowed_tools
