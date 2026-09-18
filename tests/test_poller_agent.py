"""Tests for the poller's AI-native result handling (_handle_agent_result)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from ai_autopilot.config import SdlcRole, Settings
from ai_autopilot.data import PipelineState
from ai_autopilot.execution.result_contract import CaseOutcome
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.outcomes import apply_outcome, outcome_policy
from ai_autopilot.services.poller import AdoPollerService


class _FakeAdo:
    def __init__(self):
        self.tags: list[tuple[int, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.states: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []
        self.tagged_items: list = []
        self.tagged_any_queries: list[list[str]] = []
        self.comments_by_item: dict[int, list[dict]] = {}
        self.reviewers: list[tuple[str, int, str, bool]] = []
        # States ADO should refuse (None = accept everything), to reproduce "this state
        # doesn't exist on that work-item type" without a live project.
        self.reject_state: set[str] | None = None

    async def add_tag(self, work_item_id, tag):
        self.tags.append((work_item_id, tag))

    async def remove_tag(self, work_item_id, tag):
        self.removed.append((work_item_id, tag))

    async def add_comment(self, work_item_id, text):
        self.comments.append((work_item_id, text))

    async def update_state(self, work_item_id, new_state):
        self.states.append((work_item_id, new_state))
        # Mirrors the real client's bool. apply_outcome now reads it: a state ADO
        # refuses (one that does not exist on the item's type) must not be reported as
        # a move, so a fake returning None would make every transition look refused.
        return self.reject_state is None or new_state not in self.reject_state

    async def get_all_tagged_work_items(self):
        return self.tagged_items

    async def get_work_items_tagged_any(self, tags):
        # Records what the run-now sweep asked for: the whole bug was that it asked the
        # wrong question (trigger tags) and so never saw the items it was meant to find.
        self.tagged_any_queries.append(list(tags))
        wanted = {(t or "").strip().lower() for t in tags}
        return [
            i for i in self.tagged_items
            if wanted & {(t or "").strip().lower() for t in i.tags}
        ]

    async def get_work_item(self, work_item_id):
        return WorkItemInfo(id=work_item_id, title="t", work_item_type="Task")

    async def get_work_item_comments(self, work_item_id):
        return self.comments_by_item.get(work_item_id, [])

    async def add_pull_request_reviewer(self, repo_id, pr_id, reviewer_id, *, required=False):
        self.reviewers.append((repo_id, pr_id, reviewer_id, required))
        return True


class _FakeExec:
    def __init__(self, dispatch=(True, "autopilot-7"), final=None,
                 quiet=None, last="reading OrderService.cs"):
        self._dispatch, self._final = dispatch, final
        # How long the live session has been silent. None = "no session recorded here",
        # which is what a fake with no opinion should say — the watchdog must then do
        # nothing at all rather than assume the worst about a run it cannot see.
        self._quiet, self._last = quiet, last
        self.released: list[str | None] = []
        self.closed: list[tuple[int, str | None]] = []

    async def dispatch_interactive(self, item, *, autonomy, draft_pr, stages=None,
                                   opens_pr=True):
        launched, session = self._dispatch
        self.briefed_stages = [st.name for st in stages] if stages else None
        # Whether the role this run is for opens a PR at all — a QC role's stages say no.
        self.briefed_opens_pr = opens_pr
        return launched, session, "/ws/scratch"

    def finalize_interactive(self, item, run_dir):
        return self._final

    def interactive_scratch_dir(self, item_id):
        return f"/ws/agent-{item_id}"

    def interactive_quiet_seconds(self, run_dir, item_id):
        return self._quiet

    def interactive_last_activity(self, run_dir):
        return self._last, self._quiet

    async def release_scratch(self, run_dir):
        self.released.append(run_dir)

    async def close_interactive(self, run_dir, item_id):
        self.closed.append((item_id, run_dir))
        return True

    async def prune_orphans(self):
        pass


class _FakeExecRepo:
    def __init__(self):
        self.completed: list[tuple[int, bool]] = []
        self.results: list = []
        self.retries: list[tuple[int, int]] = []
        self.profiles: list[str] = []
        self.started: list[tuple[int, str, object]] = []

    async def start_execution(self, item, skill, trigger_tag=None, profile="",
                              started_at=None):
        # `profile` is which ROLE the run is. Recorded because "interactive:<session>"
        # names the console, not the work — see the In-flight page.
        self.profiles.append(profile)
        # `started_at` is back-dated for a run that began before this row did (an
        # interactive session recovered after a restart), so it is worth capturing.
        self.started.append((item.id, skill, started_at))
        return 99

    async def complete_execution(self, record_id, result):
        self.completed.append((record_id, result.success))
        self.results.append(result)

    async def mark_retrying(self, work_item_id, retry_count):
        self.retries.append((work_item_id, retry_count))


class _FakeCost:
    async def track(self, record_id, tokens):
        pass


class _FakeNotifier:
    def __init__(self):
        self.completed: list[bool] = []
        self.started: list[tuple[int, str, bool]] = []   # (item id, skill, posted a comment)

    async def notify_started(self, item, skill, *, post_comment=True):
        self.started.append((item.id, skill, post_comment))

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


class _FakeQuality:
    """Captures the append-only quality events the poller records."""

    def __init__(self):
        self.events: list[dict] = []

    async def record(self, **kw):
        self.events.append(kw)

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.events]


class _FakeState:
    def __init__(self):
        self.calls: list[tuple[int, object]] = []

    async def set(self, work_item_id, state, *, title="", detail=None, pr_url=None):
        self.calls.append((work_item_id, state))


class _FakeSdlcState:
    def __init__(self):
        self.cleared: list[int] = []

    async def clear(self, work_item_id):
        self.cleared.append(work_item_id)


def _poller(
    autonomy="assisted", exhausted=False, bot=None, **cfg_over
) -> tuple[AdoPollerService, SimpleNamespace]:
    cfg = Settings(workspace_directory=r"C:\ws", autonomy_level=autonomy, **cfg_over)

    async def mention_identity():
        # The real Container resolves this from ADO; None means "@mentions off", which is
        # what every test that isn't about mentions wants.
        return bot

    c = SimpleNamespace(
        config=cfg, ado=_FakeAdo(), notifier=_FakeNotifier(),
        retry_policy=_FakeRetry(exhausted), state_repo=_FakeState(),
        sdlc_state_repo=_FakeSdlcState(), mention_identity=mention_identity,
        executor=_FakeExec(), execution_repo=_FakeExecRepo(), cost_tracker=_FakeCost(),
        quality_repo=_FakeQuality(),
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


async def test_a_hand_off_state_is_never_a_reopen_signal():
    """Moving a finished bug to "Ready for Testing" hands it to QC, not back to the bot.

    Regression for a real incident: adding that state to trigger_states made the
    poller strip autopilot-done from every finished item already parked there and
    rework the lot (#8526, #8107) — then take each one again as fast as a person
    could put it back, because every restore looked like one more reopen.
    """
    p, c = _poller()
    done = c.config.processed_tag
    c.config.trigger_states = [*c.config.trigger_states, "Ready for Testing", "Ready for UAT"]
    c.config.board_testing_state = ["Ready for Testing", "In Testing"]
    c.config.done_states = ["Ready for UAT"]
    c.ado.tagged_items = [
        _tagged(7, "New", ["autopilot", done]),                 # a real reopen
        _tagged(8, "Ready for Testing", ["autopilot", done]),   # QC has it → keep
        _tagged(9, "Ready for UAT", ["autopilot", done]),       # a Done state → keep
    ]
    await p._reconcile_reopened()
    assert [wid for wid, _ in c.ado.removed] == [7]


async def test_reconcile_reopened_respects_toggle_off():
    p, c = _poller()
    c.config.reprocess_on_reopen = False
    c.ado.tagged_items = [_tagged(7, "New", ["autopilot", c.config.processed_tag])]
    await p._reconcile_reopened()
    assert c.ado.removed == []


async def test_restart_wipes_sdlc_and_dispatches_from_any_state():
    p, c = _poller()
    restart, done = c.config.restart_tag, c.config.processed_tag
    c.ado.tagged_items = [
        _tagged(7, "Resolved", ["autopilot", done, restart]),  # restart from a DONE state
        _tagged(8, "Active", ["autopilot"]),                    # no restart tag → ignored
    ]
    dispatched: list[int] = []

    async def _fake_process(item):
        dispatched.append(item.id)

    p._process = _fake_process
    await p._reconcile_restart_requests()
    await asyncio.sleep(0)  # let the create_task run

    assert c.sdlc_state_repo.cleared == [7]            # progress wiped → true restart
    assert (7, restart) in c.ado.removed               # restart signal consumed
    assert (7, done) in c.ado.removed                  # skip tag cleared
    assert (7, PipelineState.QUEUED) in c.state_repo.calls
    assert dispatched == [7]                           # dispatched even from Resolved
    assert 8 not in c.sdlc_state_repo.cleared


async def test_restart_noop_when_tag_blank():
    p, c = _poller()
    c.config.restart_tag = ""
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot", "autopilot-restart"])]
    await p._reconcile_restart_requests()
    assert c.ado.removed == []
    assert c.sdlc_state_repo.cleared == []


async def test_restart_skips_live_session():
    p, c = _poller()
    p._live[7] = 99  # an in-flight interactive session
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot", c.config.restart_tag])]
    await p._reconcile_restart_requests()
    assert c.ado.removed == []
    assert c.sdlc_state_repo.cleared == []


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


# ── Reviewers on the PR the autopilot opens ──────────────────────────────────

_PR_URL = "https://dev.azure.com/nois/DxFactory/_git/Backend-Fresh/pullrequest/2470"


def _assigned_item() -> WorkItemInfo:
    return WorkItemInfo(
        id=7, title="t", work_item_type="Task",
        assigned_to="Que Phan", assigned_to_email="que.phan@nois.vn",
        assigned_to_id="11111111-2222-3333-4444-555555555555",
    )


async def test_assignee_is_added_as_reviewer_on_a_draft_pr():
    """A draft PR is exactly when someone has to be told to look — so the reviewer is
    added there too, not only on unattended PRs."""
    p, c = _poller(autonomy="assisted", pr_add_assignee_as_reviewer=True)
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = _PR_URL
    await p._handle_agent_result(_assigned_item(), res)
    # Repo NAME from the url is a valid repositoryId for the git REST endpoints.
    assert c.ado.reviewers == [
        ("Backend-Fresh", 2470, "11111111-2222-3333-4444-555555555555", False)
    ]


async def test_extra_reviewers_are_added_and_never_duplicated():
    p, c = _poller(
        pr_add_assignee_as_reviewer=True,
        pr_extra_reviewer_ids=["11111111-2222-3333-4444-555555555555", "  ", "aaaa-bbbb"],
        pr_reviewers_required=True,
    )
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = _PR_URL
    await p._handle_agent_result(_assigned_item(), res)
    assert [r[2] for r in c.ado.reviewers] == [
        "11111111-2222-3333-4444-555555555555", "aaaa-bbbb"   # assignee listed twice → once
    ]
    assert all(r[3] is True for r in c.ado.reviewers)          # required


async def test_every_pr_of_a_multi_repo_run_gets_the_reviewer():
    p, c = _poller(pr_add_assignee_as_reviewer=True)
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = _PR_URL
    res.pr_urls = [
        _PR_URL,                                                          # same one → once
        "https://dev.azure.com/nois/DxFactory/_git/Micro-Frontend/pullrequest/2471",
    ]
    await p._handle_agent_result(_assigned_item(), res)
    assert [(r[0], r[1]) for r in c.ado.reviewers] == [
        ("Backend-Fresh", 2470), ("Micro-Frontend", 2471)
    ]


async def test_no_reviewer_added_when_the_feature_is_off_or_nobody_is_assigned():
    p, c = _poller()                                   # default: feature off
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = _PR_URL
    await p._handle_agent_result(_assigned_item(), res)
    assert c.ado.reviewers == []

    p, c = _poller(pr_add_assignee_as_reviewer=True)   # on, but the item has no assignee
    await p._handle_agent_result(_item(), res)
    assert c.ado.reviewers == []


async def test_an_unparseable_pr_url_is_survived_not_raised():
    """The PR is already open; a url we can't address must not fail the run."""
    p, c = _poller(pr_add_assignee_as_reviewer=True)
    res = ExecutionResult.ok(7, "agent", "done")
    res.pr_url = "https://example.invalid/whatever"
    await p._handle_agent_result(_assigned_item(), res)
    assert c.ado.reviewers == []
    assert (7, c.config.review_tag) in c.ado.tags      # the run still completed normally


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
    assert (7, c.config.live_tag) in c.ado.tags               # live tag → no re-dispatch on restart
    assert (7, PipelineState.IN_PROGRESS) in c.state_repo.calls
    assert any("Live session started" in t for _, t in c.ado.comments)


async def test_an_interactive_dispatch_broadcasts_the_start():
    """Only this execution mode skipped notify_started, so on an interactive machine Teams
    got a "completed" card with no "started" card before it."""
    p, c = _poller()
    c.executor = _FakeExec(dispatch=(True, "autopilot-7"))
    await p._dispatch_interactive(_item())
    assert c.notifier.started == [(7, "interactive:autopilot-7", False)]
    # post_comment=False: the richer "Live session started" comment is already there, and a
    # second generic one would be duplicate noise on the work item.
    assert sum("Đã nhận việc" in t for _, t in c.ado.comments) == 0
    assert sum("Live session started" in t for _, t in c.ado.comments) == 1


async def test_a_failed_launch_does_not_announce_a_start():
    p, c = _poller()
    c.executor = _FakeExec(dispatch=(False, None))
    await p._dispatch_interactive(_item())
    assert c.notifier.started == []


async def test_orphan_interactive_session_finalized_after_restart():
    p, c = _poller()  # assisted → draft PR → review outcome
    done = ExecutionResult.ok(7, "agent", "done")
    done.pr_url = "https://pr"
    c.executor = _FakeExec(final=done)
    # tagged live but NOT tracked in _live (in-memory state lost on restart)
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot", c.config.live_tag])]
    await p._finalize_live_sessions()
    assert (7, c.config.live_tag) in c.ado.removed            # live tag cleared
    assert (7, c.config.review_tag) in c.ado.tags             # outcome applied


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
    assert c.execution_repo.completed == [(99, True)]
    assert (7, c.config.review_tag) in c.ado.tags              # went to In review
    # Default policy keeps console + worktree until the PR closes, so review
    # feedback can be reworked in the SAME session (PR monitor closes them).
    assert c.executor.closed == []
    assert c.executor.released == []


async def test_finalize_closes_session_when_policy_is_result():
    p, c = _poller()
    c.config.interactive_close_on = "result"
    done = ExecutionResult.ok(7, "agent", "done")
    done.pr_url = "https://pr"
    c.executor = _FakeExec(final=done)
    p._live = {7: 99}
    p._live_dirs = {7: "/ws/scratch"}
    await p._finalize_live_sessions()
    assert c.executor.closed == [(7, "/ws/scratch")]           # console shut
    assert c.executor.released == ["/ws/scratch"]              # scratch torn down


async def test_finalize_skips_while_session_running():
    p, c = _poller()
    c.executor = _FakeExec(final=None)                         # no result.json yet
    p._live = {7: 99}
    await p._finalize_live_sessions()
    assert p._live == {7: 99}                                  # still live, not finalised


# ── Watchdog: the ceiling an interactive session never had ─────────────────────
#
# A headless run has always had task_timeout_minutes. An interactive one had nothing:
# the ONLY thing that finalised it was its own result file, which a wedged session
# never writes — so a hung MCP call or a console that died with its parent held the
# item's live tag and its worktree forever, with the board still showing it as being
# worked on and nobody told. These tests are about that hole, so each one states the
# behaviour that closes it rather than just exercising the code.

async def _quiet_session(quiet, **cfg_over):
    """A poller with one live session that has been silent for ``quiet`` seconds."""
    p, c = _poller(**cfg_over)
    c.executor = _FakeExec(final=None, quiet=quiet, last="mcp__ado__repo_pull_request")
    p._live = {7: 99}
    p._live_dirs = {7: "/ws/scratch"}
    await p._finalize_live_sessions()
    return p, c


async def test_a_session_silent_past_the_ceiling_is_closed_and_the_item_released():
    """The whole point: silence forever used to mean held forever."""
    p, c = await _quiet_session(quiet=61 * 60)                 # default ceiling is 60m
    assert c.executor.closed == [(7, "/ws/scratch")]           # console shut
    assert p._live == {}                                       # item no longer held
    assert (7, c.config.live_tag) in c.ado.removed             # live tag cleared
    assert c.execution_repo.completed == [(99, False)]         # recorded as a FAILURE


async def test_giving_up_keeps_the_worktree_so_run_resumes_instead_of_restarting():
    """What this knows is that the session stopped talking — not that its work was
    worthless. The branch, the build state and the conversation are all still in that
    worktree, so releasing it would turn a stalled run into a repeat of the whole run."""
    _, c = await _quiet_session(quiet=61 * 60)
    assert c.executor.released == []


async def test_the_reason_names_the_ceiling_and_what_the_session_last_did():
    """"Failed" with no reason sends the reader to the wrong place. The three things
    they need are which setting ended it, where it stopped, and that the work was
    kept — otherwise the obvious move is to restart the item from scratch."""
    _, c = await _quiet_session(quiet=61 * 60)
    reason = c.execution_repo.results[-1].error
    assert "interactive_idle_timeout_minutes" in reason
    assert "mcp__ado__repo_pull_request" in reason             # where it actually stopped
    assert "61 minutes" in reason
    assert "resumes" in reason


async def test_giving_up_goes_through_the_normal_failure_policy():
    """Not a special case: a session that stalled gets the retry budget, the
    escalation and the notifications every other failure gets. A watchdog that only
    logged would leave the item sitting in its working state with nobody told."""
    _, c = await _quiet_session(quiet=61 * 60)
    assert c.execution_repo.retries == [(7, 1)]                # first of max_retries


async def test_a_session_inside_the_ceiling_is_left_alone():
    """Long work is not a symptom. Killing a session that is merely slow would be
    strictly worse than the hole this closes."""
    p, c = await _quiet_session(quiet=59 * 60)
    assert p._live == {7: 99} and c.executor.closed == []


async def test_a_session_with_no_transcript_at_all_is_not_judged():
    """Unknown is not the same as silent. A run this side cannot see must not be
    killed on a guess — the orphan sweep already reports it."""
    p, c = await _quiet_session(quiet=None)
    assert p._live == {7: 99} and c.executor.closed == []


async def test_the_watchdog_can_be_switched_off():
    p, c = await _quiet_session(quiet=99 * 3600, interactive_idle_timeout_minutes=0)
    assert p._live == {7: 99} and c.executor.closed == []


async def test_a_quiet_session_is_warned_about_once_not_every_poll():
    """The watchdog runs every poll, so a stuck session would otherwise repeat the
    same warning until the ceiling — burying the log exactly when it is being read."""
    p, c = _poller()
    c.executor = _FakeExec(final=None, quiet=10 * 60)          # quiet, but under the ceiling
    p._live = {7: 99}
    p._live_dirs = {7: "/ws/scratch"}
    await p._finalize_live_sessions()
    assert p._quiet_warned == {7}
    await p._finalize_live_sessions()
    assert p._quiet_warned == {7}                              # not re-added, not re-warned


async def test_a_session_that_starts_talking_again_can_be_warned_about_again():
    p, c = _poller()
    c.executor = _FakeExec(final=None, quiet=10 * 60)
    p._live, p._live_dirs = {7: 99}, {7: "/ws/scratch"}
    await p._finalize_live_sessions()
    assert p._quiet_warned == {7}
    c.executor._quiet = 5                                      # it woke up
    await p._finalize_live_sessions()
    assert p._quiet_warned == set()


# ── /ai command loop (steer the autopilot with /ai … comments) ──────────────────

def _cmt(cid, text, *, is_bot, email="user@x"):
    return {
        "id": cid, "text": text, "is_bot": is_bot,
        "created_by": "user", "created_by_email": email,
    }


def test_is_bot_comment_detects_signature_not_author():
    from ai_autopilot.ado.client import is_bot_comment
    from ai_autopilot.config import BOT_COMMENT_PREFIX

    assert is_bot_comment(BOT_COMMENT_PREFIX + "all done") is True      # bot's own comment
    # ADO stores the 🤖 emoji HTML-encoded — detection must unescape and still match.
    assert is_bot_comment("done &#129302; more") is True
    assert is_bot_comment("please also handle Y") is False              # human comment
    assert is_bot_comment(None) is False


def test_match_command_recognises_prefixes_and_keeps_intent():
    from ai_autopilot.config import match_command

    cmds = ["/ai", "/review", "dxfactory@nois.vn"]
    addr = "dxfactory@nois.vn dùng size 2048"
    assert match_command("/ai fix the null check", cmds) == "/ai fix the null check"
    assert match_command("<div>/review this endpoint</div>", cmds) == "/review this endpoint"
    assert match_command(addr, cmds) == addr                     # address the account directly
    assert match_command("just a normal comment", cmds) is None
    assert match_command(None, cmds) is None


async def test_ai_command_dispatches_and_injects_guidance():
    p, c = _poller()
    c.config.trigger_tag = "autopilot"                 # so the tagged item is "owned"
    done = c.config.processed_tag
    item = _tagged(7, "Resolved", ["autopilot", done])  # a finished item
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(1, "PR opened", is_bot=True),
        _cmt(2, "/ai also handle Y", is_bot=False),     # /ai command → act
    ]}
    p._comment_seen[7] = 1
    dispatched: list[int] = []

    async def _fake_process(it):
        dispatched.append(it.id)

    p._process = _fake_process
    await p._reconcile_human_replies()
    await asyncio.sleep(0)                             # let the create_task run

    assert (7, done) in c.ado.removed                 # skip tag cleared (still owned via trigger)
    assert (7, PipelineState.QUEUED) in c.state_repo.calls
    assert item.pending_comment == "/ai also handle Y"     # full command fed to the brief
    assert dispatched == [7]


