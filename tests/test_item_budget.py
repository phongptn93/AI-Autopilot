"""Per-item token budget and the machine's circuit breaker."""

from __future__ import annotations

from ai_autopilot.data import PipelineState
from ai_autopilot.data.database import Database
from ai_autopilot.data.repository import ExecutionRepository
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from tests.test_poller_agent import _poller


def _spent(c, tokens: int) -> None:
    async def tokens_for_item(_item_id):
        return tokens

    c.execution_repo.tokens_for_item = tokens_for_item


def _item() -> WorkItemInfo:
    return WorkItemInfo(id=7, title="t", work_item_type="Task")


# ── budget ─────────────────────────────────────────────────────────────────────


async def test_tokens_for_item_sums_every_run(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    await db.create_all()
    repo = ExecutionRepository(db)
    for tokens in (1000, 2500):
        rid = await repo.start_execution(_item(), "agent")
        r = ExecutionResult.fail(7, "agent", "x")
        r.cost_tokens = tokens
        await repo.complete_execution(rid, r)
    assert await repo.tokens_for_item(7) == 3500
    assert await repo.tokens_for_item(8) == 0


async def test_a_failure_over_budget_is_held_not_retried():
    svc, c = _poller(item_budget_tokens=100_000)
    _spent(c, 150_000)
    result = ExecutionResult.fail(7, "agent", "build broke")

    await svc._handle_agent_result(_item(), result)

    assert result.needs_human and not result.success      # the run stays failed
    assert (7, PipelineState.NEEDS_HUMAN) in c.state_repo.calls
    assert (7, PipelineState.FAILED) not in c.state_repo.calls
    assert c.retry_policy.failures == []                 # no retry registered
    assert any("ngân sách" in t and "150,000" in t for _i, t in c.ado.comments)


async def test_under_budget_retries_as_before():
    svc, c = _poller(item_budget_tokens=100_000)
    _spent(c, 50_000)
    await svc._handle_agent_result(_item(), ExecutionResult.fail(7, "agent", "x"))
    assert c.retry_policy.failures and (7, PipelineState.FAILED) in c.state_repo.calls


async def test_a_success_over_budget_is_held_before_it_is_handed_on():
    svc, c = _poller(item_budget_tokens=10)
    _spent(c, 11)
    result = ExecutionResult.ok(7, "agent", "done")
    result.pr_url = "https://dev.azure.com/o/P/_git/Api/pullrequest/3"
    result.files_changed = ["Api/Orders.cs"]
    await svc._handle_agent_result(_item(), result)
    assert result.needs_human and result.success
    assert (7, PipelineState.IN_REVIEW) not in c.state_repo.calls


async def test_budget_off_and_unreadable_history_change_nothing():
    svc, c = _poller()                                    # 0 = off: never even reads
    await svc._handle_agent_result(_item(), ExecutionResult.fail(7, "agent", "x"))
    assert c.retry_policy.failures
    svc2, c2 = _poller(item_budget_tokens=10)             # fake repo has no tokens_for_item
    await svc2._handle_agent_result(_item(), ExecutionResult.fail(7, "agent", "x"))
    assert c2.retry_policy.failures


# ── circuit breaker ───────────────────────────────────────────────────────────


class _Notices:
    def __init__(self):
        self.sent: list = []

    async def notify(self, message):
        self.sent.append(message)


async def test_five_failures_in_a_row_pause_the_machine_and_say_so_once():
    svc, c = _poller()
    notices = _Notices()
    c.notifier.notify = notices.notify
    for n in range(5):
        await svc._handle_agent_result(
            WorkItemInfo(id=10 + n, title="t"),
            ExecutionResult.fail(10 + n, "agent", "PAT expired"),
        )
    assert svc.paused and svc.paused_reason == "ngắt mạch: 5 run lỗi liên tiếp"
    assert len(notices.sent) == 1 and "PAT expired" in notices.sent[0].text
    # Already paused: further failures do not announce again.
    for n in range(5):
        await svc._handle_agent_result(WorkItemInfo(id=30 + n, title="t"),
                                       ExecutionResult.fail(30 + n, "agent", "x"))
    assert len(notices.sent) == 1


async def test_a_success_resets_the_count_and_needs_human_is_neutral():
    svc, c = _poller()
    for n in range(4):
        await svc._handle_agent_result(WorkItemInfo(id=n, title="t"),
                                       ExecutionResult.fail(n, "agent", "x"))
    asked = ExecutionResult.fail(9, "agent", "which table?")
    asked.needs_human = True
    await svc._handle_agent_result(WorkItemInfo(id=9, title="t"), asked)
    assert svc._consecutive_failures == 4 and not svc.paused
    await svc._handle_agent_result(WorkItemInfo(id=8, title="t"),
                                   ExecutionResult.ok(8, "agent", "done"))
    assert svc._consecutive_failures == 0
    for n in range(4):
        await svc._handle_agent_result(WorkItemInfo(id=n, title="t"),
                                       ExecutionResult.fail(n, "agent", "x"))
    assert not svc.paused


async def test_the_breaker_can_be_turned_off():
    svc, _c = _poller(circuit_breaker_failures=0)
    for n in range(8):
        await svc._handle_agent_result(WorkItemInfo(id=n, title="t"),
                                       ExecutionResult.fail(n, "agent", "x"))
    assert not svc.paused
