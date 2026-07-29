"""Tests for the Teams bot free-text answering (read-only, tool-less).

The module is import-safe without the ``teams-bot`` extra: the microsoft-agents
packages are imported lazily inside ``build_agent`` / ``_teams_email``, so these
helpers can be exercised with plain fakes.
"""

from __future__ import annotations

import ai_autopilot.execution.claude_client as claude_client
from ai_autopilot import teams_agent
from ai_autopilot.config import Settings


class _FakeContext:
    """Records what the bot would send back to the Teams user."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_activity(self, message) -> None:
        self.sent.append(message)


class _FakeRun:
    def __init__(self, text: str):
        self.text = text


class _FakeReviewerTracker:
    async def team_overview(self) -> list:
        return []  # _format_team_overview → "(không có PR active nào)"


async def test_answer_freeform_reasons_over_snapshot(monkeypatch):
    captured = {}

    async def fake_run_claude(prompt, work_dir, **kwargs):
        captured["prompt"] = prompt
        captured["allowed_tools"] = kwargs.get("allowed_tools")
        return _FakeRun("Tuần này bạn nên tập trung vào **PR !42**.")

    async def fake_items(context, container):
        return "me@x.com", []          # empty → formatted as "(không có ...)"

    async def fake_prs(context, reviewer_tracker):
        return "me@x.com", []

    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    monkeypatch.setattr(teams_agent, "_items_data", fake_items)
    monkeypatch.setattr(teams_agent, "_prs_data", fake_prs)

    out = await teams_agent._answer_freeform(
        _FakeContext(), Settings(), container=None,
        reviewer_tracker=_FakeReviewerTracker(), question="tuần này nên ưu tiên gì?",
    )

    assert out == "Tuần này bạn nên tập trung vào **PR !42**."
    # Tool-less → cannot mutate anything.
    assert captured["allowed_tools"] == []
    # The snapshot + question are actually handed to Claude.
    assert "tuần này nên ưu tiên gì?" in captured["prompt"]
    assert "Work item của bạn" in captured["prompt"]
    assert "PR của bạn" in captured["prompt"]
    assert "Tổng quan PR của team" in captured["prompt"]


async def test_answer_freeform_falls_back_on_failure(monkeypatch):
    async def boom(*args, **kwargs):
        raise TimeoutError

    async def fake_items(context, container):
        return None, []              # unknown identity → personal sections skipped

    async def fake_prs(context, reviewer_tracker):
        return None, []

    monkeypatch.setattr(claude_client, "run_claude", boom)
    monkeypatch.setattr(teams_agent, "_items_data", fake_items)
    monkeypatch.setattr(teams_agent, "_prs_data", fake_prs)

    out = await teams_agent._answer_freeform(
        _FakeContext(), Settings(), container=None,
        reviewer_tracker=_FakeReviewerTracker(), question="bất kỳ",
    )
    assert out == teams_agent._FREEFORM_FALLBACK  # graceful, never raises


async def test_free_text_create_ticket_uses_confirm_card(monkeypatch):
    """Natural-language 'tạo ticket ...' routes to the confirm card (never creates
    directly) — reusing the same gated path as /log."""
    sent_titles = []

    async def fake_classify(config, text):
        return {"intent": "create_ticket", "filter": "đăng nhập lỗi SSO timeout"}

    async def fake_card(context, title):
        sent_titles.append(title)

    monkeypatch.setattr(teams_agent, "_classify_intent", fake_classify)
    monkeypatch.setattr(teams_agent, "_send_log_confirm_card", fake_card)

    await teams_agent._handle_free_text(
        _FakeContext(), Settings(teams_agent_nlu_enabled=True), container=None,
        reviewer_tracker=_FakeReviewerTracker(), text="tạo ticket giúp mình vụ đăng nhập lỗi",
    )
    assert sent_titles == ["đăng nhập lỗi SSO timeout"]  # confirm card, not a direct create


async def test_free_text_mutation_request_still_redirects(monkeypatch):
    """A mutation-style message is redirected to ADO BEFORE any Claude call —
    the read-only guarantee must not depend on the model."""

    async def must_not_run(*args, **kwargs):
        raise AssertionError("mutation request must not reach the freeform answerer")

    monkeypatch.setattr(teams_agent, "_answer_freeform", must_not_run)

    ctx = _FakeContext()
    await teams_agent._handle_free_text(
        ctx, Settings(teams_agent_nlu_enabled=True), container=None,
        reviewer_tracker=_FakeReviewerTracker(), text="sửa giúp bug ở PR 42",
    )
    assert ctx.sent == [teams_agent._REDIRECT_TO_ADO]
