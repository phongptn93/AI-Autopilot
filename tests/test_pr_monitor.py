"""Tests for the PR babysitter's dispatch behaviour (locking + authorisation)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_autopilot.config import BOT_COMMENT_PREFIX, Settings
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.services.pr_monitor import PrMonitorService


class _FakeAdo:
    """Records replies; answers threads/work-item lookups from canned data."""

    def __init__(self, threads: list[dict]) -> None:
        self.threads = threads
        self.replies: list[str] = []
        self.statuses: list[str] = []

    async def get_pull_request_threads(self, repo_id, pr_id):
        return self.threads

    async def get_work_item(self, work_item_id):
        return SimpleNamespace(id=work_item_id, title="t")

    async def reply_to_pull_request_thread(self, repo_id, pr_id, thread_id, text):
        self.replies.append(text)
        return True

    async def set_pull_request_thread_status(self, repo_id, pr_id, thread_id, status):
        self.statuses.append(status)
        return True

    async def add_comment(self, work_item_id, text):
        return True

    async def get_repositories(self):
        return []


class _FakeFeedback:
    """Counts concurrent handle_feedback calls to detect same-branch overlap."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.calls: list[str] = []

    async def handle_feedback(self, item, branch, feedback, revision, repo="",
                              review_only=False):
        self.calls.append(feedback)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        return ExecutionResult.ok(item.id, feedback, "done")


def _thread(tid: int, cid: int, content: str, email: str = "phong@nois.vn"):
    return {
        "id": tid, "status": "active",
        "comments": [{
            "id": cid, "commentType": "text",
            "author": {"displayName": "Phong", "uniqueName": email},
            "content": content,
        }],
    }


async def _no_bot_identity() -> dict:
    return {"id": "", "display_name": "", "unique_name": ""}


async def _no_mention_identity():
    return None


def _service(ado, feedback, **overrides) -> PrMonitorService:
    config = Settings(
        comment_command="/ai, /review", max_concurrent=4,
        pr_adjust_related_drafts=False, **overrides,
    )
    c = SimpleNamespace(
        config=config, ado=ado, feedback=feedback, executor=None,
        # No bot identity in these fakes → @mention detection is simply off, so these
        # tests keep exercising the plain /command path.
        bot_identity=_no_bot_identity, mention_identity=_no_mention_identity,
    )
    return PrMonitorService(c)


_PR = {"pullRequestId": 5, "sourceRefName": "refs/heads/feature/be/42-thing"}


async def test_same_branch_commands_are_serialised():
    # A human replies /ai to two review threads at once → both must run, but never
    # concurrently on the same branch (worktree add -B would fail / stale fetch).
    ado = _FakeAdo([_thread(10, 1, "/ai fix issue 1"), _thread(20, 2, "/ai fix issue 2")])
    feedback = _FakeFeedback()
    svc = _service(ado, feedback)

    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)

    assert sorted(feedback.calls) == ["/ai fix issue 1", "/ai fix issue 2"]
    assert feedback.max_active == 1        # per-branch lock: no overlap


async def test_unauthorized_commenter_gets_refusal_not_silence():
    ado = _FakeAdo([_thread(10, 1, "/ai delete everything", email="mallory@other.vn")])
    feedback = _FakeFeedback()
    svc = _service(ado, feedback, assignee_trigger_user="phong@nois.vn")

    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)

    assert feedback.calls == []                       # never executed
    assert len(ado.replies) == 1                      # told them who drives the bot
    assert "phong@nois.vn" in ado.replies[0]

    # A second scan must not refuse again (comment marked handled).
    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)
    assert len(ado.replies) == 1


async def test_commands_from_anyone_runs_a_strangers_command():
    """Opting in means no refusal for anybody — the owner stays configured (it still
    decides which items get picked up), so the gate must read the flag, not the roster."""
    ado = _FakeAdo([_thread(10, 1, "/ai fix issue 1", email="mallory@other.vn")])
    feedback = _FakeFeedback()
    svc = _service(
        ado, feedback, assignee_trigger_user="phong@nois.vn", commands_from_anyone=True
    )

    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)

    assert feedback.calls == ["/ai fix issue 1"]
    assert not any("chỉ nhận lệnh" in r for r in ado.replies)


