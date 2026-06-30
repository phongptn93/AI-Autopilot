"""Tests for the poller's AI-native result handling (_handle_agent_result)."""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.data import PipelineState
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.services.poller import AdoPollerService


class _FakeAdo:
    def __init__(self):
        self.tags: list[tuple[int, str]] = []
        self.comments: list[tuple[int, str]] = []

    async def add_tag(self, work_item_id, tag):
        self.tags.append((work_item_id, tag))

    async def add_comment(self, work_item_id, text):
        self.comments.append((work_item_id, text))

    async def get_work_item(self, work_item_id):
        return WorkItemInfo(id=work_item_id, title="t", work_item_type="Task")


class _FakeExec:
    def __init__(self, dispatch=(True, "autopilot-7"), final=None):
        self._dispatch, self._final = dispatch, final

    async def dispatch_interactive(self, item, *, autonomy, draft_pr):
        return self._dispatch

    def finalize_interactive(self, item):
        return self._final


class _FakeExecRepo:
    def __init__(self):
        self.completed: list[tuple[int, bool]] = []

    async def start_execution(self, item, skill):
        return 99

    async def complete_execution(self, record_id, result):
        self.completed.append((record_id, result.success))


class _FakeCost:
    async def track(self, record_id, tokens):
        pass


class _FakeNotifier:
    def __init__(self):
        self.completed: list[tuple[bool, bool]] = []

    async def notify_completed(self, item, result, mark_processed=True):
        self.completed.append((result.success, mark_processed))


class _FakeRetry:
    def __init__(self, exhausted=False):
        self.successes: list[int] = []
        self.failures: list[tuple[int, str]] = []
        self._exhausted = exhausted

    def record_success(self, work_item_id):
        self.successes.append(work_item_id)

    def record_failure(self, work_item_id, error):
        self.failures.append((work_item_id, error))

    def is_exhausted(self, work_item_id):
        return self._exhausted

    def get_state(self, work_item_id):
        return None


class _FakeState:
    def __init__(self):
        self.calls: list[tuple[int, object]] = []

    async def set(self, work_item_id, state, *, title="", detail=None, pr_url=None):
        self.calls.append((work_item_id, state))


def _poller(autonomy="assisted", exhausted=False) -> tuple[AdoPollerService, SimpleNamespace]:
    cfg = Settings(workspace_directory=r"C:\ws", autonomy_level=autonomy)
    c = SimpleNamespace(
        config=cfg, ado=_FakeAdo(), notifier=_FakeNotifier(),
        retry_policy=_FakeRetry(exhausted), state_repo=_FakeState(),
        executor=_FakeExec(), execution_repo=_FakeExecRepo(), cost_tracker=_FakeCost(),
    )
    return AdoPollerService(c), c


def _item() -> WorkItemInfo:
    return WorkItemInfo(id=7, title="t", work_item_type="Task")


async def test_needs_human_escalates_and_does_not_retry():
    p, c = _poller()
    res = ExecutionResult.fail(7, "agent", "AC unclear")
    res.needs_human = True
    await p._handle_agent_result(_item(), res)
    assert 7 in c.retry_policy.successes            # treated as resolved, not retried
    assert any("Needs human" in t for _, t in c.ado.comments)
    assert c.ado.tags == [(7, c.config.escalation_tag)]   # held, not processed/review
    assert (7, PipelineState.NEEDS_HUMAN) in c.state_repo.calls


async def test_completed_draft_tags_review():
    p, c = _poller(autonomy="assisted")              # pr_is_draft == True
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = "https://pr"
    await p._handle_agent_result(_item(), res)
    assert (7, c.config.review_tag) in c.ado.tags
    assert any("PR created (draft)" in t for _, t in c.ado.comments)
    assert (7, PipelineState.IN_REVIEW) in c.state_repo.calls


async def test_unattended_completed_marks_processed():
    p, c = _poller(autonomy="unattended")            # pr_is_draft == False
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = "https://pr"
    await p._handle_agent_result(_item(), res)
    assert c.notifier.completed == [(True, True)]     # notify_completed(mark_processed=True)


async def test_report_mode_marks_processed_without_pr():
    p, c = _poller(autonomy="report")
    res = ExecutionResult.ok(7, "agent", "planned")  # no pr_url
    await p._handle_agent_result(_item(), res)
    assert (7, c.config.processed_tag) in c.ado.tags


async def test_failure_retries_when_not_exhausted():
    p, c = _poller(exhausted=False)
    res = ExecutionResult.fail(7, "agent", "boom")
    await p._handle_agent_result(_item(), res)
    assert c.retry_policy.failures == [(7, "boom")]
    assert not any("gave up" in t for _, t in c.ado.comments)


async def test_failure_gives_up_when_exhausted():
    p, c = _poller(exhausted=True)
    res = ExecutionResult.fail(7, "agent", "boom")
    await p._handle_agent_result(_item(), res)
    assert any("gave up" in t for _, t in c.ado.comments)


async def test_dispatch_interactive_tracks_live_session():
    p, c = _poller()
    c.executor = _FakeExec(dispatch=(True, "autopilot-7"))
    await p._dispatch_interactive(_item())
    assert p._live == {7: 99}                                 # tracked for finalisation
    assert (7, PipelineState.IN_PROGRESS) in c.state_repo.calls
    assert any("Live session started" in t for _, t in c.ado.comments)


async def test_finalize_live_session_when_result_ready():
    p, c = _poller()
    done = ExecutionResult.ok(7, "agent", "done")
    done.pr_url = "https://pr"
    c.executor = _FakeExec(final=done)
    p._live = {7: 99}
    await p._finalize_live_sessions()
    assert p._live == {}                                       # cleared after finalise
    assert c.execution_repo.completed == [(99, True)]
    assert (7, c.config.review_tag) in c.ado.tags              # went to In review


async def test_finalize_skips_while_session_running():
    p, c = _poller()
    c.executor = _FakeExec(final=None)                         # no result.json yet
    p._live = {7: 99}
    await p._finalize_live_sessions()
    assert p._live == {7: 99}                                  # still live, not finalised
