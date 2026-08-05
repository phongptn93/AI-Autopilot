"""Tests for the poller's AI-native result handling (_handle_agent_result)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.data import PipelineState
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.outcomes import outcome_policy
from ai_autopilot.services.poller import AdoPollerService


class _FakeAdo:
    def __init__(self):
        self.tags: list[tuple[int, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.states: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []
        self.tagged_items: list = []
        self.comments_by_item: dict[int, list[dict]] = {}
        self.reviewers: list[tuple[str, int, str, bool]] = []

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

    async def get_work_item_comments(self, work_item_id):
        return self.comments_by_item.get(work_item_id, [])

    async def add_pull_request_reviewer(self, repo_id, pr_id, reviewer_id, *, required=False):
        self.reviewers.append((repo_id, pr_id, reviewer_id, required))
        return True


class _FakeExec:
    def __init__(self, dispatch=(True, "autopilot-7"), final=None):
        self._dispatch, self._final = dispatch, final
        self.released: list[str | None] = []

    async def dispatch_interactive(self, item, *, autonomy, draft_pr):
        launched, session = self._dispatch
        return launched, session, "/ws/scratch"

    def finalize_interactive(self, item, run_dir):
        return self._final

    def interactive_scratch_dir(self, item_id):
        return f"/ws/agent-{item_id}"

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
    assert c.executor.released == ["/ws/scratch"]              # scratch torn down
    assert c.execution_repo.completed == [(99, True)]
    assert (7, c.config.review_tag) in c.ado.tags              # went to In review


async def test_finalize_skips_while_session_running():
    p, c = _poller()
    c.executor = _FakeExec(final=None)                         # no result.json yet
    p._live = {7: 99}
    await p._finalize_live_sessions()
    assert p._live == {7: 99}                                  # still live, not finalised


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
