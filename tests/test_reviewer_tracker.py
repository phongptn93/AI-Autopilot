"""Tests for the PR reviewer tracker (add detection, auto-review, reminders)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.data import Database, PrReviewerRepository, PrReviewerState
from ai_autopilot.models import ExecutionResult
from ai_autopilot.services.reviewer_tracker import (
    VOTE_APPROVED,
    ReviewerTrackerService,
)

BOT = {"id": "bot-id", "display_name": "AI Autopilot", "unique_name": "bot@nois.vn"}


class _FakeAdo:
    """Serves canned repos/PRs; records comments and votes."""

    def __init__(self, prs: list[dict]) -> None:
        self.prs = prs
        self.comments: list[str] = []
        self.votes: list[tuple[str, int]] = []

    async def get_repositories(self):
        return [{"id": "repo-1", "name": "repo-a"}]

    async def get_active_pull_requests(self, repo_id):
        return self.prs

    async def get_connection_data(self):
        return dict(BOT)

    async def add_pull_request_comment(self, repo_id, pr_id, text, *, active=False):
        self.comments.append(text)
        return True

    async def cast_pull_request_vote(self, repo_id, pr_id, reviewer_id, vote):
        self.votes.append((reviewer_id, vote))
        return True

    async def get_work_item(self, work_item_id):
        return None  # force the synthetic work-item path


class _FakeFeedback:
    def __init__(self, verdict: str = "approve") -> None:
        self.calls: list[str] = []
        self.verdict = verdict

    async def handle_feedback(self, item, branch, feedback, revision, repo="",
                              review_only=False):
        self.calls.append(feedback)
        return ExecutionResult.ok(item.id, "review", f"posted.\nVERDICT: {self.verdict}")


def _pr(reviewers: list[dict], commit: str = "c1", pr_id: int = 7) -> dict:
    return {
        "pullRequestId": pr_id,
        "title": "Add thing",
        "sourceRefName": "refs/heads/feature/be/42-thing",
        "lastMergeSourceCommit": {"commitId": commit},
        "reviewers": reviewers,
    }


def _human(rid: str = "u1", vote: int = 0) -> dict:
    return {"id": rid, "displayName": "Dev A", "uniqueName": "dev.a@nois.vn", "vote": vote}


def _bot_reviewer(vote: int = 0) -> dict:
    return {"id": BOT["id"], "displayName": BOT["display_name"],
            "uniqueName": BOT["unique_name"], "vote": vote}


async def _make(ado, feedback, **overrides):
    config = Settings(
        pr_reviewer_tracking_enabled=True, pr_auto_review_on_added=True,
        pr_reviewer_reminder_hours=24, max_concurrent=2, **overrides,
    )
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    repo = PrReviewerRepository(db)
    async def bot_identity() -> dict:
        return await ado.get_connection_data()

    async def mention_identity():
        return None  # @mention detection off in these fakes → plain /command path

    c = SimpleNamespace(
        config=config, ado=ado, feedback=feedback, pr_reviewer_repo=repo,
        bot_identity=bot_identity, mention_identity=mention_identity,
    )
    return ReviewerTrackerService(c), repo, db


async def _drain(svc: ReviewerTrackerService) -> None:
    while svc._tasks:
        await asyncio.gather(*list(svc._tasks))


async def test_new_human_reviewer_is_tracked():
    ado = _FakeAdo([_pr([_human(vote=0)])])
    svc, repo, _ = await _make(ado, _FakeFeedback())

    await svc._scan()

    rows = await repo.reviewers_for_pr(7)
    assert "u1" in rows
    assert rows["u1"].vote == 0
    assert rows["u1"].added_at is not None
    assert not rows["u1"].is_bot


async def test_vote_change_is_recorded():
    ado = _FakeAdo([_pr([_human(vote=0)])])
    svc, repo, _ = await _make(ado, _FakeFeedback())
    await svc._scan()

    ado.prs = [_pr([_human(vote=10)])]
    await svc._scan()

    rows = await repo.reviewers_for_pr(7)
    assert rows["u1"].vote == 10
    assert rows["u1"].last_vote_at is not None


async def test_bot_added_triggers_auto_review_and_vote():
    ado = _FakeAdo([_pr([_bot_reviewer()])])
    feedback = _FakeFeedback(verdict="approve")
    svc, repo, _ = await _make(ado, feedback)

    await svc._scan()
    await _drain(svc)

    assert len(feedback.calls) == 1
    assert (BOT["id"], VOTE_APPROVED) in ado.votes
    # ack + completion comments were posted
    assert any("Đã nhận vai trò reviewer" in c for c in ado.comments)
    assert any("Review xong" in c for c in ado.comments)
    rows = await repo.reviewers_for_pr(7)
    assert rows[BOT["id"]].reviewed_commit == "c1"

    # Same commit → no re-review on the next scan.
    await svc._scan()
    await _drain(svc)
    assert len(feedback.calls) == 1


async def test_new_commit_rearms_auto_review():
    ado = _FakeAdo([_pr([_bot_reviewer()], commit="c1")])
    feedback = _FakeFeedback()
    svc, repo, _ = await _make(ado, feedback)
    await svc._scan()
    await _drain(svc)
    assert len(feedback.calls) == 1

    ado.prs = [_pr([_bot_reviewer(vote=10)], commit="c2")]
    await svc._scan()
    await _drain(svc)

    assert len(feedback.calls) == 2
    rows = await repo.reviewers_for_pr(7)
    assert rows[BOT["id"]].reviewed_commit == "c2"


async def test_failed_review_does_not_loop():
    class _FailingFeedback(_FakeFeedback):
        async def handle_feedback(self, item, branch, feedback, revision, repo="",
                                  review_only=False):
            self.calls.append(feedback)
            return ExecutionResult.fail(item.id, "review", "boom")

    ado = _FakeAdo([_pr([_bot_reviewer()])])
    feedback = _FailingFeedback()
    svc, _, _ = await _make(ado, feedback)

    await svc._scan()
    await _drain(svc)
    await svc._scan()
    await _drain(svc)

    assert len(feedback.calls) == 1  # the failed iteration is not retried
    assert ado.votes == []


async def test_reminder_sent_once_after_deadline():
    ado = _FakeAdo([_pr([_human(vote=0)])])
    svc, repo, db = await _make(ado, _FakeFeedback())
    await svc._scan()
    assert not any("Nhắc review" in c for c in ado.comments)

    # Backdate the added_at past the 24h deadline.
    async with db.session() as session:
        row = await session.get(PrReviewerState, (7, "u1"))
        row.added_at = datetime.now(UTC) - timedelta(hours=25)
        await session.commit()

    await svc._scan()
    reminders = [c for c in ado.comments if "Nhắc review" in c]
    assert len(reminders) == 1
    assert "Dev A" in reminders[0]

    await svc._scan()  # already reminded → no duplicate
    assert len([c for c in ado.comments if "Nhắc review" in c]) == 1


async def _overdue(db, *, added_hours: int | None = None, reminded_hours: int | None = None):
    """Backdate the tracked row so a reminder clock has elapsed."""
    async with db.session() as session:
        row = await session.get(PrReviewerState, (7, "u1"))
        if added_hours is not None:
            row.added_at = datetime.now(UTC) - timedelta(hours=added_hours)
        if reminded_hours is not None:
            row.reminded_at = datetime.now(UTC) - timedelta(hours=reminded_hours)
        await session.commit()


async def test_reminder_never_repeats_by_default():
    """repeat_hours defaults to 0 — a reviewer is nudged once and then left alone, even
    once far more time has passed than the first deadline."""
    ado = _FakeAdo([_pr([_human(vote=0)])])
    svc, _, db = await _make(ado, _FakeFeedback())
    await svc._scan()
    await _overdue(db, added_hours=25)
    await svc._scan()
    assert len([c for c in ado.comments if "Nhắc review" in c]) == 1

    await _overdue(db, reminded_hours=240)  # ten days of silence
    await svc._scan()
    assert len([c for c in ado.comments if "Nhắc review" in c]) == 1


async def test_reminder_repeats_on_the_repeat_clock():
    ado = _FakeAdo([_pr([_human(vote=0)])])
    svc, _, db = await _make(ado, _FakeFeedback(), pr_reviewer_reminder_repeat_hours=6)
    await svc._scan()
    await _overdue(db, added_hours=25)
    await svc._scan()
    assert len([c for c in ado.comments if "Nhắc review" in c]) == 1

    await svc._scan()  # repeat window has NOT elapsed
    assert len([c for c in ado.comments if "Nhắc review" in c]) == 1

    await _overdue(db, reminded_hours=7)
    await svc._scan()
    reminders = [c for c in ado.comments if "Nhắc review" in c]
    assert len(reminders) == 2
    # The repeat must not claim they were "added over 24h ago" all over again.
    assert "vẫn chưa vote" in reminders[1]
    assert "6h" in reminders[1]


async def test_repeat_reminder_stops_once_they_vote():
    ado = _FakeAdo([_pr([_human(vote=0)])])
    svc, _, db = await _make(ado, _FakeFeedback(), pr_reviewer_reminder_repeat_hours=6)
    await svc._scan()
    await _overdue(db, added_hours=25)
    await svc._scan()
    assert len([c for c in ado.comments if "Nhắc review" in c]) == 1

    ado.prs[0]["reviewers"] = [_human(vote=10)]  # approved
    await _overdue(db, reminded_hours=99)
    await svc._scan()
    assert len([c for c in ado.comments if "Nhắc review" in c]) == 1


async def test_removed_reviewer_is_forgotten():
    ado = _FakeAdo([_pr([_human()])])
    svc, repo, _ = await _make(ado, _FakeFeedback())
    await svc._scan()
    assert await repo.reviewers_for_pr(7)

    ado.prs = [_pr([])]
    await svc._scan()
    assert await repo.reviewers_for_pr(7) == {}


def test_parse_verdict():
    parse = ReviewerTrackerService._parse_verdict
    assert parse("bla\nVERDICT: approve") == 10
    assert parse("VERDICT: suggestions") == 5
    assert parse("verdict: WAIT") == -5
    assert parse("no marker here") is None
    assert parse(None) is None
