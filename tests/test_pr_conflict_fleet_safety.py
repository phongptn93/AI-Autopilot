"""Conflict resolution across several machines, and after the PR moved on.

Three failures seen on a real PR: two machines opened two sessions on the same PR; a
session that ended after the PR was merged still announced "conflict needs a person";
and the Teams card for that notice printed "Work Item #0 / Unknown / unassigned" while
leaving out the one sentence that said what happened.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_autopilot.config import Settings
from ai_autopilot.data import Database, PrConflictRepository
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.notifications.base import NotificationMessage, NotificationType
from ai_autopilot.notifications.teams import TeamsNotifier
from ai_autopilot.services.pr_conflicts import PrConflictService


class _SharedPr:
    """One PR that several machines talk to — threads land in posting order."""

    def __init__(self, pr=None):
        self.threads: list[dict] = []
        self.pr = pr or {"status": "active", "mergeStatus": "conflicts"}

    def client(self):
        shared = self

        class _Ado:
            comments: list[str] = []

            async def get_pull_request_threads(self, repo_id, pr_id):
                return list(shared.threads)

            async def add_pull_request_comment(self, repo_id, pr_id, text, active=False):
                self.comments.append(text)
                shared.threads.append({"id": len(shared.threads) + 1, "comments": [
                    {"content": text,
                     "publishedDate": f"2099-01-01T00:00:{len(shared.threads):02d}Z"}]})
                return True

            async def get_pull_request(self, repo_id, pr_id):
                return dict(shared.pr)

        return _Ado()


async def _service(tmp_path, ado, name):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / f'{name}.sqlite'}")
    await db.create_all()

    async def _noop(*a, **kw):
        return None

    cfg = Settings(trigger_tag=f"{name}-autopilot", pr_conflict_claim_settle_seconds=0)
    c = SimpleNamespace(config=cfg, ado=ado, pr_conflict_repo=PrConflictRepository(db),
                        executor=SimpleNamespace(), audit_repo=SimpleNamespace(record=_noop),
                        notifier=SimpleNamespace(notify=_noop))
    svc = PrConflictService.__new__(PrConflictService)
    svc._c, svc._config = c, cfg
    svc._log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    svc._claiming = set()
    row, _ = await c.pr_conflict_repo.observe("r1", 4573, repo_name="app",
                                              source_branch="f", target_branch="main",
                                              files=["a.json"], target_commit="abc123")
    return svc, row, db


async def test_two_machines_on_one_pr_only_one_takes_it(tmp_path):
    pr = _SharedPr()
    a, row_a, db_a = await _service(tmp_path, pr.client(), "tram-a")
    b, row_b, db_b = await _service(tmp_path, pr.client(), "tram-b")
    try:
        assert await a._claim_pr(row_a, "t-abc123") is True
        assert await b._claim_pr(row_b, "t-abc123") is False      # sees A's live claim
        assert await a._claim_pr(row_a, "t-abc123") is True       # A keeps it, no re-post
        assert len(pr.threads) == 1
    finally:
        await db_a.dispose()
        await db_b.dispose()


async def test_a_simultaneous_claim_is_settled_by_the_earliest(tmp_path):
    """Both post before either reads back: the earlier claim wins, on both machines."""
    pr = _SharedPr()
    a, row_a, db_a = await _service(tmp_path, pr.client(), "tram-a")
    b, row_b, db_b = await _service(tmp_path, pr.client(), "tram-b")
    try:
        # B's claim lands first, as if A's read happened before B's post arrived.
        await b._c.ado.add_pull_request_comment(
            "r1", 4573, "<sub>autopilot-claim:t-abc123 · tram-b-autopilot</sub>")
        await a._c.ado.add_pull_request_comment(
            "r1", 4573, "<sub>autopilot-claim:t-abc123 · tram-a-autopilot</sub>")
        assert await a._claim_pr(row_a, "t-abc123") is False
        assert await b._claim_pr(row_b, "t-abc123") is True
    finally:
        await db_a.dispose()
        await db_b.dispose()


async def test_a_new_target_commit_is_a_new_claim(tmp_path):
    pr = _SharedPr()
    a, row_a, db_a = await _service(tmp_path, pr.client(), "tram-a")
    b, row_b, db_b = await _service(tmp_path, pr.client(), "tram-b")
    try:
        assert await a._claim_pr(row_a, "t-abc123")
        assert await b._claim_pr(row_b, "t-def456")
    finally:
        await db_a.dispose()
        await db_b.dispose()


async def test_unreadable_threads_mean_do_not_act(tmp_path):
    class _Down:
        async def get_pull_request_threads(self, *a):
            raise RuntimeError("ADO down")

    svc, row, db = await _service(tmp_path, _Down(), "tram-a")
    try:
        assert await svc._claim_pr(row, "t-abc123") is False
    finally:
        await db.dispose()


@pytest.mark.parametrize("pr,expected", [
    ({"status": "completed"}, "closed"),
    ({"status": "abandoned"}, "closed"),
    ({"status": "active", "mergeStatus": "succeeded"}, "resolved"),
    ({"status": "active", "mergeStatus": "conflicts"}, ""),
])
async def test_settled_elsewhere(tmp_path, pr, expected):
    svc, row, db = await _service(tmp_path, _SharedPr(pr).client(), "tram-a")
    try:
        assert await svc._settled_elsewhere(row) == expected
    finally:
        await db.dispose()


async def test_a_failure_after_the_pr_was_merged_tells_nobody(tmp_path):
    shared = _SharedPr({"status": "completed"})
    ado = shared.client()
    svc, row, db = await _service(tmp_path, ado, "tram-a")
    notices: list = []

    async def capture(msg):
        notices.append(msg)

    svc._c.notifier = SimpleNamespace(notify=capture)

    async def _close(*a, **k):
        return None

    svc._close_execution = _close
    try:
        await svc._finish_failed(row, "phiên quá 8 giờ", {}, 0)
        row = await svc._c.pr_conflict_repo.get(row.id)
        assert row.status == "closed"
        assert notices == [] and ado.comments == []
    finally:
        await db.dispose()


def test_a_notice_without_a_work_item_shows_its_words_not_empty_item_fields():
    card = TeamsNotifier._payload(NotificationMessage(
        work_item=WorkItemInfo(id=0, title="feat(dxcmms): Cấu hình chu kì năm"),
        type=NotificationType.REMINDER,
        heading="🙋 PR !4573 — conflict cần người giải",
        text="app: f → main. phiên quá 8 giờ",
    ))
    body = str(card)
    assert "Work Item" not in body and "Category" not in body and "unassigned" not in body
    assert "phiên quá 8 giờ" in body and "Cấu hình chu kì năm" in body


def test_a_work_item_notice_keeps_its_fields():
    card = TeamsNotifier._payload(NotificationMessage(
        work_item=WorkItemInfo(id=9083, title="Xuất tồn kho"), type=NotificationType.STARTED))
    body = str(card)
    assert "Work Item" in body and "#9083" in body