async def test_ai_command_ignores_non_command_comments():
    p, c = _poller()
    c.config.trigger_tag = "autopilot"
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot", c.config.review_tag])]
    c.ado.comments_by_item = {7: [
        _cmt(5, "bot", is_bot=True),
        _cmt(6, "please also handle Y", is_bot=False),   # a plain comment, NOT a /command
    ]}
    p._comment_seen[7] = 5
    await p._reconcile_human_replies()
    assert c.ado.removed == []                            # no /ai → nothing happens


async def test_ai_command_feeds_all_unhandled_and_blocks_double_dispatch():
    p, c = _poller()
    c.config.trigger_tag = "autopilot"
    item = _tagged(7, "Active", ["autopilot", c.config.review_tag])
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(1, "/ai handled earlier", is_bot=False),
        _cmt(2, "/ai do X", is_bot=False),
        _cmt(3, "/ai also do Y", is_bot=False),   # 2 and 3 are new since baseline
    ]}
    p._comment_seen[7] = 1

    async def _fake_process(it):
        pass

    p._process = _fake_process
    await p._reconcile_human_replies()

    assert item.pending_comment == "/ai do X\n\n/ai also do Y"   # both fed, oldest→newest
    assert 7 in p._processed                                     # blocks pending double-dispatch