async def test_authorized_command_runs_and_resolves():
    ado = _FakeAdo([_thread(10, 1, "/ai rename field")])
    feedback = _FakeFeedback()
    svc = _service(ado, feedback, assignee_trigger_user="phong@nois.vn")

    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)

    assert feedback.calls == ["/ai rename field"]
    assert "pending" in ado.statuses and "fixed" in ado.statuses
    # Completion reply teaches the user they can keep the conversation going.
    assert any("/ai" in r for r in ado.replies)


async def test_handled_command_puts_pr_on_hot_lane_and_picks_followup():
    # Local setups have no webhook — after the bot engages a PR, the hot lane re-polls
    # just that PR, so a follow-up reply is picked up in seconds without a global scan.
    ado = _FakeAdo([_thread(10, 1, "/ai fix issue 1")])
    feedback = _FakeFeedback()
    svc = _service(ado, feedback)

    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)
    assert ("repo-1", 5) in svc._hot                  # engaged → fast lane

    ado.threads.append(_thread(20, 2, "/ai fix issue 2"))   # the follow-up reply
    await svc._poll_hot_once()
    await asyncio.gather(*svc._tasks)
    assert "/ai fix issue 2" in feedback.calls        # picked up by the hot lane


async def test_hot_lane_cools_down_after_window():
    ado = _FakeAdo([])
    svc = _service(ado, _FakeFeedback())
    svc._hot[("repo-1", 5)] = (0.0, "repo-a", _PR)    # already expired
    await svc._poll_hot_once()
    assert svc._hot == {}                              # pruned, back to global scan


async def test_review_is_free_of_the_revision_budget():
    # /review is advisory (changes no code) → it must neither consume nor hit
    # max_revisions, so "review lại" always works — even on a revision-capped item.
    ado = _FakeAdo([_thread(10, 1, "/ai change it")])
    feedback = _FakeFeedback()
    svc = _service(ado, feedback, max_revisions=1)

    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)
    assert svc._revision_counts[42] == 1              # /ai consumed the whole budget

    ado.threads.append(_thread(20, 2, "/review check it again"))
    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)

    assert "/review check it again" in feedback.calls  # ran despite the cap
    assert svc._revision_counts[42] == 1               # and didn't consume budget
    assert not any("Đã đạt" in r for r in ado.replies)  # no cap refusal posted

    # ...while a further ACTION command is still correctly capped.
    ado.threads.append(_thread(30, 3, "/ai change more"))
    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)
    assert "/ai change more" not in feedback.calls
    assert any("Đã đạt" in r for r in ado.replies)


async def test_command_state_survives_restart(tmp_path):
    # Handled commands and the spent revision budget live in the DB — a restarted
    # process must neither re-run an old command nor forget how much budget is gone.
    from ai_autopilot.data import Database, PrCommandRepository

    db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    await db.create_all()
    repo = PrCommandRepository(db)

    ado = _FakeAdo([_thread(10, 1, "/ai change it")])
    feedback = _FakeFeedback()
    svc1 = _service(ado, feedback, max_revisions=1)
    svc1._c.pr_command_repo = repo
    await svc1._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc1._tasks)
    assert feedback.calls == ["/ai change it"]

    # "Restart": fresh service, empty memory, same DB. The fake ado never records the
    # bot's replies into the threads, so ONLY the persisted handled set stops a re-run.
    feedback2 = _FakeFeedback()
    svc2 = _service(ado, feedback2, max_revisions=1)
    svc2._c.pr_command_repo = repo
    await svc2._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc2._tasks)
    assert feedback2.calls == []                       # not re-dispatched

    # The spent budget survives too: a NEW /ai after the restart is capped.
    ado.threads.append(_thread(20, 2, "/ai more"))
    await svc2._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc2._tasks)
    assert feedback2.calls == []
    assert any("Đã đạt" in r for r in ado.replies)
    await db.dispose()


async def test_bot_signed_command_never_triggers():
    ado = _FakeAdo([_thread(10, 1, BOT_COMMENT_PREFIX + "/ai not a real command")])
    feedback = _FakeFeedback()
    svc = _service(ado, feedback)

    await svc._inspect_pr("repo-1", "repo-a", _PR)
    await asyncio.gather(*svc._tasks)
    assert feedback.calls == []


