"""Tests for FeedbackHandler → executor wiring (/ai vs /review)."""

from __future__ import annotations

import ai_autopilot.execution.claude_client as claude_client
from ai_autopilot.config import Settings
from ai_autopilot.execution.feedback_handler import (
    FeedbackHandler,
    _advisory_default,
    infer_mention_command,
    resolve_command,
)
from ai_autopilot.models import ExecutionResult, WorkItemInfo


class _SpyExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def revise(self, item, branch, prompt, **kwargs):
        self.calls.append({"item": item, "branch": branch, "prompt": prompt, **kwargs})
        return ExecutionResult.ok(item.id, prompt, "done")


def _handler() -> tuple[FeedbackHandler, _SpyExecutor]:
    executor = _SpyExecutor()
    return FeedbackHandler(executor, Settings(base_branch="development")), executor


async def test_review_only_runs_read_only():
    handler, executor = _handler()
    item = WorkItemInfo(id=7, title="t")

    result = await handler.handle_feedback(
        item, "feature/be/7-x", "/review check the null handling", revision=1,
        repo="repo-a", review_only=True,
    )

    assert result.success is True
    call = executor.calls[0]
    # /review is advisory: no worktree, and "no file changes" is the happy path.
    assert call["read_only"] is True
    assert call["allow_no_changes"] is True
    # The branch is not checked out — the prompt must say how to inspect it.
    assert "NOT checked" in call["prompt"]
    assert "origin/development...origin/feature/be/7-x" in call["prompt"]
    assert "Do NOT modify" in call["prompt"]


async def test_action_feedback_runs_full_revise():
    handler, executor = _handler()
    item = WorkItemInfo(id=7, title="t")

    await handler.handle_feedback(
        item, "feature/be/7-x", "/ai rename the field", revision=1,
        repo="repo-a", review_only=False,
    )

    call = executor.calls[0]
    assert call["read_only"] is False       # /ai edits code → needs the real workspace
    assert call["allow_no_changes"] is False
    assert "commit and push" in call["prompt"]


# ── Inferring what a bare @mention meant ─────────────────────────────────────
#
# SAFETY-CRITICAL. A mention carries no command, but everything downstream is keyed on
# one. The plain command path treats "no recognised advisory command" as an ACTION — so
# routing mentions through it unchanged would let "@bot sao chỗ này chậm vậy?" (a
# question) rewrite the branch and push. Every uncertainty must land on advisory.

def _reply(text: str):
    async def fake_run_claude(prompt, work_dir, **kwargs):
        assert kwargs.get("allowed_tools") == []   # inference must never use tools

        class _R:
            pass

        _R.text = text
        return _R()

    return fake_run_claude


async def test_question_infers_advisory_command(monkeypatch):
    monkeypatch.setattr(claude_client, "run_claude", _reply("/impact"))
    command, advisory = await infer_mention_command(Settings(), "sao chỗ này chậm vậy?")
    assert (command, advisory) == ("/impact", True)


async def test_explicit_change_request_may_infer_action(monkeypatch):
    monkeypatch.setattr(claude_client, "run_claude", _reply("/ai"))
    command, advisory = await infer_mention_command(Settings(), "sửa giúp null check dòng 42")
    assert (command, advisory) == ("/ai", False)


async def test_unknown_command_falls_back_to_advisory(monkeypatch):
    """A command that isn't configured must not be trusted into an action."""
    monkeypatch.setattr(claude_client, "run_claude", _reply("/deploy-to-prod"))
    assert await infer_mention_command(Settings(), "x") == ("/review", True)


async def test_inference_failure_falls_back_to_advisory(monkeypatch):
    async def boom(prompt, work_dir, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(claude_client, "run_claude", boom)
    assert await infer_mention_command(Settings(), "x") == ("/review", True)


async def test_unparseable_reply_falls_back_to_advisory(monkeypatch):
    monkeypatch.setattr(claude_client, "run_claude", _reply("I think you should fix it"))
    assert await infer_mention_command(Settings(), "x") == ("/review", True)


async def test_action_only_config_still_never_pushes_by_default(monkeypatch):
    """With NO advisory command configured there is no safe command to fall back to —
    the mention must still be treated as advisory rather than defaulting to /ai."""
    cfg = Settings(comment_command="/ai", comment_advisory_commands="")
    assert _advisory_default(cfg) == ""
    monkeypatch.setattr(claude_client, "run_claude", _reply("nonsense"))
    assert await infer_mention_command(cfg, "chỗ này sai rồi") == ("", True)


async def test_resolve_command_prefixes_inferred_command(monkeypatch):
    """The inferred command is written INTO the instruction so _guidance and the
    advisory recompute downstream see a real command."""
    monkeypatch.setattr(claude_client, "run_claude", _reply("/security"))
    cmd = {"instruction": "chỗ này có SQL injection không?", "via_mention": True}
    assert await resolve_command(Settings(), cmd) is True
    assert cmd["instruction"] == "/security chỗ này có SQL injection không?"


async def test_resolve_command_leaves_slash_commands_alone(monkeypatch):
    async def must_not_run(*args, **kwargs):
        raise AssertionError("an explicit command must not be re-inferred")

    monkeypatch.setattr(claude_client, "run_claude", must_not_run)
    cmd = {"instruction": "/ai đổi field", "via_mention": False}
    assert await resolve_command(Settings(), cmd) is False   # /ai = action
    assert cmd["instruction"] == "/ai đổi field"             # untouched

    cmd2 = {"instruction": "/review xem hộ", "via_mention": False}
    assert await resolve_command(Settings(), cmd2) is True   # /review = advisory


async def test_mention_end_to_end_question_stays_advisory(monkeypatch):
    """The whole path the user asked about: someone @mentions the bot on a PR with a
    QUESTION → it is picked up (it used to be ignored entirely) and answered as advisory,
    never as a code change."""
    from ai_autopilot.config import BotIdentity
    from ai_autopilot.services.pr_feedback import command_threads

    guid = "11111111-2222-3333-4444-555555555555"
    bot = BotIdentity(identity_id=guid, display_name="Phong Pham", claimed="phong@nois.vn")
    threads = [{
        "id": 7, "status": "active",
        "comments": [{
            "id": 1, "commentType": "text",
            "author": {"displayName": "Phong", "uniqueName": "phong@nois.vn"},
            "content": (
                f'<a href="#" data-vss-mention="version:2.0,{guid}">@Phong Pham</a>'
                " chỗ này có nguy cơ SQL injection không?"
            ),
        }],
    }]
    cfg = Settings()
    cmds = command_threads(threads, cfg.comment_commands, bot=bot)
    assert len(cmds) == 1 and cmds[0]["via_mention"] is True

    monkeypatch.setattr(claude_client, "run_claude", _reply("/security"))
    advisory = await resolve_command(cfg, cmds[0])
    assert advisory is True                                   # → read_only, no push
    assert cmds[0]["instruction"].startswith("/security")     # routes to the right agent


async def test_mention_end_to_end_change_request_becomes_action(monkeypatch):
    """...and an unmistakable change request does get to act."""
    cfg = Settings()
    cmd = {"instruction": "sửa lại null check ở dòng 42 giúp mình", "via_mention": True}
    monkeypatch.setattr(claude_client, "run_claude", _reply("/ai"))
    assert await resolve_command(cfg, cmd) is False           # action → revises + pushes
    assert cmd["instruction"].startswith("/ai")
