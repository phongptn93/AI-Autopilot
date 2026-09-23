"""Tests for the Claude SDK wrapper — reasoning-effort plumbing.

Every call used to run at the model's default effort, including trivial ones (classify a
message into one of a dozen intents, reword data Python already fetched). ``effort`` lets
those run cheaper and faster, which is felt most on the chat path where someone waits.
"""

from __future__ import annotations

import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

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


def test_the_cli_stderr_replaces_the_sdk_placeholder():
    """ProcessError hardcodes stderr="Check stderr output for details" and throws the
    real output away, so the only account of why the CLI died was a sentence pointing at
    something unreachable — and this project posts that onto pull requests."""
    from claude_agent_sdk import ProcessError

    from ai_autopilot.execution.claude_client import _attach_stderr

    exc = ProcessError("Command failed with exit code 1", exit_code=1,
                       stderr="Check stderr output for details")
    _attach_stderr(exc, ["error: credit balance is too low", "run /login"])

    assert "credit balance is too low" in exc.stderr
    assert "Check stderr output" not in str(exc)
    assert "credit balance is too low" in str(exc)


def test_stderr_the_sdk_actually_kept_is_left_alone():
    from claude_agent_sdk import ProcessError

    from ai_autopilot.execution.claude_client import _attach_stderr

    exc = ProcessError("nope", exit_code=2, stderr="a real message the SDK captured")
    _attach_stderr(exc, ["something we tailed"])
    assert exc.stderr == "a real message the SDK captured"


def test_an_exception_without_stderr_is_untouched():
    """Explaining a failure must never become a second failure."""
    from ai_autopilot.execution.claude_client import _attach_stderr

    exc = ValueError("plain")
    _attach_stderr(exc, ["tail"])
    assert str(exc) == "plain"