async def test_ai_command_durable_baseline_from_last_bot_comment():
    p, c = _poller()
    c.config.trigger_tag = "autopilot"
    item = _tagged(7, "Active", ["autopilot", c.config.escalation_tag])  # held / needs_human
    c.ado.tagged_items = [item]
    # Fresh session (after restart): no _comment_seen. Bot's last action = #5; the /ai at
    # #6 came AFTER it → picked up despite the restart; the older #3 is not.
    c.ado.comments_by_item = {7: [
        _cmt(3, "/ai old, already answered", is_bot=False),
        _cmt(5, "bot escalation", is_bot=True),
        _cmt(6, "/ai here is the missing info", is_bot=False),
    ]}
    dispatched: list[int] = []

    async def _fake_process(it):
        dispatched.append(it.id)

    p._process = _fake_process
    await p._reconcile_human_replies()
    await asyncio.sleep(0)
    assert item.pending_comment == "/ai here is the missing info"   # only the post-bot command
    assert dispatched == [7]
    assert (7, c.config.escalation_tag) in c.ado.removed           # un-held (needs_human resumed)


async def test_ai_command_only_from_this_machines_user():
    p, c = _poller()
    c.config.trigger_tag = "autopilot"
    c.config.auto_transition_assignee = "phong.pham@nois.vn"     # this machine acts for Phong
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot", c.config.review_tag])]
    c.ado.comments_by_item = {7: [
        _cmt(5, "bot", is_bot=True),
        _cmt(6, "/ai đổi size", is_bot=False, email="someone.else@nois.vn"),  # not my user
    ]}
    p._comment_seen[7] = 5
    await p._reconcile_human_replies()
    assert c.ado.removed == []                            # another person's /ai → ignored here


async def test_ai_command_matches_this_machines_user():
    p, c = _poller()
    c.config.trigger_tag = "autopilot"
    c.config.auto_transition_assignee = "phong.pham@nois.vn"
    item = _tagged(7, "Active", ["autopilot", c.config.review_tag])
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(5, "bot", is_bot=True),
        _cmt(6, "/ai đổi size", is_bot=False, email="phong.pham@nois.vn"),
    ]}
    p._comment_seen[7] = 5

    async def _fake_process(it):
        pass

    p._process = _fake_process
    await p._reconcile_human_replies()
    assert item.pending_comment == "/ai đổi size"


async def test_ai_command_caps_and_notifies_once():
    p, c = _poller()
    c.config.trigger_tag = "autopilot"
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot", c.config.review_tag])]
    c.ado.comments_by_item = {7: [_cmt(5, "bot", is_bot=True), _cmt(9, "/ai again", is_bot=False)]}
    p._comment_seen[7] = 5
    p._comment_rounds[7] = c.config.max_comment_rounds     # already at the cap
    await p._reconcile_human_replies()
    assert 7 in p._comment_capped
    assert any("vòng" in t for _, t in c.ado.comments)     # told the human to use restart tag
    assert c.ado.removed == []                             # capped → not reprocessed


async def test_ai_command_defers_when_item_in_flight():
    p, c = _poller()
    c.config.trigger_tag = "autopilot"
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot"])]
    c.ado.comments_by_item = {7: [_cmt(9, "/ai new info mid-run", is_bot=False)]}
    p._comment_seen[7] = 1
    p._inflight.add(7)                                 # a run is currently in flight
    await p._reconcile_human_replies()
    assert c.ado.removed == []                             # the running item is not interrupted
    assert p._pending_comment[7] == "/ai new info mid-run"  # queued for after the run finishes


async def test_ai_command_respects_toggle_off():
    p, c = _poller()
    c.config.trigger_tag = "autopilot"
    c.config.comment_reprocess_enabled = False
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot"])]
    c.ado.comments_by_item = {7: [_cmt(9, "/ai x", is_bot=False)]}
    await p._reconcile_human_replies()
    assert c.ado.removed == []                          # returned early, nothing touched


