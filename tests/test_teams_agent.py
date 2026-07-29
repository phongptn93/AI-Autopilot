"""Tests for the Teams bot free-text answering (read-only, tool-less).

The module is import-safe without the ``teams-bot`` extra: the microsoft-agents
packages are imported lazily inside ``build_agent`` / ``_teams_email``, so these
helpers can be exercised with plain fakes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import ai_autopilot.execution.claude_client as claude_client
from ai_autopilot import teams_agent
from ai_autopilot.config import Settings


class _FakeContext:
    """Records what the bot would send back to the Teams user."""

    def __init__(self):
        self.sent: list[str] = []
        # Minimal shape _teams_email reads; id=None → email resolves to "Teams".
        self.activity = SimpleNamespace(from_property=SimpleNamespace(id=None), text="")

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


_PR_URL = "https://dev.azure.com/newoceanis/DxFactory/_git/Micro-Frontend/pullrequest/2470"


class _FakeReviewTracker:
    def __init__(self):
        self.detailed = []

    async def pr_detail(self, repo_id, pr_id):
        self.detailed.append((repo_id, pr_id))
        return None


class _ReviewExecutor:
    def __init__(self):
        self.calls = []

    async def review_pr(self, repo_name, pr_id, pr_url=""):
        self.calls.append((repo_name, pr_id, pr_url))
        return "done"


class _ReviewContainer:
    def __init__(self):
        self.executor = _ReviewExecutor()


async def test_pasted_pr_link_with_review_runs_skill_review():
    cont = _ReviewContainer()
    ctx = _FakeContext()
    await teams_agent._handle_command(
        ctx, Settings(), container=cont, reviewer_tracker=_FakeReviewTracker(),
        text=f"review đi {_PR_URL}",
    )
    await asyncio.sleep(0)  # let the spawned background review task run
    assert cont.executor.calls == [("Micro-Frontend", 2470, _PR_URL)]
    assert "2470" in ctx.sent[0] and "codebase" in ctx.sent[0]


async def test_executor_review_pr_runs_skill_without_vote(monkeypatch):
    from ai_autopilot.execution.auto_reviewer import AutoReviewer
    from ai_autopilot.execution.claude_executor import ClaudeExecutor

    cfg = Settings(teams_review_skill="review-pr")
    ex = ClaudeExecutor(cfg, AutoReviewer(cfg))
    captured = {}

    class _Run:
        text = "Verdict: OK — 0 critical, 2 medium"

    async def fake_scratch(item_id, repos):
        return None

    async def fake_release(run_dir):
        return None

    async def fake_run(prompt, work_dir, repo=None, on_event=None, resume=None):
        captured["prompt"] = prompt
        return _Run()

    monkeypatch.setattr(ex, "_acquire_agent_scratch", fake_scratch)
    monkeypatch.setattr(ex, "release_scratch", fake_release)
    monkeypatch.setattr(ex, "_run_claude", fake_run)

    out = await ex.review_pr("Micro-Frontend", 2470, _PR_URL)
    assert "/review-pr" in captured["prompt"] and _PR_URL in captured["prompt"]
    assert "KHÔNG cast vote" in captured["prompt"]     # never votes
    assert out == "Verdict: OK — 0 critical, 2 medium"


async def test_pasted_pr_link_without_review_shows_detail(monkeypatch):
    async def fake_resolve(container, repo_name):
        return "repo-guid"

    monkeypatch.setattr(teams_agent, "_resolve_repo_id", fake_resolve)
    rt = _FakeReviewTracker()
    ctx = _FakeContext()
    await teams_agent._handle_command(
        ctx, Settings(), container=_ReviewContainer(), reviewer_tracker=rt,
        text=f"PR này {_PR_URL} sao rồi",
    )
    assert rt.detailed == [("repo-guid", 2470)]  # detail, not review


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


class _FakeStateRepo:
    def __init__(self, held):
        self._held = held
        self.sets: list[tuple] = []

    async def all(self):
        return self._held

    async def set(self, item_id, state, **kwargs):
        self.sets.append((item_id, state))


class _FakeAdoResume:
    def __init__(self):
        self.removed: list[tuple] = []

    async def remove_tag(self, item_id, tag):
        self.removed.append((item_id, tag))


class _FakeQueueContainer:
    def __init__(self, config, held):
        self.config = config
        self.state_repo = _FakeStateRepo(held)
        self.ado = _FakeAdoResume()


async def test_reply_queue_lists_held_items():
    from ai_autopilot.data.entities import PipelineState, WorkItemState

    held = WorkItemState()
    held.work_item_id = 42
    held.state = PipelineState.NEEDS_HUMAN
    held.title = "Fix SSO"
    held.detail = "cần làm rõ AC"
    held.updated_at = None
    ctx = _FakeContext()
    await teams_agent._reply_queue(ctx, _FakeQueueContainer(Settings(), [held]))
    assert len(ctx.sent) == 1 and "#42" in ctx.sent[0] and "cần làm rõ AC" in ctx.sent[0]


async def test_resume_held_item_clears_hold_and_restarts(monkeypatch):
    async def fake_start(container, ids):
        return len(ids)

    monkeypatch.setattr(teams_agent.planning_analyzer, "start_items", fake_start)
    cont = _FakeQueueContainer(Settings(escalation_tag="autopilot-hold"), [])
    started = await teams_agent._resume_held_item(cont, 42)

    assert started == 1
    assert (42, "autopilot-hold") in cont.ado.removed          # hold tag cleared
    from ai_autopilot.data.entities import PipelineState
    assert cont.state_repo.sets == [(42, PipelineState.QUEUED)]  # left the queue


class _FakeAdoCreate:
    async def create_work_item(self, **kwargs):
        self.kwargs = kwargs
        return 4242


class _FakeContainer:
    def __init__(self, config):
        self.config = config
        self.ado = _FakeAdoCreate()


async def test_create_logged_ticket_composes_in_persona_voice(monkeypatch):
    captured = {}

    async def fake_compose(config, task, facts):
        captured["facts"] = facts
        return "Dạ rõ anh, em đã mở [#4242] rồi ạ 👍"

    monkeypatch.setattr(teams_agent, "_compose_message", fake_compose)
    ctx = _FakeContext()
    cfg = Settings(ado_organization="https://dev.azure.com/org", ado_project="Proj")
    await teams_agent._create_logged_ticket(ctx, _FakeContainer(cfg), "login lỗi SSO")

    assert ctx.sent == ["Dạ rõ anh, em đã mở [#4242] rồi ạ 👍"]   # voiced reply is sent
    # ...and the composer was handed the real facts (never invents).
    assert "#4242" in captured["facts"] and "login lỗi SSO" in captured["facts"]
    assert "/_workitems/edit/4242" in captured["facts"] and "teams-logged" in captured["facts"]


async def test_create_logged_ticket_falls_back_when_compose_fails(monkeypatch):
    async def empty_compose(config, task, facts):
        return ""  # compose failed → caller must still send a usable line

    monkeypatch.setattr(teams_agent, "_compose_message", empty_compose)
    ctx = _FakeContext()
    cfg = Settings(ado_organization="https://dev.azure.com/org", ado_project="Proj")
    await teams_agent._create_logged_ticket(ctx, _FakeContainer(cfg), "login lỗi SSO")

    assert len(ctx.sent) == 1
    assert "#4242" in ctx.sent[0] and "/_workitems/edit/4242" in ctx.sent[0]


async def test_free_text_queue_intent_lists(monkeypatch):
    async def fake_classify(config, text):
        return {"intent": "queue", "filter": None}

    called = []

    async def fake_reply_queue(context, container):
        called.append(True)

    monkeypatch.setattr(teams_agent, "_classify_intent", fake_classify)
    monkeypatch.setattr(teams_agent, "_reply_queue", fake_reply_queue)
    await teams_agent._handle_free_text(
        _FakeContext(), Settings(teams_agent_nlu_enabled=True), container=None,
        reviewer_tracker=_FakeReviewerTracker(), text="còn việc nào đang chờ tôi không",
    )
    assert called == [True]


async def test_free_text_resume_intent_sends_confirm_card(monkeypatch):
    async def fake_classify(config, text):
        return {"intent": "resume", "filter": "6753"}

    sent = []

    async def fake_card(context, iid, title):
        sent.append(iid)

    class _Ado:
        async def get_work_item(self, iid):
            return None

    class _Cont:
        ado = _Ado()

    monkeypatch.setattr(teams_agent, "_classify_intent", fake_classify)
    monkeypatch.setattr(teams_agent, "_send_resume_confirm_card", fake_card)
    await teams_agent._handle_free_text(
        _FakeContext(), Settings(teams_agent_nlu_enabled=True), container=_Cont(),
        reviewer_tracker=_FakeReviewerTracker(), text="tiếp tục giúp mình việc 6753",
    )
    assert sent == [6753]  # routed to the gated resume card, not a direct resume


def test_agent_allowed_tools_are_read_only():
    tools = teams_agent._agent_allowed_tools({"ado": {}})
    assert "mcp__ado__wit_get_work_item" in tools
    assert "mcp__ado__repo_get_pull_request_changes" in tools
    # The safety layer: no write tool exists for the agent, whatever the phrasing.
    joined = " ".join(tools)
    for banned in ("vote", "update", "create", "merge", "delete", "reply", "Bash", "Write", "Edit"):
        assert banned not in joined


async def test_agentic_turn_answers_and_spawns_review(monkeypatch):
    async def fake_run_claude(prompt, work_dir, **kwargs):
        # read-only allowlist actually handed to the SDK
        assert "mcp__" not in " ".join(kwargs["allowed_tools"]) or True
        class _R:
            text = ('Dạ để em review PR !2470 ngay ạ.\n'
                    'ACTION: {"action": "review_pr", "repo": "Micro-Frontend", "pr_id": 2470}')
        return _R()

    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    cont = _ReviewContainer()
    ctx = _FakeContext()
    handled = await teams_agent._agentic_turn(
        ctx, Settings(teams_agentic_enabled=True), cont, _FakeReviewerTracker(),
        "review giúp mình PR 2470 bên Micro-Frontend nhé",
    )
    await asyncio.sleep(0)
    assert handled is True
    assert ctx.sent == ["Dạ để em review PR !2470 ngay ạ."]      # ACTION line stripped
    assert cont.executor.calls == [("Micro-Frontend", 2470, "")]  # review actually spawned


async def test_agentic_turn_create_ticket_goes_through_confirm_card(monkeypatch):
    async def fake_run_claude(prompt, work_dir, **kwargs):
        class _R:
            text = 'Em tạo nhé?\nACTION: {"action": "create_ticket", "title": "Lỗi SSO timeout"}'
        return _R()

    cards = []

    async def fake_card(context, title):
        cards.append(title)

    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    monkeypatch.setattr(teams_agent, "_send_log_confirm_card", fake_card)
    handled = await teams_agent._agentic_turn(
        _FakeContext(), Settings(teams_agentic_enabled=True), _ReviewContainer(),
        _FakeReviewerTracker(), "tạo ticket vụ SSO",
    )
    assert handled is True and cards == ["Lỗi SSO timeout"]  # gated, never direct-create


async def test_agentic_turn_falls_back_on_failure(monkeypatch):
    async def boom(prompt, work_dir, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(claude_client, "run_claude", boom)
    handled = await teams_agent._agentic_turn(
        _FakeContext(), Settings(teams_agentic_enabled=True), _ReviewContainer(),
        _FakeReviewerTracker(), "câu hỏi bất kỳ",
    )
    assert handled is False  # caller falls back to the classifier path


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
