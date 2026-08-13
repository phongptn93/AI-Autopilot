"""Batched runs: one agent run over a linked cluster → one PR per work item.

Covers the three places a batch can go wrong: forming the cluster (scheduler),
splitting one agent result back into per-item outcomes (executor), and keeping the
per-item bookkeeping intact (poller).
"""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.data import PipelineState
from ai_autopilot.execution.auto_reviewer import AutoReviewer
from ai_autopilot.execution.claude_executor import ClaudeExecutor
from ai_autopilot.execution.result_contract import AgentResult, Artifact, batch_key
from ai_autopilot.models import ExecutionResult, TaskCategory, WorkItemInfo
from ai_autopilot.services.poller import AdoPollerService
from tests.test_poller_agent import (
    _FakeAdo,
    _FakeCost,
    _FakeExecRepo,
    _FakeNotifier,
    _FakeQuality,
    _FakeRetry,
    _FakeSdlcState,
    _FakeState,
)


def _wi(iid: int, title: str = "t", priority: int = 2) -> WorkItemInfo:
    item = WorkItemInfo(id=iid, title=title, work_item_type="Task", priority=priority)
    item.category = TaskCategory.BACKEND_TASK
    return item


def _executor(**over) -> ClaudeExecutor:
    cfg = Settings(**{"workspace_directory": r"C:\ws", **over})
    return ClaudeExecutor(cfg, AutoReviewer(cfg))


# ── splitting one result into per-item outcomes ───────────────────────────────


def _agent(*arts: Artifact, status="completed", **kw) -> AgentResult:
    return AgentResult(status=status, summary="did it", artifacts=list(arts), **kw)


def test_each_item_gets_its_own_pr():
    items = [_wi(101), _wi(102), _wi(103)]
    agent = _agent(
        Artifact(repo="r", branch="feature/be/101-a", pr_url="https://pr/1", work_item_id=101),
        Artifact(repo="r", branch="feature/be/102-b", pr_url="https://pr/2", work_item_id=102),
        Artifact(repo="r", branch="feature/be/103-c", pr_url="https://pr/3", work_item_id=103),
    )
    out = _executor()._results_from_batch(items, agent, "assisted")
    assert [out[i].success for i in (101, 102, 103)] == [True, True, True]
    assert out[102].pr_url == "https://pr/2"
    assert out[103].branch_name == "feature/be/103-c"


def test_an_item_without_a_pr_fails_even_when_the_batch_says_completed():
    # 2 of 3 landed: the third must stay FAILED (and retryable) on its own board row.
    items = [_wi(101), _wi(102), _wi(103)]
    agent = _agent(
        Artifact(repo="r", pr_url="https://pr/1", work_item_id=101),
        Artifact(repo="r", pr_url="https://pr/2", work_item_id=102),
    )
    out = _executor()._results_from_batch(items, agent, "assisted")
    assert out[101].success and out[102].success
    assert not out[103].success and "#103" in out[103].error


def test_artifacts_are_never_credited_to_the_wrong_item():
    items = [_wi(101), _wi(102)]
    agent = _agent(
        Artifact(repo="r", pr_url="https://pr/1", work_item_id=101),
        Artifact(repo="r", pr_url="https://pr/999", work_item_id=999),  # not in this batch
    )
    out = _executor()._results_from_batch(items, agent, "assisted")
    assert out[101].pr_url == "https://pr/1"
    assert not out[102].success and out[102].pr_url is None


def test_unattributed_artifact_is_accepted_for_a_single_item_batch():
    # No ambiguity with one item — an agent that omitted work_item_id still counts.
    out = _executor()._results_from_batch(
        [_wi(101)], _agent(Artifact(repo="r", pr_url="https://pr/1")), "assisted"
    )
    assert out[101].success and out[101].pr_url == "https://pr/1"