# ── @mention on a WORK ITEM (parity with the PR path) ─────────────────────────

_BOT_GUID = "11111111-2222-3333-4444-555555555555"


def _mention_html(text: str, guid: str = _BOT_GUID, label: str = "AI Autopilot") -> str:
    """A comment as ADO stores it when someone @mentions the bot."""
    return (
        f'<div><a href="#" data-vss-mention="version:2.0,{guid}">@{label}</a> {text}</div>'
    )


async def test_at_mention_on_a_work_item_is_handled():
    """The asymmetry this closes: the same @mention that works on a pull request did
    nothing on a work item, because this path only matched a LEADING /command."""
    from ai_autopilot.config import BotIdentity

    p, c = _poller(bot=BotIdentity(identity_id=_BOT_GUID, display_name="AI Autopilot"))
    c.config.trigger_tag = "autopilot"
    c.config.auto_transition_assignee = "phong.pham@nois.vn"
    item = _tagged(7, "Active", ["autopilot", c.config.review_tag])
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(5, "bot", is_bot=True),
        _cmt(6, _mention_html("xem lại chỗ này"), is_bot=False,
             email="phong.pham@nois.vn"),
    ]}
    p._comment_seen[7] = 5

    async def _fake_process(it):
        pass

    p._process = _fake_process
    # The mention carries no command, so one is inferred — stub it so the test doesn't
    # depend on a model call, and assert the inferred command reaches the brief.
    import ai_autopilot.services.poller as poller_mod

    async def _fake_resolve(cfg, cmd):
        cmd["instruction"] = f"/review {cmd['instruction']}"
        return True

    original, poller_mod.resolve_command = poller_mod.resolve_command, _fake_resolve
    try:
        await p._reconcile_human_replies()
    finally:
        poller_mod.resolve_command = original

    assert item.pending_comment is not None
    assert "xem lại chỗ này" in item.pending_comment
    assert item.pending_comment.startswith("/review")   # inferred, advisory by default


async def test_a_mention_of_someone_else_is_ignored():
    """Tagging a colleague on the item must not wake the autopilot."""
    from ai_autopilot.config import BotIdentity

    p, c = _poller(bot=BotIdentity(identity_id=_BOT_GUID, display_name="AI Autopilot"))
    c.config.trigger_tag = "autopilot"
    c.config.auto_transition_assignee = "phong.pham@nois.vn"
    item = _tagged(7, "Active", ["autopilot"])
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(6, _mention_html("giúp mình với", guid="99999999-8888-7777-6666-555555555555",
                              label="Someone Else"),
             is_bot=False, email="phong.pham@nois.vn"),
    ]}
    await p._reconcile_human_replies()
    assert item.pending_comment is None


async def test_mentions_are_off_when_the_shared_switch_is_off():
    """comment_mention_enabled gates the PR path and this one together — the container
    returns no identity, so a mention simply isn't a trigger."""
    p, c = _poller(bot=None)          # mention_identity() → None
    c.config.trigger_tag = "autopilot"
    c.config.auto_transition_assignee = "phong.pham@nois.vn"
    item = _tagged(7, "Active", ["autopilot"])
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(6, _mention_html("xem lại"), is_bot=False, email="phong.pham@nois.vn"),
    ]}
    await p._reconcile_human_replies()
    assert item.pending_comment is None


async def test_a_slash_command_still_wins_over_mention_inference():
    """A named command must be taken literally — no inference, no advisory downgrade."""
    from ai_autopilot.config import BotIdentity

    p, c = _poller(bot=BotIdentity(identity_id=_BOT_GUID, display_name="AI Autopilot"))
    c.config.trigger_tag = "autopilot"
    c.config.auto_transition_assignee = "phong.pham@nois.vn"
    item = _tagged(7, "Active", ["autopilot"])
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(6, "/ai sửa null check", is_bot=False, email="phong.pham@nois.vn"),
    ]}

    async def _fake_process(it):
        pass

    p._process = _fake_process
    import ai_autopilot.services.poller as poller_mod

    called = []

    async def _spy(cfg, cmd):
        called.append(cmd)
        return True

    original, poller_mod.resolve_command = poller_mod.resolve_command, _spy
    try:
        await p._reconcile_human_replies()
    finally:
        poller_mod.resolve_command = original

    assert item.pending_comment == "/ai sửa null check"
    assert called == []          # inference is only for bare mentions


async def test_a_listed_teammate_can_command_this_machine():
    """The gate that refused a colleague's /ai on a shared PR."""
    p, c = _poller(command_users=["que.phan@nois.vn"])
    c.config.trigger_tag = "autopilot"
    c.config.auto_transition_assignee = "phong.pham@nois.vn"
    item = _tagged(7, "Active", ["autopilot"])
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(6, "/ai sửa null check", is_bot=False, email="que.phan@nois.vn"),
    ]}

    async def _fake_process(it):
        pass

    p._process = _fake_process
    await p._reconcile_human_replies()
    assert item.pending_comment == "/ai sửa null check"


async def test_commands_from_anyone_accepts_a_stranger_but_not_their_work_items():
    p, c = _poller(commands_from_anyone=True)
    c.config.trigger_tag = "autopilot"
    c.config.assignee_trigger_tag = "ai-autopilot"
    c.config.auto_transition_assignee = "phong.pham@nois.vn"
    item = _tagged(7, "Active", ["autopilot"])
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(6, "/ai sửa null check", is_bot=False, email="stranger@elsewhere.vn"),
    ]}

    async def _fake_process(it):
        pass

    p._process = _fake_process
    await p._reconcile_human_replies()
    assert item.pending_comment == "/ai sửa null check"
    # Ownership untouched: an item carrying only the SHARED tag, assigned to someone
    # else, is still not ours to run.
    others = _tagged(8, "Active", ["ai-autopilot"])
    others.assigned_to_email = "stranger@elsewhere.vn"
    assert p._owns_item(others) is False


async def test_someone_not_on_the_roster_is_still_ignored():
    p, c = _poller(command_users=["que.phan@nois.vn"])
    c.config.trigger_tag = "autopilot"
    c.config.auto_transition_assignee = "phong.pham@nois.vn"
    item = _tagged(7, "Active", ["autopilot"])
    c.ado.tagged_items = [item]
    c.ado.comments_by_item = {7: [
        _cmt(6, "/ai đổi hết đi", is_bot=False, email="stranger@elsewhere.vn"),
    ]}
    await p._reconcile_human_replies()
    assert item.pending_comment is None


class _Recorder:
    """Captures a structlog-shaped error call, which is the whole point of the change:
    a refused state used to leave nothing behind at all."""

    def __init__(self):
        self.errors: list[tuple[str, dict]] = []

    def error(self, event, **kw):
        self.errors.append((event, kw))


async def test_a_refused_outcome_state_is_reported_and_the_item_keeps_its_state():
    """The defect: apply_outcome discarded update_state's result. ADO refuses a state
    that does not exist on the item's TYPE — exactly what flows.py exists to prevent —
    and the item was then tagged done while its card never left its old column."""
    cfg = Settings(dry_run=False, processed_tag="done", resolved_state="Resolved")
    ado, log = _FakeAdo(), _Recorder()
    ado.reject_state = {"Resolved"}
    moved = await apply_outcome(ado, cfg, 8962, "done", "Requirement", log=log)
    assert moved is False
    assert log.errors and log.errors[0][1]["state"] == "Resolved"
    assert log.errors[0][1]["type"] == "Requirement"
    # Still tagged: the tag is the skip tag, and dropping it would re-run finished work
    # and open a second PR — a far more expensive way to be wrong than a stale tag.
    assert (8962, "done") in ado.tags


async def test_an_accepted_outcome_state_reports_the_move():
    cfg = Settings(dry_run=False, processed_tag="done", resolved_state="Resolved")
    ado, log = _FakeAdo(), _Recorder()
    assert await apply_outcome(ado, cfg, 7, "done", "Bug", log=log) is True
    assert (7, "Resolved") in ado.states
    assert log.errors == []


async def test_the_outcome_state_is_written_before_the_tag():
    """Order is the fix: the tag is what a board lane claims, so it must never be
    applied on the strength of a state write that has not happened yet."""
    cfg = Settings(dry_run=False, processed_tag="done", resolved_state="Resolved")
    ado, seen = _FakeAdo(), []
    orig_state, orig_tag = ado.update_state, ado.add_tag

    async def state(wid, s):
        seen.append("state")
        return await orig_state(wid, s)

    async def tag(wid, t):
        seen.append("tag")
        return await orig_tag(wid, t)

    ado.update_state, ado.add_tag = state, tag
    await apply_outcome(ado, cfg, 7, "done", "Bug")
    assert seen == ["state", "tag"]


async def test_an_interactive_session_is_briefed_on_one_role_only():
    """SDLC mode is headless and pre-empts interactive, so getting per-role runs used
    to cost the Remote-Control session a human steers. The role's stages go in the
    brief instead: the session survives, and it still runs only that role's work."""
    p, c = _poller()
    from ai_autopilot.config import SdlcStageWiring
    c.config.sdlc_stage_wiring = {
        "test": SdlcStageWiring(queue_state="Ready for Testing", working_state="In Testing"),
    }
    item = WorkItemInfo(id=7, title="t", work_item_type="Bug",
                        state="Ready for Testing", tags=["autopilot"])
    await p._dispatch_interactive(item)
    assert c.executor.briefed_stages == ["test"]
    # …and the board can say WHO is holding it, not just that something is running.
    assert (7, "In Testing") in c.ado.states