async def test_a_run_in_flight_says_it_is_alive_and_how_long_it_has_been_quiet(monkeypatch):
    """A run was one log line, then minutes of silence, then a result — so an operator
    watching the terminal could not tell a long run from a wedged one. Seen on a /review
    of PR #3881: "running claude" at 02:41:39, then nothing but ADO poll lines."""
    said: list[dict] = []
    monkeypatch.setattr(cc, "_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(cc._log, "info", lambda event, **kw: said.append({"e": event, **kw}))

    async def _slow_query(prompt, options):
        await asyncio.sleep(0.08)
        if False:
            yield None

    monkeypatch.setattr(cc, "query", _slow_query)
    await cc.run_claude("do a thing", ".", timeout_seconds=5)

    beats = [s for s in said if s["e"] == "claude run in flight"]
    assert beats, "a run in flight must say so"
    # quiet_for is the number that answers "is it stuck?" — a run producing events is
    # working, one that has produced none for minutes is not.
    assert "quiet_for_s" in beats[0] and "elapsed_s" in beats[0]
    assert beats[0]["last"] == "(nothing yet)"


async def test_the_heartbeat_stops_when_the_run_does(monkeypatch):
    """A finished run that goes on announcing itself is worse than silence."""
    said: list[str] = []
    monkeypatch.setattr(cc, "_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(cc._log, "info", lambda event, **kw: said.append(event))

    async def _quick_query(prompt, options):
        if False:
            yield None

    monkeypatch.setattr(cc, "query", _quick_query)
    await cc.run_claude("quick", ".", timeout_seconds=5)
    said.clear()
    await asyncio.sleep(0.06)
    assert "claude run in flight" not in said


async def test_a_context_overflow_is_not_retried_and_says_what_happened(monkeypatch):
    """Seen live on a code-review loop: the agent ran `git log --all`, the API answered
    "Prompt is too long", and the CLI exited with the SAME paradoxical envelope a dropped
    connection produces. So it was retried twice — reproducing the flood each time — and
    then reported as a connection error, about a problem that is entirely about size."""
    attempts = {"n": 0}

    async def fake_query(*, prompt, options):
        attempts["n"] += 1
        # What the stream shows the operator just before it dies.
        yield AssistantMessage(content=[TextBlock(text="Prompt is too long")], model="x")
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(cc, "query", fake_query)
    with pytest.raises(RuntimeError, match="Context overflow"):
        await cc.run_claude("review the last 24h", ".", timeout_seconds=5)

    assert attempts["n"] == 1      # …and exactly once, not three times


async def test_a_genuine_connection_drop_is_still_retried(monkeypatch):
    """The paradoxical envelope really is a dropped stream most of the time — absorbing
    it is what stops one network blip re-running a 20-minute stage."""
    attempts = {"n": 0}

    async def fake_query(*, prompt, options):
        attempts["n"] += 1
        yield AssistantMessage(content=[TextBlock(text="working on it")], model="x")
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(cc, "query", fake_query)
    monkeypatch.setattr(cc, "_TRANSIENT_BACKOFF", 0.0)
    with pytest.raises(Exception, match="error result"):
        await cc.run_claude("do a thing", ".", timeout_seconds=5)

    assert attempts["n"] == cc._TRANSIENT_RETRIES + 1


async def test_a_thrashing_run_is_stopped_instead_of_burning_its_timeout(monkeypatch):
    """Observed live: "Autocompact is thrashing" at 11 minutes and still going. The run
    compacts, the next turns refill the context, it compacts again — paying for every
    lap and finishing nothing until the task timeout kills it."""
    attempts = {"n": 0}

    async def fake_query(*, prompt, options):
        attempts["n"] += 1
        yield AssistantMessage(
            content=[TextBlock(text="Autocompact is thrashing: the context refilled to "
                                    "the limit within 3 turns of the previous compact")],
            model="x",
        )
        # It would keep going for another ten minutes; the drive loop must not let it.
        yield AssistantMessage(content=[TextBlock(text="reading another file")], model="x")

    monkeypatch.setattr(cc, "query", fake_query)
    # The error leads with the CLI's own diagnosis rather than a heading of our own: it
    # used to be wrapped inside a sentence that restated it, so the reader got the same
    # explanation twice and the inner copy arrived cut off mid-word.
    with pytest.raises(RuntimeError, match="Autocompact is thrashing") as caught:
        await cc.run_claude("audit everything", ".", timeout_seconds=5)

    message = str(caught.value)
    assert "Narrow what this run is asked to read" in message   # and what to do about it
    assert message.count("Autocompact is thrashing") == 1       # said once, not nested
    assert attempts["n"] == 1      # not retried: the next lap reads the same thing


# ── the diagnosis a human reads on the report page ───────────────────────────
def test_diagnosis_is_never_cut_mid_word():
    """A flat `text[:200]` left the report page ending in "…context window. T)".

    A message that stops on a stray letter reads as a broken tool, and a reader who
    believes the tool is broken does not act on the finding it was carrying.
    """
    from ai_autopilot.execution.claude_client import _clip

    long_first_sentence = "Autocompact is thrashing because " + "x" * 600
    out = _clip(long_first_sentence)
    assert out.endswith("…")
    assert not out.rstrip("…").endswith(" ")       # no dangling space before the ellipsis


def test_diagnosis_keeps_the_facts_and_drops_the_cli_s_vaguer_advice():
    """Ours says the concrete levers; keeping both printed the same advice twice."""
    from ai_autopilot.execution.claude_client import _clip

    out = _clip(
        "Autocompact is thrashing: the context refilled to the limit within 3 turns "
        "of the previous compact, 3 times in a row. A file being read or a tool output "
        "is likely too large for the context window. Try narrowing the scope of what "
        "the agent reads, or split the task into smaller pieces."
    )
    assert "3 times in a row" in out               # the numbers are the useful part
    assert "too large for the context window" in out
    assert "Try narrowing" not in out              # the caller says this, better


def test_diagnosis_survives_text_with_no_sentence_break():
    from ai_autopilot.execution.claude_client import _clip

    assert _clip("autocompact is thrashing and it keeps going") == (
        "autocompact is thrashing and it keeps going"
    )
    assert _clip("") == ""