def test_needs_human_escalates_only_the_items_without_a_pr():
    items = [_wi(101), _wi(102)]
    agent = _agent(
        Artifact(repo="r", pr_url="https://pr/1", work_item_id=101),
        status="needs_human", needs_human=True, reason="cần quyền truy cập repo",
    )
    out = _executor()._results_from_batch(items, agent, "assisted")
    assert out[101].success and not out[101].needs_human      # its PR is already open
    assert out[102].needs_human and out[102].error == "cần quyền truy cập repo"


def test_missing_result_file_fails_every_member_with_the_agents_last_words():
    items = [_wi(101), _wi(102)]
    out = _executor()._results_from_batch(items, None, "assisted", run_text="chờ #99 xong đã")
    assert set(out) == {101, 102}
    assert all(not r.success for r in out.values())
    assert batch_key(101) in out[101].error and "#99" in out[101].error


def test_report_mode_completes_without_a_pr():
    out = _executor()._results_from_batch([_wi(101), _wi(102)], _agent(), "report")
    assert out[101].success and out[102].success


async def test_batch_preflight_failure_is_reported_on_every_item():
    ex = _executor(workspace_directory="")
    out = await ex.run_agent_batch([_wi(101), _wi(102)], autonomy="assisted", draft_pr=False)
    assert set(out) == {101, 102}
    assert all("workspace_directory" in r.error for r in out.values())


# ── the brief the batch runs on ───────────────────────────────────────────────


def test_batch_brief_demands_one_pr_per_item_and_the_id_on_every_artifact():
    brief = _executor()._build_batch_brief(
        [_wi(101), _wi(102)], ["Backend-Fresh"], autonomy="assisted", draft_pr=True
    )
    assert "ONE branch and ONE pull request PER WORK ITEM" in brief
    assert "work_item_id" in brief
    assert ".autopilot/runs/batch-101.json" in brief
    assert "#101" in brief and "#102" in brief
    assert "DRAFT" in brief


def test_batch_brief_stacks_or_forks_per_config():
    items, repos = [_wi(101), _wi(102)], ["Backend-Fresh"]
    stacked = _executor(batch_stacked_prs=True)._build_batch_brief(
        items, repos, autonomy="assisted", draft_pr=False
    )
    assert "STACKED branches" in stacked and "TARGETS that previous branch" in stacked

    forked = _executor(batch_stacked_prs=False)._build_batch_brief(
        items, repos, autonomy="assisted", draft_pr=False
    )
    assert "INDEPENDENT branches" in forked and "STACKED branches" not in forked


# ── forming the cluster ───────────────────────────────────────────────────────


class _Notifier(_FakeNotifier):
    def __init__(self):
        super().__init__()
        self.errors: list[tuple[int, str]] = []

    async def notify_error(self, item, error):
        self.errors.append((item.id, error))


class _FakeRouter:
    def classify(self, item):
        return item


class _FakePlugins:
    async def run_pre_processors(self, item):
        return item

    async def run_post_processors(self, item, result):
        pass


class _FakeRbac:
    def is_user_allowed(self, item):
        return True


def _poller(**cfg_over) -> tuple[AdoPollerService, SimpleNamespace]:
    cfg = Settings(**{
        "workspace_directory": r"C:\ws",
        "batch_related_enabled": True,
        "execution_mode": "headless",   # batching is a headless-only path
        **cfg_over,
    })

    async def mention_identity():
        return None

    c = SimpleNamespace(
        config=cfg, ado=_FakeAdo(), notifier=_Notifier(), retry_policy=_FakeRetry(),
        state_repo=_FakeState(), sdlc_state_repo=_FakeSdlcState(),
        mention_identity=mention_identity, execution_repo=_FakeExecRepo(),
        cost_tracker=_FakeCost(), quality_repo=_FakeQuality(),
        router=_FakeRouter(), plugins=_FakePlugins(), rbac=_FakeRbac(),
    )
    return AdoPollerService(c), c


def test_related_cluster_collapses_to_one_lead():
    p, _ = _poller()
    items = [_wi(101, priority=2), _wi(102, priority=1), _wi(103, priority=3), _wi(200)]
    related = {101: {102, 103}, 102: {101}, 103: {101}}
    out, _preds, out_rel = p._collapse_batches(items, {}, related)

    assert [i.id for i in out] == [102, 200]          # 102 leads (best priority); 200 untouched
    assert [i.id for i in p._batch_of[102]] == [102, 101, 103]
    assert out_rel[102] == set()                      # links INSIDE the cluster are not conflicts


