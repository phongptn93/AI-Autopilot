"""Test configuration.

Isolate the test suite from any developer ``config.yaml`` sitting in the repo
root: point ``AUTOPILOT_CONFIG_FILE`` at a non-existent path *before*
``ai_autopilot.config`` is imported, so ``Settings()`` falls back to model
defaults and tests stay deterministic regardless of local config.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ["AUTOPILOT_CONFIG_FILE"] = str(Path(__file__).parent / "__no_such_config__.yaml")

from collections.abc import Callable  # noqa: E402 — must follow the env var above
from dataclasses import dataclass, field  # noqa: E402

import pytest  # noqa: E402

from ai_autopilot.execution.claude_client import ClaudeRun  # noqa: E402


@dataclass
class ClaudeCall:
    """One recorded ``ClaudeExecutor._run_claude`` invocation."""

    prompt: str
    work_dir: str
    kwargs: dict = field(default_factory=dict)

    # Named accessors for the kwargs tests actually assert on. Going through
    # ``.get`` means a test reads ``call.disallowed_tools`` whether or not the caller
    # passed it, instead of a KeyError that says nothing about what went wrong.
    @property
    def repo(self) -> str | None:
        return self.kwargs.get("repo")

    @property
    def resume(self) -> str | None:
        return self.kwargs.get("resume")

    @property
    def disallowed_tools(self) -> list[str]:
        return self.kwargs.get("disallowed_tools") or []


class FakeClaude:
    """Stand-in for ``ClaudeExecutor._run_claude`` that records calls.

    It accepts ``**kwargs`` deliberately. Every hand-written fake of this method spelled
    the parameters out, and each time the real signature grew (``repo``, then ``resume``,
    then ``disallowed_tools``) the fakes raised ``unexpected keyword argument`` — three of
    them, surfacing on three separate runs, each looking like an unrelated failure.

    It also returns a real :class:`ClaudeRun` rather than a ``SimpleNamespace``. The old
    fakes set ``total_tokens`` directly, which the real class computes from the per-kind
    token counts — so a fake could satisfy a test that production code would not.
    """

    def __init__(
        self, *, text: str = "done", input_tokens: int = 0, output_tokens: int = 0,
        cost_usd: float | None = None, session_id: str | None = None,
        is_error: bool = False, side_effect: Callable | None = None,
    ) -> None:
        self.calls: list[ClaudeCall] = []
        self._run = ClaudeRun(
            text=text, is_error=is_error, session_id=session_id, cost_usd=cost_usd,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        self._side_effect = side_effect

    async def __call__(self, prompt, work_dir, **kwargs) -> ClaudeRun:
        self.calls.append(ClaudeCall(prompt, work_dir, kwargs))
        if self._side_effect is not None:
            await self._side_effect(prompt, work_dir, **kwargs)
        return self._run

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def last(self) -> ClaudeCall:
        assert self.calls, "_run_claude was never called"
        return self.calls[-1]


@pytest.fixture
def fake_claude(monkeypatch):
    """Install a :class:`FakeClaude` on an executor instance and hand it back.

        rec = fake_claude(ex, text="findings posted")
        ...
        assert "Write" in rec.last.disallowed_tools
    """
    def install(executor, **behaviour) -> FakeClaude:
        recorder = FakeClaude(**behaviour)
        monkeypatch.setattr(executor, "_run_claude", recorder)
        return recorder

    return install