async def test_an_unwired_machine_still_gets_the_whole_item_brief():
    p, c = _poller()
    item = WorkItemInfo(id=7, title="t", work_item_type="Bug",
                        state="Ready for Testing", tags=["autopilot"])
    await p._dispatch_interactive(item)
    assert c.executor.briefed_stages is None


async def test_interactive_mode_is_not_taken_away_by_turning_the_relay_on():
    """Execution MODE says who does the work; the relay says WHICH work.

    They used to be one switch: the engine is headless and pre-empted interactive, so
    enabling the relay to get per-role runs silently removed the Remote-Control
    session the team steers. The session carries the role's stages in its brief now,
    so it answers both — and an item briefed for `full` runs the same stages the
    headless engine would have.
    """
    p, c = _poller()
    c.config.sdlc_loop_enabled = True
    c.config.execution_mode = "interactive"
    c.config.sdlc_default_profile = "full"
    item = WorkItemInfo(id=7, title="t", work_item_type="Bug", state="New", tags=["autopilot"])
    await p._process_agent(item, item)
    assert c.executor.briefed_stages == [
        "analyze", "design", "implement", "test", "review", "pr",
    ]

    # A wired state still narrows it to that role.
    from ai_autopilot.config import SdlcStageWiring
    c.config.sdlc_stage_wiring = {
        "test": SdlcStageWiring(queue_state="Ready for Testing", working_state="In Testing"),
    }
    qc = WorkItemInfo(id=8, title="t", work_item_type="Bug",
                      state="Ready for Testing", tags=["autopilot"])
    await p._dispatch_interactive(qc)
    assert c.executor.briefed_stages == ["test"]


async def test_headless_mode_still_runs_the_engine():
    p, c = _poller()
    c.config.sdlc_loop_enabled = True
    c.config.execution_mode = "headless"
    ran: list[int] = []
    p._process_sdlc = lambda item, classified: _noop(ran, item.id)
    item = WorkItemInfo(id=9, title="t", work_item_type="Bug", state="New", tags=["autopilot"])
    await p._process_agent(item, item)
    assert ran == [9]


async def _noop(sink, value):
    sink.append(value)


async def test_a_branch_outside_the_prefixes_is_called_out_at_once():
    """Ownership is the branch prefix and nothing else, so a branch outside it means
    the merged PR never advances the item and /commands on it are ignored — both of
    which surface much later, as "it merged and nothing moved". 23 of the last 60 runs
    on a live machine did this because the brief never stated the rule."""
    p, c = _poller()
    warned: list = []
    p._log = SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None,
                             error=lambda *a, **k: None,
                             warning=lambda msg, **kw: warned.append(kw))
    item = WorkItemInfo(id=8107, title="t", work_item_type="Bug", state="New", tags=[])

    ok = ExecutionResult.ok(item.id, "agent", "done")
    ok.branch_name = "bugfix/8107-select-search"
    p._warn_unowned_branch(item, ok)
    assert warned == []

    bad = ExecutionResult.ok(item.id, "agent", "done")
    bad.branch_name = "8107-select-search-stale-request-race"
    p._warn_unowned_branch(item, bad)
    assert len(warned) == 1
    assert warned[0]["branch"] == "8107-select-search-stale-request-race"
    assert "bugfix/" in warned[0]["prefixes"] or "feature/" in warned[0]["prefixes"]


async def test_an_item_stranded_by_the_live_tag_is_reported_once():
    """A run killed with its process never writes a result, so the live tag stays on
    the item — and the poller skips that tag. The item then goes quiet for good:
    the board still shows it, ▶ Run reports started=1, and nothing ever happens.

    This side cannot tell a dead console from a slow one, so it says so instead of
    choosing silence, and says it once rather than every scan.
    """
    p, c = _poller()
    c.config.live_tag = "autopilot-live"
    c.config.restart_tag = "autopilot-restart"
    warned: list = []
    p._log = SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None,
                             error=lambda *a, **k: None,
                             warning=lambda msg, **kw: warned.append(kw))
    c.ado.tagged_items = [_tagged(8626, "Active", ["vm-autopilot", "autopilot-live"])]
    c.executor._final = None                     # the console died: no result, ever

    await p._finalize_orphan_sessions()
    await p._finalize_orphan_sessions()           # a second scan must stay quiet

    assert len(warned) == 1
    # The payload is a batch now: a restart strands every item the previous process
    # held, and one warning each arrived at exactly the moment the log is being read.
    assert warned[0]["ids"] == [8626] and warned[0]["count"] == 1
    # The remedy named is the one that exists — ▶ Run — with the restart tag kept as
    # the heavier alternative it is, since it releases the item by wiping its progress.
    assert "▶ Run" in warned[0]["hint"]
    assert "autopilot-restart" in warned[0]["hint"]


async def test_the_live_session_comment_says_which_role_it_is_running():
    """With the relay, a run is no longer "the whole item" — it is one role's steps,
    and a reader of the work item cannot see that anywhere else. The same sentence
    used to appear whether the session was running a single QC check or all six."""
    p, c = _poller()
    c.config.sdlc_loop_enabled = True
    c.config.execution_mode = "interactive"
    c.config.sdlc_default_profile = "qc"
    item = WorkItemInfo(id=8965, title="t", work_item_type="Requirement",
                        state="Active", tags=["autopilot"])
    await p._dispatch_interactive(item)

    said = " ".join(str(x) for x in c.ado.comments)
    assert "Live session started" in said
    assert "<b>qc</b>" in said and "test" in said
    assert "a later role picks the item up" in said

    # Unscoped runs keep the plain notice — there is no role to name.
    c.config.sdlc_loop_enabled = False
    c.config.sdlc_stage_wiring = {}
    c.ado.comments.clear()
    await p._dispatch_interactive(item)
    assert "Running" not in " ".join(str(x) for x in c.ado.comments)


def test_the_poll_query_says_where_it_differs_from_the_trigger_states():
    """Someone reading the log to find out why an item in a trigger state is never
    picked up used to find nothing: the role wiring rewrites the query on a different
    page from the one that lists the states, and the amendment was never stated."""
    from ai_autopilot.config import SdlcRole
    from ai_autopilot.services.poller import AdoPollerService

    said: list[tuple[str, dict]] = []
    cfg = Settings(
        trigger_states=["New", "Active"],
        sdlc_roles={
            "dev": SdlcRole(stages=["implement"], waits_in="Active", auto=False),
            "qc": SdlcRole(stages=["test"], waits_in="Ready for Testing", auto=True),
        },
    )
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._log = type("L", (), {"info": lambda _s, e, **kw: said.append((e, kw))})()
    svc._log_trigger_amendments()

    assert len(said) == 1
    kw = said[0][1]
    assert kw["dropped"] == ["Active"]              # not auto → removed from the query
    assert kw["added"] == ["Ready for Testing"]     # auto → added
    assert "New" in kw["polled"]


def test_nothing_is_said_when_the_wiring_changes_no_state():
    """Silent in the steady state — an untouched install must not gain a line."""
    from ai_autopilot.services.poller import AdoPollerService

    said: list = []
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = Settings(trigger_states=["New", "Active"])
    svc._log = type("L", (), {"info": lambda _s, e, **kw: said.append(e)})()
    svc._log_trigger_amendments()
    assert said == []


async def test_a_handoff_releases_the_item_so_the_next_role_can_run_it():
    """The chain stopped dead after its first leg. _handle_agent_result tags the item
    done, the poller skips every item carrying an outcome tag, and the hand-off then
    set the next role's state — so the board showed the right column and nothing ever
    ran there. It looked like a configuration mistake."""
    from ai_autopilot.config import SdlcRole
    from ai_autopilot.models import ExecutionResult, WorkItemInfo
    from ai_autopilot.services.poller import AdoPollerService

    cfg = Settings(
        processed_tag="autopilot-done",
        sdlc_roles={
            "dev": SdlcRole(stages=["implement"], waits_in="Ready for Development",
                            done="Ready for Testing"),
            "qc": SdlcRole(stages=["test"], waits_in="Ready for Testing"),
        },
    )
    ado = _FakeAdo()
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(ado=ado)
    svc._log = type("L", (), {"info": lambda *a, **k: None, "error": lambda *a, **k: None})()
    item = WorkItemInfo(id=7, title="t", work_item_type="Requirement",
                        state="Ready for Development", tags=["autopilot-done"])

    await svc._apply_sdlc_handoff(item, ExecutionResult.ok(7, "dev", "done"), "dev")

    assert (7, "Ready for Testing") in ado.states       # handed to qc's door…
    assert (7, "autopilot-done") in ado.removed         # …and released so qc can run it


