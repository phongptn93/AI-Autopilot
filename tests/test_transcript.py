"""Tests for reading a live interactive session's own transcript.

This is the only window the control plane has into an interactive run — it is a
separate console, so nothing about it is observable in process. Two things are read
from it and both have a way of being wrong that is worse than being absent:

- the **pulse** a watchdog closes sessions on. Reading it wrong kills work.
- the **bill**. The CLI appends the same assistant message repeatedly while it
  streams, each line carrying that message's running total — so summing lines
  overstates the spend badly (on a real session here, 940 usage lines carried 537
  distinct ids). A cost figure that is 75% high is worse than a blank one, because
  a blank one is not believed.

No CLI is involved: every test writes the transcript itself.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from ai_autopilot.execution import transcript


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    """A working directory whose Claude Code transcripts land under ``tmp_path``."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    work = tmp_path / "scratch"
    work.mkdir()
    return str(work)


def _write(cwd: str, *entries: dict, name: str = "session.jsonl") -> None:
    directory = transcript.project_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )


def _assistant(msg_id: str, *, out: int = 10, model: str = "claude-opus-5",
               text: str = "working", ts: str = "2026-09-11T02:00:00.000Z") -> dict:
    return {
        "type": "assistant", "timestamp": ts, "uuid": f"u-{msg_id}",
        "message": {
            "id": msg_id, "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1, "output_tokens": out,
                      "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5},
        },
    }


# ── the bill ──────────────────────────────────────────────────────────────────

def test_a_message_repeated_while_it_streams_is_counted_once(cwd):
    """THE test. The CLI re-appends a message as it streams and the counts on each
    line are that message's running total, not an increment. Summing the lines is how
    a session's spend comes out multiples too high."""
    _write(cwd, _assistant("m1"), _assistant("m1"), _assistant("m1"))
    usage = transcript.read_usage(cwd)
    assert usage.messages == 1
    assert usage.output_tokens == 10          # not 30
    assert usage.total_tokens == 1 + 10 + 100 + 5


def test_distinct_messages_are_added_up(cwd):
    _write(cwd, _assistant("m1", out=10), _assistant("m2", out=7))
    usage = transcript.read_usage(cwd)
    assert usage.messages == 2 and usage.output_tokens == 17


def test_the_last_line_of_a_live_session_may_be_half_written(cwd):
    """The file is being appended to while this reads it, so the final line is
    routinely truncated. Losing the whole run's figures over that would make the
    metering useless for exactly the sessions it exists to measure — live ones."""
    directory = transcript.project_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.jsonl").write_text(
        json.dumps(_assistant("m1")) + '\n{"type":"assistant","message":{"usage":{"inp',
        encoding="utf-8",
    )
    usage = transcript.read_usage(cwd)
    assert usage.messages == 1 and usage.output_tokens == 10


def test_no_transcript_reads_as_unknown_not_as_zero(cwd):
    """Zero is a claim ("it ran and cost nothing"). Unknown is the truth, and the
    dashboard already renders unknown honestly."""
    assert transcript.read_usage(cwd) is None


def test_a_transcript_with_no_usage_is_empty_rather_than_unknown(cwd):
    """"It ran and we saw no spend" and "there was no run" are different answers."""
    _write(cwd, {"type": "user", "message": {"role": "user", "content": "hello"}})
    usage = transcript.read_usage(cwd)
    assert usage is not None and usage.total_tokens == 0


def test_cost_is_left_unset_because_this_format_carries_no_price(cwd):
    """Writing 0.0 would turn "we do not know" into a number someone budgets against."""
    _write(cwd, _assistant("m1"))
    result = type("R", (), {"cost_usd": None, "cost_tokens": 0, "model_used": "",
                            "input_tokens": 0, "output_tokens": 0,
                            "cache_read_tokens": 0, "cache_creation_tokens": 0})()
    transcript.read_usage(cwd).apply(result)
    assert result.cost_usd is None
    assert result.cost_tokens == 116 and result.model_used == "claude-opus-5"


