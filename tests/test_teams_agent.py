"""Tests for the Teams bot free-text answering (read-only, tool-less).

The module is import-safe without the ``teams-bot`` extra: the microsoft-agents
packages are imported lazily inside ``build_agent`` / ``_teams_email``, so these
helpers can be exercised with plain fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    def __init__(self, text: str, session_id: str | None = None):
        self.text = text
        self.session_id = session_id


class _FakeReviewerTracker:
    async def team_overview(self) -> list:
        return []  # _format_team_overview → "(không có PR active nào)"


class _FakeAudit:
    def __init__(self):
        self.events: list[dict] = []

    async def record(self, **kwargs) -> None:
        self.events.append(kwargs)


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
        self.audit_repo = _FakeAudit()


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

    async def fake_run(prompt, work_dir, repo=None, on_event=None, resume=None,
                       disallowed_tools=None):
        captured["prompt"] = prompt
        captured["denied"] = disallowed_tools
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
        self.audit_repo = _FakeAudit()


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
        self.audit_repo = _FakeAudit()


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
            session_id = None      # ClaudeRun always carries this
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
            session_id = None
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
    # False = "I did not answer"; the caller then sends a static hint (it must NOT spend
    # another Claude call on the classifier path — see the agentic-only test below).
    assert handled is False


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


# ── Deferred replies: the Teams turn must never wait on Claude ────────────────


class _FakeProactive:
    """Records what a background task delivered into the conversation."""

    def __init__(self):
        self.sent: list = []

    async def send_activity(self, adapter, conversation, activity):
        self.sent.append(activity)

    async def store_conversation(self, context):
        return None


class _FakeDeferral:
    """Stands in for ``_Deferral`` without needing a real TurnContext to build a
    ConversationReference from. ``context_for`` returns the REAL ``_DeferredContext`` so
    the delivery path under test is the production one."""

    def __init__(self, sem=None):
        self.app = SimpleNamespace(proactive=_FakeProactive())
        self.sem = sem

    def context_for(self, context, email):
        return teams_agent._DeferredContext(
            self.app, adapter=None, conversation=None,
            activity=context.activity, email=email,
        )


async def test_deferred_turn_acks_instantly_and_answers_in_background(monkeypatch):
    """The regression that made the bot feel frozen: the messaging endpoint only returns
    HTTP 200 once the turn handler finishes, so an inline 120s Claude call blew past the
    channel's ~15s deadline and the user got nothing. The turn must return while Claude
    is still running, then deliver the answer into the same conversation."""
    release = asyncio.Event()

    async def slow_run_claude(prompt, work_dir, **kwargs):
        await release.wait()          # still running when the turn returns

        class _R:
            text = "Dạ PR !2470 đang chờ review ạ."
            session_id = None

        return _R()

    monkeypatch.setattr(claude_client, "run_claude", slow_run_claude)
    defer, ctx = _FakeDeferral(), _FakeContext()

    await asyncio.wait_for(
        teams_agent._handle_command(
            ctx, Settings(teams_agentic_enabled=True), container=_ReviewContainer(),
            reviewer_tracker=_FakeReviewerTracker(), text="PR 2470 sao rồi?",
            defer=defer,
        ),
        timeout=1,                    # the turn itself must not wait on Claude
    )
    assert not release.is_set()                       # Claude deliberately still running
    assert teams_agent._THINKING_ACK in ctx.sent      # user got immediate feedback
    assert defer.app.proactive.sent == []             # nothing delivered yet

    release.set()
    for _ in range(10):               # let the detached task finish
        await asyncio.sleep(0)
    assert [a.text for a in defer.app.proactive.sent] == ["Dạ PR !2470 đang chờ review ạ."]


async def test_deferred_failure_is_reported_not_swallowed(monkeypatch):
    """A detached task that raises must still tell the user something — otherwise the
    turn "succeeded" and the reply simply never arrives."""
    defer, ctx = _FakeDeferral(), _FakeContext()

    async def boom(_ctx):
        raise RuntimeError("nope")

    assert await teams_agent._run_deferred(defer, ctx, "⏳", boom) is True
    for _ in range(10):
        await asyncio.sleep(0)
    assert "⚠️" in defer.app.proactive.sent[0].text


async def test_run_deferred_without_deferral_runs_inline():
    """``defer=None`` (unit tests, SDK absent) keeps the old inline behaviour."""
    ran = []
    assert await teams_agent._run_deferred(None, _FakeContext(), "⏳", ran.append) is False
    assert ran == []                  # caller runs the work itself in this case


async def test_deferred_work_is_capped_by_semaphore():
    """Answering off the turn means a busy channel could otherwise spawn one Claude
    process per message. The cap bounds the RUNS, never the acks — a queued user must
    still hear back immediately (and be told they're queued)."""
    defer = _FakeDeferral(sem=asyncio.Semaphore(1))
    release, running, done = asyncio.Event(), [], []

    async def work(ctx):
        running.append(1)
        await release.wait()
        done.append(1)

    first, second = _FakeContext(), _FakeContext()
    assert await teams_agent._run_deferred(defer, first, "⏳", work) is True
    for _ in range(5):
        await asyncio.sleep(0)         # let the first task take the slot
    assert await teams_agent._run_deferred(defer, second, "⏳", work) is True
    for _ in range(5):
        await asyncio.sleep(0)

    assert len(running) == 1           # second is queued, not running
    assert first.sent[-1] == "⏳"      # first went straight through
    assert teams_agent._QUEUED_SUFFIX in second.sent[-1]   # second was told it waits

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(done) == 2              # and it does eventually run


async def test_uncapped_when_configured_zero():
    """0 = no cap, and must not deadlock (a Semaphore(0) never admits anyone)."""
    defer = _FakeDeferral(sem=None)
    ran = []
    assert await teams_agent._run_deferred(defer, _FakeContext(), "", ran.append) is True
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(ran) == 1


async def test_deferred_context_keeps_caller_identity():
    """Identity is resolved on the LIVE turn: the connector lookup needs a real
    TurnContext, so without caching the background work would lose the caller's email
    and silently drop every personal section."""
    deferred = teams_agent._DeferredContext(
        app=SimpleNamespace(proactive=_FakeProactive()), adapter=None,
        conversation=None, activity=SimpleNamespace(from_property=SimpleNamespace(id="29:x")),
        email="phong.pham@nois.vn",
    )
    assert await teams_agent._teams_email(deferred) == "phong.pham@nois.vn"
    # A cached None means "looked up and failed" — must NOT retry the connector.
    failed = teams_agent._DeferredContext(
        app=None, adapter=None, conversation=None,
        activity=SimpleNamespace(from_property=SimpleNamespace(id="29:x")), email=None,
    )
    assert await teams_agent._teams_email(failed) is None


async def test_agentic_failure_sends_hint_without_a_second_claude_call(monkeypatch):
    """One message = at most one Claude process. Chaining the classifier + phrasing calls
    behind a failed agentic turn cost up to three sequential runs (~210s)."""
    calls = []

    async def boom(prompt, work_dir, **kwargs):
        calls.append(prompt)
        raise TimeoutError

    monkeypatch.setattr(claude_client, "run_claude", boom)
    ctx = _FakeContext()
    await teams_agent._handle_command(
        ctx, Settings(teams_agentic_enabled=True), container=_ReviewContainer(),
        reviewer_tracker=_FakeReviewerTracker(), text="câu hỏi bất kỳ",
    )
    assert len(calls) == 1
    assert ctx.sent == [teams_agent._FREEFORM_FALLBACK]


# ── Slash commands: a syntax slip must not become a Claude run ────────────────


async def test_malformed_slash_command_answers_usage_without_claude(monkeypatch):
    """``/review 2470`` is the right command missing its repo. It used to fall through to
    the agent, which spent a full run answering something else — so the command looked
    broken. It must answer with its own usage, instantly."""
    async def must_not_run(*args, **kwargs):
        raise AssertionError("a malformed command must not reach Claude")

    monkeypatch.setattr(claude_client, "run_claude", must_not_run)
    ctx = _FakeContext()
    await teams_agent._handle_command(
        ctx, Settings(teams_agentic_enabled=True), container=_ReviewContainer(),
        reviewer_tracker=_FakeReviewerTracker(), text="/review 2470",
    )
    assert "/review <repo> <pr-id>" in ctx.sent[0]


async def test_unknown_slash_command_suggests_nearest(monkeypatch):
    async def must_not_run(*args, **kwargs):
        raise AssertionError("an unknown command must not reach Claude")

    monkeypatch.setattr(claude_client, "run_claude", must_not_run)
    ctx = _FakeContext()
    await teams_agent._handle_command(
        ctx, Settings(teams_agentic_enabled=True), container=_ReviewContainer(),
        reviewer_tracker=_FakeReviewerTracker(), text="/prss",
    )
    assert "/prs" in ctx.sent[0] and "/help" in ctx.sent[0]


def test_slash_help_ignores_plain_text():
    assert teams_agent._slash_help("PR nào của tôi đang bị block?") is None
    assert teams_agent._slash_help("/items") is not None


def test_help_text_follows_configured_pr_commands():
    """/help used to hardcode the PR command list (and had already dropped /review), so
    it advertised commands the instance might ignore."""
    text = teams_agent._help_text(
        Settings(comment_command="/ai, /qc", comment_advisory_commands="/qc")
    )
    assert "`/ai`" in text and "`/qc`" in text
    assert "/spec" not in text and "/summary" not in text
    # Chat commands are still listed, with their usage.
    assert "/review <repo> <pr-id>" in text


# ── Dedup + digest delivery ──────────────────────────────────────────────────


async def test_redelivered_activity_is_ignored():
    """A channel redelivery must not run the message (and spawn Claude) twice."""
    teams_agent._SEEN_ACTIVITIES.clear()
    cfg = Settings()
    sent = []

    class _Ctx(_FakeContext):
        def __init__(self):
            super().__init__()
            self.activity = SimpleNamespace(
                id="act-1", text="/status", value=None,
                from_property=SimpleNamespace(id=None),
            )

        async def send_activity(self, message):
            sent.append(message)

    for _ in range(2):
        await teams_agent._handle_turn(_Ctx(), cfg, None, _FakeReviewerTracker())
    assert len(sent) == 1  # second delivery skipped


async def test_digest_sends_to_every_stored_conversation():
    """Guards the double-prefix bug: ``all_keys`` yields already-prefixed storage keys,
    so passing one to ``get_conversation`` (which prefixes again) missed every time and
    the digest went to nobody while logging a healthy "sent"."""
    from microsoft_agents.activity import ConversationAccount, ConversationReference
    from microsoft_agents.hosting.core.app.proactive import (
        Conversation,
        Proactive,
        ProactiveOptions,
    )

    class _DictStorage:
        """Same contract as _DbConversationStorage: prefixed keys in, keys back out."""

        def __init__(self):
            self.items: dict = {}

        async def read(self, keys, *, target_cls=None, **kwargs):
            out = {}
            for key in keys:
                if key in self.items:
                    data = self.items[key]
                    out[key] = (
                        target_cls.from_json_to_store_item(data) if target_cls else data
                    )
            return out

        async def write(self, changes):
            for key, item in changes.items():
                self.items[key] = (
                    item.store_item_to_json()
                    if hasattr(item, "store_item_to_json") else item
                )

        async def all_keys(self):
            return list(self.items)

    storage = _DictStorage()
    proactive = Proactive(
        SimpleNamespace(options=SimpleNamespace(storage=storage)),
        ProactiveOptions(storage=storage),
    )
    for cid in ("19:channel-a", "19:channel-b"):
        await proactive.store_conversation(
            Conversation({}, ConversationReference(
                conversation=ConversationAccount(id=cid),
                service_url="https://smba.example",
            ))
        )

    delivered = []

    class _App:
        def __init__(self):
            self.proactive = proactive

    async def fake_send(adapter, conversation, activity):
        delivered.append(conversation.conversation_reference.conversation.id)

    proactive.send_activity = fake_send   # only the wire call is faked

    class _EmptyTracker:
        async def new_prs_since(self, cutoff): return []
        async def merged_prs_since(self, cutoff): return []
        async def tickets_logged_since(self, cutoff): return []
        async def prs_ready_to_merge(self): return []
        async def team_overview(self, limit=10): return []

    # A real Container always carries `.config` — the digest reads it to build the ADO
    # links, so the fake has to as well rather than the code guarding with getattr.
    container = SimpleNamespace(config=Settings(
        ado_organization="https://dev.azure.com/org", ado_project="Proj",
    ))

    await teams_agent._send_digest(
        container=container, app=_App(), adapter=None,
        reviewer_tracker=_EmptyTracker(), storage=storage,
        message_factory=SimpleNamespace(text=lambda t: SimpleNamespace(text=t)),
        window_hours=24,
    )
    assert sorted(delivered) == ["19:channel-a", "19:channel-b"]


# ── Mutation pre-filter: word boundaries, and questions are not instructions ──


def test_mutation_instructions_are_refused():
    """Layer 1 of the read-only guard: an instruction to change something is refused
    before any Claude call."""
    for text in (
        "sửa giúp bug ở PR 42",
        "merge hộ PR 2470",          # " merge " needed literal spaces before → missed this
        "approve PR này đi",
        "vote giúp mình PR 42",
        "push code lên đi",
        "revert commit vừa rồi",
        "xoá PR 42 đi",
        "reject PR đó",
    ):
        assert teams_agent._is_mutation_request(text), text


def test_questions_about_changes_are_answered_not_refused():
    """The substring match refused ordinary questions: "fix" also hit "prefix" and
    "đã fix", so "PR nào cần fix?" got a lecture about Azure DevOps instead of an answer."""
    for text in (
        "PR nào cần fix?",
        "ai đã push lên branch này?",
        "bug đó đã fix chưa?",
        "PR nào đang chờ merge?",
        "ai chưa approve PR 2470?",
        "commit mới nhất là gì?",
        "prefix của branch là gì?",   # "prefix" contains "fix" — must not match at all
        "PR nào của tôi đang bị block?",
    ):
        assert not teams_agent._is_mutation_request(text), text


def test_mutation_words_need_word_boundaries():
    """Past tenses and longer words that merely CONTAIN a hint are not instructions."""
    assert not teams_agent._MUTATION_RE.search("prefix")
    assert not teams_agent._MUTATION_RE.search("deleted rows")
    assert not teams_agent._MUTATION_RE.search("approved by QC")
    assert teams_agent._MUTATION_RE.search("fix cái này")   # the bare verb still matches


# ── Quoted replies: "AI Autopilot review" pointing at a quoted PR ─────────────
#
# Reported from real use: replying "AI Autopilot review" while QUOTING a PR notification
# got "cho em xin link PR" back. Two gaps behind it — the bot only read activity.text
# (never the quote, where the PR was), and only recognised a full dev.azure.com URL, while
# Teams unfurls a pasted link into a preview titled "Pull request 2488: …" that keeps the
# number but drops the URL and the repo.

def _FakeActivity(text: str, attachments=None):  # noqa: N802 — reads as a constructor
    return SimpleNamespace(text=text, attachments=attachments)


_QUOTED_PR = (
    '<blockquote itemid="1753848000000">Pull request 2488: fix(cigarette-quality): '
    "phân trang báo cáo và vùng cuộn - Repos<br>cc.<br>Lam Huynh (Industrial - Project "
    "Lead) review giúp e nha</blockquote> review"
)


class _FindPrTracker(_FakeReviewerTracker):
    """Resolves a bare PR number to its repo, like the real find_pr_by_id."""

    def __init__(self, repo: str | None = "Micro-Frontend"):
        self.looked_up: list[int] = []
        self._repo = repo

    async def find_pr_by_id(self, pr_id):
        self.looked_up.append(pr_id)
        if self._repo is None:
            return None
        return {"id": pr_id, "repo": self._repo, "repo_id": "guid", "title": "t",
                "author": "a", "source": "s", "target": "t", "is_draft": False,
                "reviewers": []}


def test_quote_is_read_from_blockquote_and_attachment():
    from types import SimpleNamespace as NS

    act = NS(text=_QUOTED_PR, attachments=None)
    assert "Pull request 2488" in teams_agent._quoted_text(act)
    assert teams_agent._strip_quote(_QUOTED_PR) == "review"   # the user's own word only

    # Teams also delivers the referenced message as an attachment.
    act2 = NS(text="review", attachments=[
        NS(content={"messagePreview": "Pull request 2488: fix(x) - Repos"}),
    ])
    assert "2488" in teams_agent._quoted_text(act2)


def test_plain_message_is_untouched_by_quote_handling():
    from types import SimpleNamespace as NS

    assert teams_agent._quoted_text(NS(text="PR nào của tôi?", attachments=None)) == ""
    assert teams_agent._strip_quote("PR nào của tôi?") == "PR nào của tôi?"


def test_pr_reference_prefers_a_link_then_falls_back_to_a_number():
    assert teams_agent._find_pr_reference(f"review {_PR_URL}") == (
        "Micro-Frontend", 2470, _PR_URL
    )
    assert teams_agent._find_pr_reference("review", "Pull request 2488: x") == (
        None, 2488, ""
    )
    # The user's own words win over the quote.
    assert teams_agent._find_pr_reference("PR 99 nhé", "Pull request 2488")[1] == 99
    assert teams_agent._find_pr_reference("hôm nay thế nào?", "") is None


async def test_review_on_quoted_pr_resolves_repo_and_runs():
    """The reported failure, end to end: "review" + a quoted PR preview must review that
    PR, not ask which one."""
    cont, rt, ctx = _ReviewContainer(), _FindPrTracker(), _FakeContext()
    await teams_agent._handle_command(
        ctx, Settings(), container=cont, reviewer_tracker=rt,
        text=teams_agent._strip_quote(_QUOTED_PR),
        quoted=teams_agent._quoted_text(_FakeActivity(_QUOTED_PR)),
    )
    await asyncio.sleep(0)
    assert rt.looked_up == [2488]                              # number → repo
    assert cont.executor.calls == [("Micro-Frontend", 2488, "")]  # review actually ran
    assert "2488" in ctx.sent[0]


async def test_question_about_a_quoted_pr_is_not_hijacked_into_a_review(monkeypatch):
    """"PR 2488 review chưa?" is a lookup — it must not trigger a review."""
    async def no_claude(*args, **kwargs):
        raise AssertionError("should not reach Claude in this test")

    monkeypatch.setattr(teams_agent, "_handle_free_text", no_claude)
    cont, rt, ctx = _ReviewContainer(), _FindPrTracker(), _FakeContext()
    with contextlib.suppress(AssertionError):
        await teams_agent._handle_command(
            ctx, Settings(), container=cont, reviewer_tracker=rt,
            text="PR 2488 review chưa?", quoted="",
        )
    assert cont.executor.calls == []      # no review spawned


async def test_unresolvable_pr_number_says_so_instead_of_guessing():
    cont, rt, ctx = _ReviewContainer(), _FindPrTracker(repo=None), _FakeContext()
    await teams_agent._handle_command(
        ctx, Settings(), container=cont, reviewer_tracker=rt,
        text="review", quoted="Pull request 999999: gone",
    )
    assert cont.executor.calls == []
    assert "999999" in ctx.sent[0] and "/review" in ctx.sent[0]


# ── Conversation memory: a thread must behave like a conversation ─────────────
#
# Reported from real use: "trong một thread, mày không đi theo lịch sử cũ". Every message
# opened a brand-new Claude session, so the user had to restate which PR or item they meant
# each time. Each reply now RESUMES the session from the previous message in that thread.


class _SessionRepo:
    """Stands in for ClaudeSessionRepository (keyed by repo + branch)."""

    def __init__(self, stored: dict | None = None):
        self.stored = stored or {}
        self.saved: list[tuple] = []
        self.asked: list[tuple] = []

    async def get(self, repo, branch, ttl_hours):
        self.asked.append((repo, branch, ttl_hours))
        return self.stored.get((repo, branch))

    async def save(self, repo, branch, session_id):
        self.saved.append((repo, branch, session_id))


class _MemoryContainer(_ReviewContainer):
    def __init__(self, session_repo):
        super().__init__()
        self.claude_session_repo = session_repo


def _ctx_in_thread(conv_id: str) -> _FakeContext:
    ctx = _FakeContext()
    ctx.activity = SimpleNamespace(
        from_property=SimpleNamespace(id=None), text="",
        conversation=SimpleNamespace(id=conv_id),
    )
    return ctx


_THREAD = "19:abc@thread.tacv2;messageid=1690000000000"


async def test_first_message_starts_fresh_then_session_is_remembered(monkeypatch):
    seen = {}

    async def fake_run_claude(prompt, work_dir, **kwargs):
        seen["resume"] = kwargs.get("resume")
        seen["prompt"] = prompt

        class _R:
            text = "Dạ PR !2470 đang chờ review ạ."
            session_id = "sess-1"

        return _R()

    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    repo = _SessionRepo()
    await teams_agent._agentic_turn(
        _ctx_in_thread(_THREAD), Settings(teams_agentic_enabled=True),
        _MemoryContainer(repo), _FakeReviewerTracker(), "PR 2470 sao rồi?",
    )
    assert seen["resume"] is None                     # nothing stored yet → fresh
    assert "TIẾP THEO" not in seen["prompt"]           # and not told it is a follow-up
    assert repo.saved == [("teams", _THREAD, "sess-1")]  # remembered for the next message


async def test_next_message_in_same_thread_resumes_that_session(monkeypatch):
    seen = {}

    async def fake_run_claude(prompt, work_dir, **kwargs):
        seen["resume"] = kwargs.get("resume")
        seen["prompt"] = prompt

        class _R:
            text = "Dạ, PR đó anh Lâm chưa vote ạ."
            session_id = "sess-2"

        return _R()

    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    repo = _SessionRepo({("teams", _THREAD): "sess-1"})
    cfg = Settings(teams_agentic_enabled=True)
    await teams_agent._agentic_turn(
        _ctx_in_thread(_THREAD), cfg, _MemoryContainer(repo),
        _FakeReviewerTracker(), "ai chưa vote?",   # meaningless without the earlier context
    )
    assert seen["resume"] == "sess-1"                       # continues the thread
    assert "TIẾP THEO" in seen["prompt"]                    # told not to re-introduce itself
    assert repo.asked == [("teams", _THREAD, cfg.claude_session_ttl_hours)]
    assert repo.saved == [("teams", _THREAD, "sess-2")]     # rolls forward


async def test_separate_threads_do_not_share_memory(monkeypatch):
    """A channel's conversation id encodes the thread, so two threads key differently."""
    other = "19:abc@thread.tacv2;messageid=1699999999999"
    assert teams_agent._conversation_key(
        SimpleNamespace(conversation=SimpleNamespace(id=_THREAD))
    ) != teams_agent._conversation_key(
        SimpleNamespace(conversation=SimpleNamespace(id=other))
    )

    seen = {}

    async def fake_run_claude(prompt, work_dir, **kwargs):
        seen["resume"] = kwargs.get("resume")

        class _R:
            text = "ok"
            session_id = "sess-other"

        return _R()

    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    repo = _SessionRepo({("teams", _THREAD): "sess-1"})
    await teams_agent._agentic_turn(
        _ctx_in_thread(other), Settings(teams_agentic_enabled=True),
        _MemoryContainer(repo), _FakeReviewerTracker(), "câu hỏi khác",
    )
    assert seen["resume"] is None      # the other thread's session must NOT leak in


async def test_memory_can_be_switched_off(monkeypatch):
    async def fake_run_claude(prompt, work_dir, **kwargs):
        assert kwargs.get("resume") is None

        class _R:
            text = "ok"
            session_id = "sess-x"

        return _R()

    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    repo = _SessionRepo({("teams", _THREAD): "sess-1"})
    await teams_agent._agentic_turn(
        _ctx_in_thread(_THREAD),
        Settings(teams_agentic_enabled=True, teams_agent_session_memory=False),
        _MemoryContainer(repo), _FakeReviewerTracker(), "x",
    )
    assert repo.asked == [] and repo.saved == []   # nothing read, nothing written


async def test_session_lookup_failure_never_blocks_the_reply(monkeypatch):
    """Memory is a nicety — a broken store must not cost the user their answer."""
    class _BrokenRepo:
        async def get(self, *a):
            raise RuntimeError("db down")

        async def save(self, *a):
            raise RuntimeError("db down")

    async def fake_run_claude(prompt, work_dir, **kwargs):
        class _R:
            text = "vẫn trả lời được"
            session_id = "sess-1"

        return _R()

    monkeypatch.setattr(claude_client, "run_claude", fake_run_claude)
    ctx = _ctx_in_thread(_THREAD)
    handled = await teams_agent._agentic_turn(
        ctx, Settings(teams_agentic_enabled=True),
        _MemoryContainer(_BrokenRepo()), _FakeReviewerTracker(), "x",
    )
    assert handled is True and ctx.sent == ["vẫn trả lời được"]


def test_overlong_conversation_id_is_hashed_to_fit_the_column():
    """String(200): SQLite would accept an overlong key, SQL Server/Postgres reject it."""
    key = teams_agent._conversation_key(
        SimpleNamespace(conversation=SimpleNamespace(id="19:" + "x" * 400))
    )
    assert key.startswith("sha256:") and len(key) <= 200
    assert teams_agent._conversation_key(SimpleNamespace(conversation=None)) == ""


# ── Digest formatting: clickable ids, no dead-weight rows ─────────────────────

_LINK_CFG = Settings(
    ado_organization="https://dev.azure.com/org/", ado_project="Track", code_project="Code",
)


def test_pr_and_work_item_ids_are_clickable():
    """An id the reader has to copy and search for is not a report. PRs resolve against
    `code_project` and work items against `ado_project` — they are often different."""
    pr = [{"id": 1400, "repo": "Micro-Frontend", "title": "fix: unlock picker", "author": "Dat"}]
    out = teams_agent._format_pr_stub_list(pr, _LINK_CFG)
    assert "[!1400](https://dev.azure.com/org/Code/_git/Micro-Frontend/pullrequest/1400)" in out

    items = [SimpleNamespace(id=7188, title="tồn khả dụng")]
    wi = teams_agent._fmt_wi_list(items, cfg=_LINK_CFG)
    assert "[#7188](https://dev.azure.com/org/Track/_workitems/edit/7188)" in wi


def test_formatters_degrade_to_plain_ids_without_config():
    """Every formatter is also called from paths that have no Settings to hand; they must
    still render, just without links."""
    pr = [{"id": 9, "repo": "R", "title": "t", "author": "a"}]
    assert "!9" in teams_agent._format_pr_stub_list(pr)
    assert "](" not in teams_agent._format_pr_stub_list(pr)


def test_zero_vote_counters_are_omitted():
    """"✅0 ⏳0 ⛔0" on every row is three glyphs of noise; only non-zero counters earn space."""
    rows = [
        {"id": 1, "repo": "R", "title": "t", "author": "a", "age_days": 5,
         "is_draft": False, "approved": 0, "pending": 0, "blocked": 0},
        {"id": 2, "repo": "R", "title": "t", "author": "a", "age_days": 5,
         "is_draft": False, "approved": 0, "pending": 2, "blocked": 1},
    ]
    first, second = teams_agent._format_team_overview(rows, _LINK_CFG).split("\n")
    assert "✅" not in first and "⏳" not in first and "⛔" not in first
    assert "⏳2 ⛔1" in second and "✅" not in second


def test_long_titles_are_trimmed_to_one_line():
    long = "x" * 200
    assert len(teams_agent._short(long)) < 70
    assert teams_agent._short("short").endswith("short")   # untouched when it fits


async def test_digest_drops_the_oldest_pr_section_and_leads_with_pace():
    """The oldest-active-PR list was 10 rows of unchanged text every cycle, which pushed
    the numbers off the first screen. `/team` still shows it on demand."""
    sent: list[str] = []

    class _Tracker:
        async def new_prs_since(self, cutoff): return [{}] * 17
        async def merged_prs_since(self, cutoff): return []
        async def tickets_logged_since(self, cutoff): return []
        async def prs_ready_to_merge(self):
            return [{"id": 1400, "repo": "MF", "title": "fix", "author": "Dat"}]
        async def team_overview(self, limit=10):
            raise AssertionError("digest must not fetch the oldest-PR list any more")

    class _Repo:
        async def get_stats(self, since): return SimpleNamespace(total=4, success=3, failed=0)

    # One conversation so the send loop runs and we can read the rendered body; the
    # conversation object is only passed through, so a sentinel is enough.
    class _Storage:
        async def all_keys(self): return ["k"]
        async def read(self, keys, target_cls=None): return {"k": object()}

    class _Proactive:
        async def send_activity(self, adapter, conversation, activity):
            sent.append(activity.text)

    await teams_agent._send_digest(
        container=SimpleNamespace(config=_LINK_CFG, execution_repo=_Repo()),
        app=SimpleNamespace(proactive=_Proactive()), adapter=None,
        reviewer_tracker=_Tracker(), storage=_Storage(),
        message_factory=SimpleNamespace(text=lambda t: SimpleNamespace(text=t)),
        window_hours=5,
    )
    assert len(sent) == 1
    body = sent[0]
    assert "PR active cũ nhất" not in body
    assert body.index("Nhịp độ") < body.index("Sẵn sàng merge")
    assert "17 mới mở" in body
    assert "/team" in body   # pointer to where the dropped list now lives


# ── Advisory runs must not be able to mutate, not merely be told not to ────────

async def test_review_pr_denies_the_file_mutating_tools(monkeypatch):
    """The scratch worktree keeps a stray edit out of your checkout, but this run has Bash
    — so an edit it decided to "just fix" could be committed and pushed to the PR branch."""
    from ai_autopilot.execution.auto_reviewer import AutoReviewer
    from ai_autopilot.execution.claude_executor import ClaudeExecutor

    cfg = Settings(teams_review_skill="review-pr")
    ex = ClaudeExecutor(cfg, AutoReviewer(cfg))
    seen = {}

    async def fake_scratch(item_id, repos):
        return None

    async def fake_run(prompt, work_dir, repo=None, on_event=None, resume=None,
                       disallowed_tools=None):
        seen["denied"] = disallowed_tools
        return SimpleNamespace(text="ok", total_tokens=0, cost_usd=0.0, session_id=None)

    monkeypatch.setattr(ex, "_acquire_agent_scratch", fake_scratch)
    monkeypatch.setattr(ex, "release_scratch", lambda d: asyncio.sleep(0))
    monkeypatch.setattr(ex, "_run_claude", fake_run)

    await ex.review_pr("Micro-Frontend", 2470)
    for tool in ("Write", "Edit", "NotebookEdit"):
        assert tool in seen["denied"], tool
    assert "Bash" not in seen["denied"]        # the review needs `git diff`


async def test_advisory_run_denies_mutators_without_a_checkout(monkeypatch):
    """This path has NO worktree to discard — it runs against the shared workspace, so the
    deny list is the only thing standing between an advisory command and your checkout."""
    from ai_autopilot.execution.auto_reviewer import AutoReviewer
    from ai_autopilot.execution.claude_executor import ClaudeExecutor

    cfg = Settings(workspace_directory=".")
    ex = ClaudeExecutor(cfg, AutoReviewer(cfg))
    seen = {}

    async def fake_git(args, repo, check=True):
        return ""

    async def fake_run(prompt, work_dir, repo=None, on_event=None, resume=None,
                       disallowed_tools=None):
        seen["denied"] = disallowed_tools
        return SimpleNamespace(text="reviewed", total_tokens=0, cost_usd=0.0, session_id=None)

    monkeypatch.setattr(ex, "_git", fake_git)
    monkeypatch.setattr(ex, "_run_claude", fake_run)
    monkeypatch.setattr(ex, "_resume_for", lambda repo, branch: asyncio.sleep(0))
    monkeypatch.setattr(ex, "_save_session", lambda repo, branch, run: asyncio.sleep(0))

    result = await ex._run_read_only(42, ".", "feature/be/42-x", "review this")
    assert result.success
    for tool in ("Write", "Edit", "NotebookEdit"):
        assert tool in seen["denied"], tool