async def test_a_handoff_nobody_waits_for_stays_done():
    """A dead end is a deliberate parking spot — a person takes it from there, and the
    item must keep the tag that stops the poller grabbing it again."""
    from ai_autopilot.config import SdlcRole
    from ai_autopilot.models import ExecutionResult, WorkItemInfo
    from ai_autopilot.services.poller import AdoPollerService

    cfg = Settings(
        processed_tag="autopilot-done",
        sdlc_roles={"qc": SdlcRole(stages=["test"], waits_in="Ready for Testing",
                                   done="Ready for UAT")},
    )
    ado = _FakeAdo()
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(ado=ado)
    svc._log = type("L", (), {"info": lambda *a, **k: None, "error": lambda *a, **k: None})()
    item = WorkItemInfo(id=8, title="t", work_item_type="Requirement",
                        state="Ready for Testing", tags=["autopilot-done"])

    await svc._apply_sdlc_handoff(item, ExecutionResult.ok(8, "qc", "done"), "qc")

    assert (8, "Ready for UAT") in ado.states
    assert ado.removed == []            # nobody waits there — it stays done


async def test_four_stranded_items_produce_one_line_not_four():
    """The real restart that prompted this: ids 7463, 7695, 8470 and 9012 each got their
    own near-identical warning. The single-item path is covered above; this is about the
    wall of them."""
    from ai_autopilot.models import WorkItemInfo
    from ai_autopilot.services.poller import AdoPollerService

    said: list[dict] = []

    class _Exec:
        @staticmethod
        def interactive_scratch_dir(item_id):
            return f"/scratch/{item_id}"

        @staticmethod
        def finalize_interactive(item, run_dir):
            return None                     # the console died with its process

    class _Ado:
        async def get_all_tagged_work_items(self):
            return [
                WorkItemInfo(id=i, title="t", work_item_type="Task", state="Active",
                             tags=["autopilot-live"])
                for i in (7463, 7695, 8470, 9012)
            ]

    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = Settings(live_tag="autopilot-live", restart_tag="autopilot-restart")
    svc._c = SimpleNamespace(ado=_Ado(), executor=_Exec())
    svc._live = {}
    svc._stranded = set()
    svc._log = type("L", (), {
        "warning": lambda _s, e, **kw: said.append(kw),
        "info": lambda *a, **k: None,
    })()

    await svc._finalize_orphan_sessions()
    assert len(said) == 1 and said[0]["count"] == 4
    assert said[0]["ids"] == [7463, 7695, 8470, 9012]


async def _anoop(*a, **k):
    """A stand-in for a collaborator whose return value this test does not read."""
    return None


class _NullStateRepo:
    """The run-now path records a QUEUED state; nothing here asserts on it."""

    async def set(self, *_a, **_kw):
        return None


async def _run_reconcile(svc) -> list[int]:
    """Run one reconcile pass and return the ids it dispatched.

    ``_reconcile_stage_entries`` fires the run through ``asyncio.create_task``, so the
    dispatch has not happened yet when it returns — one yield lets those tasks start.
    """
    started: list[int] = []

    async def _fake_process(item):
        started.append(item.id)

    svc._process = _fake_process
    await svc._reconcile_stage_entries()
    await asyncio.sleep(0)
    return started


async def test_a_roles_own_run_now_tag_runs_that_role_not_the_default_pipeline():
    """#8965: `vm-autopilot-run` on an item in "Ready for UAT" ran the FULL pipeline.

    `entry_tags()` maps tag -> role and `entry_tag_for()` promises the tag "starts this
    role regardless of the item's state", but the map was only ever used to MATCH, so
    the role it named was dropped and `resolve_profile_name` fell through state (nobody
    waits in Ready for UAT) to `sdlc_default_profile`. A role's own tag now pins it.
    """
    from ai_autopilot.config import SdlcRole
    from ai_autopilot.execution.sdlc_plan import resolve_profile_name

    cfg = Settings(
        sdlc_default_profile="full",
        stage_entry_tag="vm-autopilot-run",
        sdlc_roles={
            "full": SdlcRole(stages=["analyze", "implement", "pr"]),
            "qc": SdlcRole(stages=["test"], entry_tag="vm-autopilot-run-qc"),
        },
    )
    ado = _FakeAdo()
    item = WorkItemInfo(id=8965, title="t", work_item_type="Requirement",
                        state="Ready for UAT", tags=["vm-autopilot-run-qc", "TLLA"])
    ado.tagged_items = [item]

    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(ado=ado, state_repo=_NullStateRepo())
    svc._live, svc._processed = {}, {}
    svc._log = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})()

    assert await _run_reconcile(svc) == [8965]
    assert (8965, "vm-autopilot-run-qc") in ado.removed   # one-shot, consumed on pickup
    assert (8965, "sdlc:qc") in ado.tags                  # ...and the role is pinned
    # The pin is what the engine reads, so QC runs -- not the default profile.
    assert resolve_profile_name(item.tags, "Requirement", cfg, state="Ready for UAT") == "qc"


async def test_the_shared_run_now_tag_still_lets_the_state_pick_the_role():
    """The shared tag means "start where it stands" -- it names no role, so it must not
    pin one. Only the state may decide, exactly as documented."""
    from ai_autopilot.config import SdlcRole

    cfg = Settings(
        stage_entry_tag="vm-autopilot-run",
        sdlc_roles={"qc": SdlcRole(stages=["test"], waits_in="Ready for Testing")},
    )
    ado = _FakeAdo()
    item = WorkItemInfo(id=41, title="t", work_item_type="Requirement",
                        state="Ready for Testing", tags=["vm-autopilot-run"])
    ado.tagged_items = [item]

    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(ado=ado, state_repo=_NullStateRepo())
    svc._live, svc._processed = {}, {}
    svc._log = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})()

    assert await _run_reconcile(svc) == [41]
    assert (41, "vm-autopilot-run") in ado.removed
    assert [t for (_i, t) in ado.tags if t.startswith("sdlc:")] == []


async def test_a_handoff_releases_the_role_pin_so_the_next_leg_is_not_the_old_role():
    """A pin that outlives its run owns the item forever: the tag beats the state in
    `resolve_profile_name`, so an item handed to the next role's door would be picked up
    as the OLD role again, every leg, invisibly."""
    from ai_autopilot.config import SdlcRole
    from ai_autopilot.execution.sdlc_plan import resolve_profile_name

    cfg = Settings(
        processed_tag="autopilot-done",
        sdlc_roles={
            "dev": SdlcRole(stages=["implement"], waits_in="Ready for Development",
                            done="Ready for Testing"),
            "qc": SdlcRole(stages=["test"], waits_in="Ready for Testing"),
        },
    )
    ado = _FakeAdo()
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(ado=ado)
    svc._log = type("L", (), {"info": lambda *a, **k: None, "error": lambda *a, **k: None})()
    item = WorkItemInfo(id=9, title="t", work_item_type="Requirement",
                        state="Ready for Development", tags=["sdlc:dev", "autopilot-done"])

    await svc._apply_sdlc_handoff(item, ExecutionResult.ok(9, "dev", "done"), "dev")

    assert (9, "Ready for Testing") in ado.states
    assert (9, "sdlc:dev") in ado.removed
    # With the pin gone the door decides again, which is the whole point of the relay.
    survived = [t for t in item.tags if (9, t) not in ado.removed]
    assert resolve_profile_name(survived, "Requirement", cfg, state="Ready for Testing") == "qc"


async def test_the_run_now_tag_works_on_an_item_the_autopilot_is_not_already_holding():
    """#9004: tagging an item with the run-now tag did nothing — no pickup, no error,
    nothing in the log.

    The sweep read the TRIGGER-tag query, so only items the autopilot already owned were
    ever in the result set. An item carrying just the run-now tag — which is exactly what
    a person reaches for the tag for — was never looked at.
    """
    from ai_autopilot.config import SdlcRole

    cfg = Settings(
        trigger_tags=["vm-autopilot"],
        stage_entry_tag="vm-autopilot-run",
        sdlc_roles={"qc": SdlcRole(stages=["test"], waits_in="Ready for Testing")},
    )
    ado = _FakeAdo()
    # No trigger tag. Just the run-now tag, the way a person adds it on the board.
    item = WorkItemInfo(id=9004, title="t", work_item_type="Requirement",
                        state="Ready for Testing", tags=["vm-autopilot-run", "TLLA"])
    ado.tagged_items = [item]

    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(ado=ado, state_repo=_NullStateRepo())
    svc._live, svc._processed = {}, {}
    svc._log = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})()

    assert await _run_reconcile(svc) == [9004]
    assert (9004, "vm-autopilot-run") in ado.removed      # consumed on pickup
    # It asked by run-now tag, not by trigger tag — that is the fix.
    assert ado.tagged_any_queries == [["vm-autopilot-run"]]


async def test_every_run_now_tag_is_queried_not_just_the_shared_one():
    """A role's own tag has to be in the query too, or that role is unreachable by tag
    on any item the autopilot is not already holding."""
    from ai_autopilot.config import SdlcRole

    cfg = Settings(
        stage_entry_tag="vm-autopilot-run",
        sdlc_roles={"qc": SdlcRole(stages=["test"], entry_tag="vm-autopilot-run-qc")},
    )
    ado = _FakeAdo()
    ado.tagged_items = []

    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(ado=ado, state_repo=_NullStateRepo())
    svc._live, svc._processed = {}, {}
    svc._log = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})()

    await _run_reconcile(svc)

    assert sorted(ado.tagged_any_queries[0]) == ["vm-autopilot-run", "vm-autopilot-run-qc"]


