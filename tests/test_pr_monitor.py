"""Tests for the PR babysitter's dispatch behaviour (locking + authorisation)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_autopilot.config import BOT_COMMENT_PREFIX, Settings
from ai_autopilot.models import ExecutionResult
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


def _service(ado, feedback, **overrides) -> PrMonitorService:
    config = Settings(
        comment_command="/ai, /review", max_concurrent=4,
        pr_adjust_related_drafts=False, **overrides,
    )
    c = SimpleNamespace(config=config, ado=ado, feedback=feedback, executor=None)
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