class _FakeSessionExec:
    """Executor stub exposing just the interactive-session surface the sweep uses."""

    def __init__(self, sessions: dict[int, str], finished: set[int]) -> None:
        self._sessions, self._finished = sessions, finished
        self.closed: list[int] = []
        self.released: list[str] = []

    def list_open_sessions(self):
        return dict(self._sessions)

    def session_finished(self, run_dir, item_id):
        return item_id in self._finished

    async def close_interactive(self, run_dir, item_id):
        self.closed.append(item_id)
        return True

    async def release_scratch(self, run_dir):
        self.released.append(run_dir)


def _sweep_service(sessions, finished, **overrides):
    svc = _service(_FakeAdo([]), _FakeFeedback(), **overrides)
    svc._c.executor = _FakeSessionExec(sessions, finished)
    return svc


async def test_session_closed_once_its_pr_is_no_longer_open():
    # 42 merged (finished, no active PR) → close. 43 still has an open PR → keep,
    # so its review feedback can be reworked in the same session.
    svc = _sweep_service({42: "/ws/agent-42", 43: "/ws/agent-43"}, finished={42, 43})
    await svc._close_finished_sessions(active_items={43})
    assert svc._c.executor.closed == [42]
    assert svc._c.executor.released == ["/ws/agent-42"]


async def test_session_still_working_is_never_closed():
    # No result yet → the task is mid-flight and simply has no PR yet. Closing on
    # "not in active_items" alone would kill the console out from under it.
    svc = _sweep_service({42: "/ws/agent-42"}, finished=set())
    await svc._close_finished_sessions(active_items=set())
    assert svc._c.executor.closed == []


async def test_sweep_is_off_under_other_close_policies():
    svc = _sweep_service({42: "/ws/agent-42"}, finished={42}, interactive_close_on="result")
    await svc._close_finished_sessions(active_items=set())
    assert svc._c.executor.closed == []


async def test_unowned_pr_is_explained_once():
    """A hand-made PR is not this loop's business — but silence there reads as a broken
    bot, so it now says why, once, instead of ignoring the PR without a trace."""
    from ai_autopilot.services.pr_feedback import unowned_reason

    prefixes = tuple(Settings().bot_branch_prefixes)
    # Ownership is the prefix and nothing else — a branch we created but named without
    # a work-item id is still ours, and its item comes from ADO's link.
    assert "prefix" in unowned_reason("refs/heads/dxmpm/material-usage", prefixes)
    assert unowned_reason("refs/heads/feature/no-id-here", prefixes) == ""
    assert unowned_reason("refs/heads/feature/be/42-thing", prefixes) == ""

    class _Ado:
        async def get_pull_request_threads(self, *a, **k):
            raise AssertionError("must not fetch threads for a PR it does not own")

    c = SimpleNamespace(config=Settings(feedback_loop_enabled=True), ado=_Ado())
    svc = PrMonitorService(c)
    said: list = []
    # structlog does not go through caplog — watch the service's own logger.
    svc._log = SimpleNamespace(
        info=lambda msg, **kw: said.append(kw.get("reason", msg)),
        warning=lambda *a, **k: None, error=lambda *a, **k: None, debug=lambda *a, **k: None,
    )
    pr = {"pullRequestId": 3861, "sourceRefName": "refs/heads/dxmpm/material-usage"}
    await svc._inspect_pr("repo-id", "Micro-Frontend", pr)
    await svc._inspect_pr("repo-id", "Micro-Frontend", pr)   # a rescan stays quiet
    assert svc._unowned == {3861}
    assert len(said) == 1 and "prefix" in said[0]


async def test_a_bot_branch_without_an_id_falls_back_to_the_prs_linked_work_item():
    """Two rules decide ownership and only one is load-bearing. The prefix says "ours
    to push to"; the id in the name is just a cheap lookup — ADO already links PRs to
    work items, so dropping our own PR over its NAME was arbitrary."""
    asked: list = []

    class _Ado(_FakeAdo):
        async def get_pull_request_work_items(self, repo_id, pr_id):
            asked.append((repo_id, pr_id))
            return [8953]

    ado = _Ado([_thread(10, 1, "/ai fix it")])
    svc = _service(ado, _FakeFeedback())
    await svc._inspect_pr("repo-id", "Backend-Fresh", {
        "pullRequestId": 77, "sourceRefName": "refs/heads/feature/bom-usage-statistic",
    })
    assert asked == [("repo-id", 77)]          # asked ADO instead of giving up
    assert svc._unowned == set()               # ...and did not write it off

    # A branch nobody of ours created is still refused without an ADO round trip.
    asked.clear()
    await svc._inspect_pr("repo-id", "Micro-Frontend", {
        "pullRequestId": 3861, "sourceRefName": "refs/heads/dxmpm/material-usage",
    })
    assert asked == [] and svc._unowned == {3861}