def test_predecessor_chain_batches_in_dependency_order():
    p, _ = _poller()
    items = [_wi(101), _wi(102), _wi(103)]
    out, _preds, _rel = p._collapse_batches(items, {102: {101}, 103: {102}}, {})
    assert [i.id for i in out] == [101]
    assert [i.id for i in p._batch_of[101]] == [101, 102, 103]   # stacking order


def test_lead_inherits_links_to_the_outside_world():
    # A batch still waits for a predecessor that is not one of its members.
    p, _ = _poller()
    items = [_wi(101), _wi(102)]
    out_items, preds, related = p._collapse_batches(
        items, {102: {101, 500}}, {101: {102}, 102: {101, 600}}
    )
    lead = out_items[0].id
    assert 500 in preds[lead]
    assert related[lead] == {600}


def test_cluster_bigger_than_the_cap_is_left_alone():
    p, _ = _poller(batch_max_items=2)
    items = [_wi(101), _wi(102), _wi(103)]
    out, _p, _r = p._collapse_batches(items, {}, {101: {102, 103}, 102: {101}, 103: {101}})
    assert [i.id for i in out] == [101, 102, 103] and p._batch_of == {}


def test_batching_is_off_by_default_and_in_interactive_and_sdlc_modes():
    for over in ({"batch_related_enabled": False}, {"execution_mode": "interactive"},
                 {"sdlc_loop_enabled": True}):
        p, _ = _poller(**over)
        items = [_wi(101), _wi(102)]
        out, _p, _r = p._collapse_batches(items, {}, {101: {102}, 102: {101}})
        assert [i.id for i in out] == [101, 102] and p._batch_of == {}


# ── per-item bookkeeping around the shared run ────────────────────────────────


class _BatchExec:
    def __init__(self, results):
        self._results = results
        self.calls: list[list[int]] = []

    async def run_agent_batch(self, items, *, autonomy, draft_pr):
        self.calls.append([i.id for i in items])
        return self._results


async def test_batch_reports_each_item_separately():
    p, c = _poller()
    items = [_wi(101), _wi(102)]
    ok = ExecutionResult.ok(101, "agent-batch", "done")
    ok.pr_url = "https://pr/1"
    bad = ExecutionResult.fail(102, "agent-batch", "Batch opened no PR for #102")
    c.executor = _BatchExec({101: ok, 102: bad})

    await p._process_batch(items)

    assert c.executor.calls == [[101, 102]]                       # exactly one run
    assert [r for _, r in c.execution_repo.completed] == [True, False]
    assert (101, PipelineState.IN_PROGRESS) in c.state_repo.calls
    assert (102, PipelineState.FAILED) in c.state_repo.calls      # only 102 failed
    assert (102, "cần quyền") not in c.ado.comments


async def test_batch_skips_when_a_member_is_already_running():
    p, c = _poller()
    c.executor = _BatchExec({})
    p._inflight.add(102)
    await p._process_batch([_wi(101), _wi(102)])
    assert c.executor.calls == []          # no run started
    assert c.state_repo.calls == []        # and nothing was moved to IN_PROGRESS


async def test_dispatch_routes_a_lead_to_the_batch_and_others_to_the_single_path():
    p, c = _poller()
    single: list[int] = []

    async def _fake_process(item):
        single.append(item.id)

    p._process = _fake_process
    c.executor = _BatchExec({
        101: ExecutionResult.ok(101, "agent-batch", "x"),
        102: ExecutionResult.ok(102, "agent-batch", "x"),
    })
    p._batch_of = {101: [_wi(101), _wi(102)]}

    await p._dispatch(_wi(101))
    await p._dispatch(_wi(200))

    assert c.executor.calls == [[101, 102]]
    assert single == [200]
    assert p._batch_of == {}               # consumed, not left to leak into next cycle