async def test_a_finished_run_says_which_role_it_was():
    """"✅ Completed #9004" with a branch and a PR does not tell a reader whether QC ran
    or the whole pipeline did — and the card is where most people find out at all."""
    from ai_autopilot.config import SdlcRole

    svc, c = _poller(
        sdlc_roles={"qc": SdlcRole(stages=["test"], waits_in="Ready for Testing")},
    )
    item = WorkItemInfo(id=9004, title="Export excel", work_item_type="Requirement",
                        state="Ready for Testing")
    result = ExecutionResult.ok(9004, "agent", "done")

    await svc._handle_agent_result(item, result)

    assert result.profile == "qc"


async def test_the_role_a_run_was_dispatched_as_wins_over_re_deriving_it():
    """By the time a run finishes the item sits in its WORKING state, and resolving the
    role from there answers a different question than the run was given."""
    svc, c = _poller()
    svc._live_profiles[7] = "qc"
    item = WorkItemInfo(id=7, title="t", work_item_type="Task", state="Active")
    result = ExecutionResult.ok(7, "agent", "done")

    await svc._handle_agent_result(item, result)

    assert result.profile == "qc"


async def test_a_qc_run_files_its_test_cases_onto_the_work_item():
    """#9004: QC wrote twenty test cases into a repo and opened a PR for them, so on the
    work item — where QC actually looks — there was nothing to see."""
    svc, c = _poller()
    created: list[dict] = []

    async def _create_test_case(**kw):
        created.append(kw)
        return 9100 + len(created)

    c.ado.create_test_case = _create_test_case
    item = WorkItemInfo(id=9004, title="Export excel", work_item_type="Requirement",
                        project="TLCL-DxFac")
    result = ExecutionResult.ok(9004, "agent", "done")
    result.test_cases = [
        SimpleNamespace(title="Export with no rows", steps=["Open", "Export"],
                        expected="A file with headers only", preconditions="Logged in"),
        SimpleNamespace(title="Export 10k rows", steps=["Export"], expected="No timeout",
                        preconditions=""),
    ]

    filed = await svc._file_test_cases(item, result)

    assert filed == 2
    assert [k["title"] for k in created] == ["Export with no rows", "Export 10k rows"]
    assert created[0]["tests_item_id"] == 9004     # linked to the item it tests
    assert created[0]["project"] == "TLCL-DxFac"   # a Test Case lands in the item's project


async def test_test_cases_are_not_filed_when_the_feature_is_off():
    svc, c = _poller(qc_create_test_case_items=False)
    c.ado.create_test_case = lambda **kw: (_ for _ in ()).throw(AssertionError("called"))
    result = ExecutionResult.ok(1, "agent", "done")
    result.test_cases = [SimpleNamespace(title="x", steps=[], expected="", preconditions="")]

    assert await svc._file_test_cases(WorkItemInfo(id=1, title="t"), result) == 0


async def test_one_refused_test_case_does_not_sink_the_run():
    """A process template that refuses the type or the link must cost the run nothing —
    the work is done either way."""
    svc, c = _poller()
    calls: list[str] = []

    async def _create_test_case(**kw):
        calls.append(kw["title"])
        if kw["title"] == "boom":
            raise RuntimeError("template refuses Test Case")
        return 1

    c.ado.create_test_case = _create_test_case
    result = ExecutionResult.ok(2, "agent", "done")
    result.test_cases = [
        SimpleNamespace(title="boom", steps=[], expected="", preconditions=""),
        SimpleNamespace(title="fine", steps=[], expected="", preconditions=""),
    ]

    assert await svc._file_test_cases(WorkItemInfo(id=2, title="t"), result) == 1
    assert calls == ["boom", "fine"]              # it kept going


async def test_a_qc_run_reports_what_it_executed_onto_the_work_item():
    """#8965: a run executed twenty cases and reported 19 pass / 1 fail onto the item
    because that session happened to hold an MCP tool that could. The next run did the
    same work invisibly. Whether QC's verdict is visible must not depend on that."""
    svc, c = _poller()
    item = WorkItemInfo(id=9083, title="WO form list", project="TLCL-DxFac")
    result = ExecutionResult.ok(9083, "agent", "done")
    result.test_results = [
        CaseOutcome(title="Filter by schedule", outcome="pass"),
        CaseOutcome(title="Export 10k rows", outcome="fail", note="timeout after 30s"),
        CaseOutcome(title="Bulk delete", outcome="blocked", note="no permission on demo"),
    ]

    total = await svc._report_test_results(item, result)

    assert total == 3
    body = c.ado.comments[-1][1]
    assert c.ado.comments[-1][0] == 9083
    assert "1/3 không đạt" in body                     # the verdict leads
    assert "timeout after 30s" in body                 # the note is what makes it actionable
    # Failures first: a reader who stops after one row has seen the one that decides.
    assert body.index("Export 10k rows") < body.index("Filter by schedule")


async def test_the_qc_report_does_not_wait_for_a_pull_request():
    """The complaint that started this: a `full` run's QC outcome was reachable only
    through the completion badge, which is only posted when a PR exists — and a run
    with no PR is the normal shape of QC."""
    svc, c = _poller()
    item = WorkItemInfo(id=9083, title="t")
    result = ExecutionResult.ok(9083, "agent", "done")          # no pr_url anywhere
    result.test_results = [CaseOutcome(title="Case A", outcome="pass")]

    await svc._handle_agent_result(item, result)

    assert any("QC — Kết quả thực thi" in body for _, body in c.ado.comments)


async def test_a_report_mode_run_no_longer_swallows_its_badge():
    """The no-PR branch built the badge and then returned without posting it, so a run
    that filed test cases but opened no PR filed them in silence."""
    svc, c = _poller(pr_scoring_enabled=False)
    created: list[dict] = []

    async def _create_test_case(**kw):
        created.append(kw)
        return 9100 + len(created)

    c.ado.create_test_case = _create_test_case
    item = WorkItemInfo(id=9083, title="t")
    result = ExecutionResult.ok(9083, "agent", "done")           # no PR
    result.test_cases = [
        SimpleNamespace(title="Case A", steps=[], expected="", preconditions=""),
    ]

    await svc._handle_agent_result(item, result)

    assert created                                               # the case WAS filed
    assert any("1 test case" in body for _, body in c.ado.comments), (
        "the badge saying so must be posted even with no PR"
    )


async def test_a_comment_that_fails_to_post_does_not_sink_a_finished_run():
    svc, c = _poller()

    async def _boom(*a, **kw):
        raise RuntimeError("ADO down")

    c.ado.add_comment = _boom
    result = ExecutionResult.ok(3, "agent", "done")
    result.test_results = [CaseOutcome(title="Case A", outcome="fail", note="x")]

    assert await svc._report_test_results(WorkItemInfo(id=3, title="t"), result) == 0


async def test_nothing_is_posted_when_the_run_executed_nothing():
    """A run that only WROTE cases has no verdict to report, and an empty table would
    read as one."""
    svc, c = _poller()
    result = ExecutionResult.ok(4, "agent", "done")
    result.test_cases = [SimpleNamespace(title="x", steps=[], expected="", preconditions="")]

    assert await svc._report_test_results(WorkItemInfo(id=4, title="t"), result) == 0
    assert c.ado.comments == []


async def test_the_verdict_survives_an_escalation():
    """A QC run that escalates BECAUSE a case failed is exactly the run whose results
    someone needs to read — and needs_human returns before every reporting branch."""
    svc, c = _poller()
    item = WorkItemInfo(id=9083, title="t")
    result = ExecutionResult.fail(9083, "agent", "one case failed")
    result.needs_human = True
    result.test_results = [CaseOutcome(title="Export 10k", outcome="fail", note="timeout")]

    await svc._handle_agent_result(item, result)

    bodies = [body for _, body in c.ado.comments]
    assert any("QC — Kết quả thực thi" in b for b in bodies)
    assert any("Needs human input" in b for b in bodies)


async def test_the_verdict_survives_a_failed_run():
    svc, c = _poller(exhausted=True)
    result = ExecutionResult.fail(9083, "agent", "boom")
    result.test_results = [CaseOutcome(title="Case A", outcome="blocked", note="env down")]

    await svc._handle_agent_result(WorkItemInfo(id=9083, title="t"), result)

    assert any("QC — Kết quả thực thi" in body for _, body in c.ado.comments)


async def test_dry_run_reports_nothing():
    svc, c = _poller(dry_run=True)
    result = ExecutionResult.ok(5, "agent", "done")
    result.test_results = [CaseOutcome(title="Case A", outcome="pass")]

    assert await svc._report_test_results(WorkItemInfo(id=5, title="t"), result) == 0
    assert c.ado.comments == []


