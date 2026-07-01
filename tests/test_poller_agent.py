"""Tests for the poller's AI-native result handling (_handle_agent_result)."""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.data import PipelineState
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.services.poller import AdoPollerService, outcome_policy


class _FakeAdo:
    def __init__(self):
        self.tags: list[tuple[int, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.states: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []
        self.tagged_items: list = []

    async def add_tag(self, work_item_id, tag):
        self.tags.append((work_item_id, tag))

    async def remove_tag(self, work_item_id, tag):
        self.removed.append((work_item_id, tag))

    async def add_comment(self, work_item_id, text):
        self.comments.append((work_item_id, text))

    async def update_state(self, work_item_id, new_state):
        self.states.append((work_item_id, new_state))

    async def get_all_tagged_work_items(self):
        return self.tagged_items

    async def get_work_item(self, work_item_id):
        return WorkItemInfo(id=work_item_id, title="t", work_item_type="Task")


class _FakeExec:
    def __init__(self, dispatch=(True, "autopilot-7"), final=None):
        self._dispatch, self._final = dispatch, final
        self.released: list[str | None] = []

    async def dispatch_interactive(self, item, *, autonomy, draft_pr):
        launched, session = self._dispatch
        return launched, session, "/ws/scratch"

    def finalize_interactive(self, item, run_dir):
        return self._final

    async def release_scratch(self, run_dir):
        self.released.append(run_dir)

    async def prune_orphans(self):
        pass


class _FakeExecRepo:
    def __init__(self):
        self.completed: list[tuple[int, bool]] = []

    async def start_execution(self, item, skill, trigger_tag=None):
        return 99

    async def complete_execution(self, record_id, result):
        self.completed.append((record_id, result.success))


class _FakeCost:
    async def track(self, record_id, tokens):
        pass


class _FakeNotifier:
    def __init__(self):
        self.completed: list[bool] = []

    async def notify_completed(self, item, result):
        self.completed.append(result.success)


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


def _tagged(item_id, state, tags):
    return WorkItemInfo(id=item_id, title="t", work_item_type="Task", state=state, tags=tags)


async def test_reconcile_reopened_clears_skip_tags():
    p, c = _poller()  # trigger_states include New/To Do/Proposed/Active; state_in_progress=Active, resolved_state=Resolved
    done = c.config.processed_tag
    c.ado.tagged_items = [
        _tagged(7, "New", ["autopilot", done]),        # trigger, not an output state → reopen
        _tagged(8, "Resolved", ["autopilot", done]),   # output state → keep
        _tagged(9, "Active", ["autopilot", done]),     # trigger BUT = state_in_progress output → keep
        _tagged(10, "New", ["autopilot"]),             # no skip tag → nothing to do
    ]
    await p._reconcile_reopened()
    assert (7, done) in c.ado.removed
    assert (7, PipelineState.QUEUED) in c.state_repo.calls
    assert [wid for wid, _ in c.ado.removed] == [7]    # only the reopened one


async def test_reconcile_reopened_respects_toggle_off():
    p, c = _poller()
    c.config.reprocess_on_reopen = False
    c.ado.tagged_items = [_tagged(7, "New", ["autopilot", c.config.processed_tag])]
    await p._reconcile_reopened()
    assert c.ado.removed == []


def test_outcome_policy_maps_tag_and_state():
    cfg = Settings(
        review_tag="rv", processed_tag="done", escalation_tag="hold",
        state_in_progress="Active", state_in_review="InRev", resolved_state="Resolved",
        state_needs_human="Blocked", state_report="Reported", state_failed="Rejected",
        failed_tag="",
    )
    assert outcome_policy(cfg, "in_progress") == ("", "Active")     # no tag on start
    assert outcome_policy(cfg, "review") == ("rv", "InRev")
    assert outcome_policy(cfg, "done") == ("done", "Resolved")
    assert outcome_policy(cfg, "report") == ("done", "Reported")    # report reuses Done tag
    assert outcome_policy(cfg, "needs_human") == ("hold", "Blocked")
    assert outcome_policy(cfg, "failed") == ("done", "Rejected")    # blank failed_tag → Done tag


def test_outcome_policy_failed_tag_override():
    cfg = Settings(failed_tag="autopilot-failed", processed_tag="done")
    assert outcome_policy(cfg, "failed")[0] == "autopilot-failed"


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
    assert (7, c.config.processed_tag) in c.ado.tags  # Done outcome tags processed_tag
    assert c.notifier.completed == [True]


async def test_report_mode_marks_processed_without_pr():
    p, c = _poller(autonomy="report")
    res = ExecutionResult.ok(7, "agent", "planned")  # no pr_url
    await p._handle_agent_result(_item(), res)
    assert (7, c.config.processed_tag) in c.ado.tags


async def test_ado_state_resolved_on_done_with_pr():
    p, c = _poller(autonomy="unattended")            # non-draft → Done
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = "https://pr"
    await p._handle_agent_result(_item(), res)
    assert (7, "Resolved") in c.ado.states           # resolved_state (default)


async def test_ado_state_in_review_when_configured():
    p, c = _poller(autonomy="assisted")              # draft → In review
    c.config.state_in_review = "In Review"
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = "https://pr"
    await p._handle_agent_result(_item(), res)
    assert (7, "In Review") in c.ado.states


async def test_ado_state_unchanged_when_blank():
    p, c = _poller()
    c.config.state_needs_human = ""                  # blank (default) → no ADO write
    res = ExecutionResult.fail(7, "agent", "x")
    res.needs_human = True
    await p._handle_agent_result(_item(), res)
    assert c.ado.states == []


async def test_ado_state_skipped_in_dry_run():
    p, c = _poller(autonomy="unattended")
    c.config.dry_run = True
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = "https://pr"
    await p._handle_agent_result(_item(), res)
    assert c.ado.states == []                         # dry_run → no ADO state write


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
    assert p._live_dirs == {7: "/ws/scratch"}                 # run dir tracked for cleanup
    assert (7, PipelineState.IN_PROGRESS) in c.state_repo.calls
    assert any("Live session started" in t for _, t in c.ado.comments)


async def test_finalize_live_session_when_result_ready():
    p, c = _poller()
    done = ExecutionResult.ok(7, "agent", "done")
    done.pr_url = "https://pr"
    c.executor = _FakeExec(final=done)
    p._live = {7: 99}
    p._live_dirs = {7: "/ws/scratch"}
    await p._finalize_live_sessions()
    assert p._live == {}                                       # cleared after finalise
    assert p._live_dirs == {}                                  # run dir cleared
    assert c.executor.released == ["/ws/scratch"]              # scratch torn down
    assert c.execution_repo.completed == [(99, True)]
    assert (7, c.config.review_tag) in c.ado.tags              # went to In review


async def test_finalize_skips_while_session_running():
    p, c = _poller()
    c.executor = _FakeExec(final=None)                         # no result.json yet
    p._live = {7: 99}
    await p._finalize_live_sessions()
    assert p._live == {7: 99}                                  # still live, not finalised