async def test_the_link_wins_over_a_number_that_only_looks_like_an_id():
    """"fix/500-error-handling" is not work item 500. Trusting the name would load the
    wrong item's context and spend ITS revision budget."""

    class _Ado(_FakeAdo):
        async def get_pull_request_work_items(self, repo_id, pr_id):
            return [8953]

    svc = _service(_Ado([]), _FakeFeedback())
    assert await svc._work_item_for("r", 1, "refs/heads/fix/500-error-handling") == 8953

    class _NoLinks(_FakeAdo):
        async def get_pull_request_work_items(self, repo_id, pr_id):
            return []

    # No link at all: the branch name is the fallback, not the first answer.
    svc2 = _service(_NoLinks([]), _FakeFeedback())
    assert await svc2._work_item_for("r", 1, "refs/heads/feature/be/42-thing") == 42
    assert await svc2._work_item_for("r", 1, "refs/heads/feature/no-id") is None


async def test_a_pr_with_no_work_item_anywhere_is_skipped_with_a_reason():
    class _Ado(_FakeAdo):
        async def get_pull_request_work_items(self, repo_id, pr_id):
            return []

    ado = _Ado([_thread(10, 1, "/ai fix it")])
    svc = _service(ado, _FakeFeedback())
    said: list = []
    svc._log = SimpleNamespace(
        info=lambda msg, **kw: said.append(msg), warning=lambda *a, **k: None,
        error=lambda *a, **k: None, debug=lambda *a, **k: None,
    )
    await svc._inspect_pr("repo-id", "Backend-Fresh", {
        "pullRequestId": 78, "sourceRefName": "refs/heads/feature/no-id-at-all",
    })
    assert [m for m in said if "no work item behind this PR" in m]


async def test_a_review_that_found_nothing_does_not_claim_notes_above():
    """"Nhận xét chi tiết ở trên" was printed whether or not anything was written above
    it — a claim the reader checks in one glance, and one that was often false."""
    posted: list = []

    class _Ado(_FakeAdo):
        def __init__(self, threads):
            super().__init__(threads)
            self.bot_comments: list[dict] = []

        async def get_pull_request_threads(self, *a, **k):
            return [*self.threads, {"id": 99, "comments": self.bot_comments}]

        async def reply_to_pull_request_thread(self, repo_id, pr_id, tid, text):
            posted.append(text)

        async def set_pull_request_thread_status(self, *a, **k):
            return None

        async def add_comment(self, *a, **k):
            return None

    class _Silent(_FakeFeedback):
        async def handle_feedback(self, *a, **k):
            return ExecutionResult(work_item_id=42, success=True, skill_used="review")

    ado = _Ado([_thread(10, 1, "/review please")])
    svc = _service(ado, _Silent())
    await svc._handle_command("r", "Backend-Fresh", 5, 42, "feature/be/42-x",
                              WorkItemInfo(id=42, title="t"),
                              {"thread_id": 10, "instruction": "/review please",
                               "comment_id": 1, "advisory": True}, 0)
    assert any("không có nhận xét nào" in m for m in posted)
    assert not any("chi tiết ở trên" in m for m in posted)

    # The same run, but the review left a comment behind → the claim is true and made.
    posted.clear()
    ado2 = _Ado([_thread(11, 2, "/review please")])

    class _Talks(_FakeFeedback):
        async def handle_feedback(self, *a, **k):
            ado2.bot_comments.append({"id": 777, "content": BOT_COMMENT_PREFIX + "a finding"})
            return ExecutionResult(work_item_id=42, success=True, skill_used="review")

    svc2 = _service(ado2, _Talks())
    await svc2._handle_command("r", "Backend-Fresh", 5, 42, "feature/be/42-x",
                               WorkItemInfo(id=42, title="t"),
                               {"thread_id": 11, "instruction": "/review please",
                                "comment_id": 2, "advisory": True}, 0)
    assert any("chi tiết ở trên" in m for m in posted)