async def test_a_qc_role_run_is_no_longer_held_for_a_human_for_doing_its_job():
    """End to end through the poller: a `qc` role opens no PR and produces no diff, and
    the score gate held it for a human on exactly those two facts."""
    svc, c = _poller(
        sdlc_roles={"qc": SdlcRole(stages=["test"], waits_in="Ready for Testing")},
    )
    item = WorkItemInfo(id=9083, title="t", state="Ready for Testing")
    result = ExecutionResult.ok(9083, "agent", "20 cases run")
    result.profile = "qc"

    await svc._handle_agent_result(item, result)

    assert PipelineState.NEEDS_HUMAN not in [st for _, st in c.state_repo.calls]
    assert not any("Held for human" in body for _, body in c.ado.comments)


async def test_a_dev_role_with_no_pr_is_still_held():
    """The waiver is the ROLE's, not a blanket one: a dev run that opened no PR is the
    case the gate exists for."""
    svc, c = _poller(
        sdlc_roles={"dev": SdlcRole(stages=["implement", "pr"], waits_in="Ready for Dev")},
    )
    result = ExecutionResult.ok(9084, "agent", "done")
    result.profile = "dev"

    await svc._handle_agent_result(WorkItemInfo(id=9084, title="t"), result)

    assert any("Held for human" in body for _, body in c.ado.comments)


async def test_an_escalated_run_still_files_the_cases_it_wrote():
    """needs_human is TERMINAL — nobody re-runs the item, so cases left unfiled there
    are lost for good."""
    svc, c = _poller()
    created: list[dict] = []

    async def _create_test_case(**kw):
        created.append(kw)
        return 1

    c.ado.create_test_case = _create_test_case
    result = ExecutionResult.fail(9083, "agent", "blocked on env")
    result.needs_human = True
    result.test_cases = [
        SimpleNamespace(title="Case A", steps=[], expected="", preconditions=""),
    ]

    await svc._handle_agent_result(WorkItemInfo(id=9083, title="t"), result)

    assert [k["title"] for k in created] == ["Case A"]


async def test_a_retryable_failure_does_not_file_cases_twice():
    """That run comes back. Duplicated Test Case items are worse than late ones."""
    svc, c = _poller()
    c.ado.create_test_case = lambda **kw: (_ for _ in ()).throw(AssertionError("filed"))
    result = ExecutionResult.fail(9083, "agent", "boom")          # retryable, not escalated
    result.test_cases = [
        SimpleNamespace(title="Case A", steps=[], expected="", preconditions=""),
    ]

    await svc._handle_agent_result(WorkItemInfo(id=9083, title="t"), result)


async def test_the_engine_runs_the_role_the_door_named_not_the_default():
    """The stages are chosen where the run HAPPENS, and that call did not pass the
    item's state — so tag > machine-pin > STATE > type > default silently lost its
    middle term: an item in QC's door ran the default pipeline. Worse, the poller
    resolved WITH the state for the record and the Teams card, so the card said "qc"
    about a run that was never qc.

    The door state is also gone by then: the item is moved to the working state before
    the engine starts, and no role waits in "Active" — so the role has to be decided
    before that move and carried, which is what ``profile=`` does.
    """
    from ai_autopilot.config import SdlcRole
    from ai_autopilot.execution.sdlc_loop import SdlcLoopEngine

    cfg = Settings(
        sdlc_loop_enabled=True, sdlc_default_profile="full",
        sdlc_roles={
            "qc": SdlcRole(stages=["test"], waits_in="Ready for Testing"),
            "full": SdlcRole(stages=["implement", "test", "review", "pr"]),
        },
    )
    loop = SdlcLoopEngine.__new__(SdlcLoopEngine)
    loop._config = cfg
    item = WorkItemInfo(id=77, title="t", work_item_type="Requirement",
                        state="Ready for Testing")

    # Resolved from the state alone (the door is still there) …
    from ai_autopilot.execution.sdlc_plan import resolve_profile_name
    assert resolve_profile_name(item.tags, item.work_item_type, cfg,
                                state=item.state or "") == "qc"
    # … and the loop honours a profile handed to it, even once the item has moved on.
    item.state = "Active"
    assert [s.name for s in (loop._stages_for("qc") or [])] == ["test"]
    assert resolve_profile_name(item.tags, item.work_item_type, cfg,
                                state=item.state or "") == "full"     # the door is gone


async def test_the_sdlc_run_decides_the_role_before_the_item_leaves_its_door():
    """``_apply_outcome(in_progress)`` moves the item to the working state. Resolving
    the role after that reads "Active", which no role waits in — the default profile."""
    from ai_autopilot.config import SdlcRole

    cfg = Settings(
        sdlc_loop_enabled=True, sdlc_default_profile="full", state_in_progress="Active",
        sdlc_roles={
            "qc": SdlcRole(stages=["test"], waits_in="Ready for Testing"),
            "full": SdlcRole(stages=["implement", "test", "review", "pr"]),
        },
    )
    seen: dict = {}

    class _Engine:
        async def run(self, item, profile=""):
            seen["profile"] = profile
            seen["state_at_run"] = item.state
            return ExecutionResult.ok(item.id, "sdlc", "done")

    class _ExecRepo:
        async def start_execution(self, item, kind, trigger_tag=None, profile=""):
            seen["recorded"] = profile
            return 1

        async def complete_execution(self, *a, **k):
            return None

    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._live_profiles = {}
    svc._log = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})()
    svc._c = SimpleNamespace(
        ado=_FakeAdo(), state_repo=_NullStateRepo(), sdlc_engine=_Engine(),
        execution_repo=_ExecRepo(), cost_tracker=None,
        notifier=SimpleNamespace(notify_started=_anoop),
        plugins=SimpleNamespace(run_post_processors=_anoop),
    )
    svc._handle_agent_result = _anoop
    svc._apply_sdlc_handoff = _anoop
    svc._matched_tag = lambda item: ""

    item = WorkItemInfo(id=78, title="t", work_item_type="Requirement",
                        state="Ready for Testing")
    await svc._process_sdlc(item, item)

    assert seen["profile"] == "qc"          # the door decided, not the default
    assert seen["recorded"] == "qc"         # …and the record says the same thing
    assert seen["state_at_run"] == "Active"  # even though the door is already gone


# ── An interactive session that outlived a restart ─────────────────────────────
# Its console is still on screen and its result lands normally; what the autopilot
# lost was the in-memory row pointing at it. Everything below is what that costs.

async def test_a_recovered_orphan_session_is_recorded_like_any_other_run():
    """It used to notify and update ADO without ever opening a row.

    So a run that had been working for half an hour finished invisibly: nothing in
    History, no tokens counted, and — because "is this PR ours" is answered from the
    work items we hold runs for — its pull request missing from the merge rate and
    from cost-per-shipped-PR. The Teams card's "Duration 00:00" was the visible
    corner of it: with no row, nothing filled the time in either.
    """
    p, c = _poller()
    done = ExecutionResult.ok(7, "agent", "done")
    done.pr_url = "https://pr"
    done.duration_seconds = 1800.0          # what the session handle says it lived
    c.executor = _FakeExec(final=done)
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot", c.config.live_tag])]

    await p._finalize_orphan_sessions()

    assert c.execution_repo.completed == [(99, True)]
    item_id, skill, started_at = c.execution_repo.started[0]
    assert (item_id, skill) == (7, "interactive:(recovered)")
    # Back-dated by the real runtime: filing it as starting when we NOTICED would put
    # a half-hour run on the timeline as an instant one.
    assert started_at is not None
    assert 1795 <= (datetime.now(UTC) - started_at).total_seconds() <= 1805


async def test_finalising_an_orphan_cleans_up_that_session_not_the_last_one_seen():
    """The close/release used to sit AFTER the loop, reading whatever the last
    iteration left in `item` / `run_dir`.

    Since the stranded list only fills from sessions that are still RUNNING, the
    console it shut and the worktree it deleted were usually a live session's —
    taking unfinished work with them — while the orphan actually finalised here was
    never closed or released at all. Item 7 is finished; item 8 is still working.
    """
    p, c = _poller()
    c.config.interactive_close_on = "result"
    done = ExecutionResult.ok(7, "agent", "done")
    done.pr_url = "https://pr"

    class _PerItemExec(_FakeExec):
        def finalize_interactive(self, item, run_dir):
            return done if item.id == 7 else None      # 8 has written nothing yet

    c.executor = _PerItemExec()
    c.ado.tagged_items = [
        _tagged(7, "Active", ["autopilot", c.config.live_tag]),
        _tagged(8, "Active", ["autopilot", c.config.live_tag]),
    ]

    await p._finalize_orphan_sessions()

    # Exactly the finished session is torn down…
    assert c.executor.closed == [(7, "/ws/agent-7")]
    assert c.executor.released == ["/ws/agent-7"]
    # …and the one still working keeps its console and its worktree.
    assert 8 in p._stranded


async def test_a_still_running_orphan_is_left_completely_alone():
    p, c = _poller()
    c.config.interactive_close_on = "result"
    c.executor = _FakeExec(final=None)                 # nobody has written a result
    c.ado.tagged_items = [_tagged(7, "Active", ["autopilot", c.config.live_tag])]

    await p._finalize_orphan_sessions()

    assert c.executor.closed == [] and c.executor.released == []
    assert c.execution_repo.completed == []
    assert (7, c.config.live_tag) not in c.ado.removed  # still live, still tagged