def test_a_run_that_touched_two_models_names_the_one_that_did_the_work(cwd):
    _write(cwd, _assistant("m1", out=1000, model="claude-opus-5"),
                _assistant("m2", out=5, model="claude-haiku-4-5"))
    result = type("R", (), {"cost_usd": None, "cost_tokens": 0, "model_used": "",
                            "input_tokens": 0, "output_tokens": 0,
                            "cache_read_tokens": 0, "cache_creation_tokens": 0})()
    transcript.read_usage(cwd).apply(result)
    assert result.model_used == "claude-opus-5 +1"


# ── the pulse ─────────────────────────────────────────────────────────────────

def test_quiet_seconds_measures_how_long_since_the_last_write(cwd):
    _write(cwd, _assistant("m1"))
    path = transcript.newest(cwd)
    os.utime(path, (time.time() - 600, time.time() - 600))
    assert 590 <= transcript.quiet_seconds(cwd) <= 610


def test_quiet_is_unknown_when_there_is_no_transcript(cwd):
    """The watchdog must do nothing on unknown. Treating "no file" as "silent forever"
    would close every session on an install this cannot see into."""
    assert transcript.quiet_seconds(cwd) is None


def test_the_newest_session_wins_when_a_folder_has_several(cwd):
    _write(cwd, _assistant("old"), name="a.jsonl")
    _write(cwd, _assistant("new"), name="b.jsonl")
    old = transcript.project_dir(cwd) / "a.jsonl"
    os.utime(old, (time.time() - 9000, time.time() - 9000))
    assert transcript.session_id(cwd) == "b"


# ── what it is doing ──────────────────────────────────────────────────────────

def test_the_last_action_prefers_the_tool_call_over_the_prose(cwd):
    """"Let me check the PR" says nothing; the MCP call it then hung on says
    everything — that is the line an operator needs when a run goes quiet."""
    _write(cwd, {
        "type": "assistant", "timestamp": "2026-09-11T02:00:00.000Z",
        "message": {"id": "m1", "role": "assistant", "model": "claude-opus-5",
                    "content": [{"type": "text", "text": "Let me check the PR"},
                                {"type": "tool_use", "name": "mcp__ado__repo_pull_request",
                                 "input": {"query": "pr 3881"}}]},
    })
    line, age = transcript.last_activity(cwd)
    assert "mcp__ado__repo_pull_request" in line and age is not None


def test_the_age_survives_an_entry_this_cannot_summarise(cwd):
    """Silence is the signal the watchdog acts on, so it must not depend on being
    able to describe what the session was doing."""
    _write(cwd, {"type": "system", "timestamp": "2026-09-11T02:00:00.000Z"})
    line, age = transcript.last_activity(cwd)
    assert line == "" and age is not None


def test_the_live_feed_keeps_human_turns_and_drops_tool_results(cwd):
    """On a steered session what the person just said is the most important line on
    the page; the tool results are the bulk of the file and say nothing to a reader."""
    _write(cwd,
           {"type": "user", "timestamp": "2026-09-11T02:00:00.000Z",
            "message": {"role": "user", "content": "focus on the migration"}},
           {"type": "user", "timestamp": "2026-09-11T02:00:01.000Z",
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "content": "1200 lines of diff"}]}},
           _assistant("m1", text="on it"))
    feed = transcript.recent(cwd)
    assert "focus on the migration" in feed
    assert "1200 lines of diff" not in feed
    assert "on it" in feed


def test_the_live_feed_is_empty_rather_than_an_error_without_a_transcript(cwd):
    assert transcript.recent(cwd) == ""


def test_unreadable_lines_are_skipped_not_fatal(cwd):
    directory = transcript.project_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "session.jsonl").write_text(
        "not json at all\n" + json.dumps(_assistant("m1")) + "\n{ broken\n",
        encoding="utf-8")
    assert transcript.read_usage(cwd).messages == 1
    assert "working" in transcript.recent(cwd)
