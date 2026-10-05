"""Cases that wait for a deploy: reported as waiting, handed to QC once deployed.

The case that motivated it: a dev run on a TLLA item passed 3/3 runnable cases and
listed a 4th — "runtime verification against a tenant DB" — that no dev environment can
run before the change is deployed. It was counted as "1/4 chưa chạy được", held the item
as "no verdict", and asked QC to verify on an environment that did not have the build.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from ai_autopilot import doctor, test_report
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.data import Database, DeferredVerificationRepository
from ai_autopilot.execution.result_contract import CaseOutcome, _normalise_outcome
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.deferred_verification import (
    HANDOFF,
    UNVERIFIED,
    DeferredVerificationService,
    decide,
    has_trigger,
)

_TENANT_CASE = CaseOutcome(
    title="Runtime verification against a tenant DB / browser",
    outcome="pending_deploy",
    note="Enable the setting on TLLA, then check the report totals.",
)


def _tlla_run() -> list[CaseOutcome]:
    return [
        CaseOutcome(title="Setting off keeps old totals", outcome="pass"),
        CaseOutcome(title="Setting on groups by lot", outcome="pass"),
        CaseOutcome(title="Migration is idempotent", outcome="pass"),
        _TENANT_CASE,
    ]


# ── contract ────────────────────────────────────────────────────────────────
def test_the_words_models_write_for_waiting_on_a_deploy_are_understood():
    for word in ("pending_deploy", "Pending deploy", "awaiting deploy", "chờ deploy"):
        assert _normalise_outcome(word) == "pending_deploy", word


def test_not_running_is_still_blocked_not_deferred():
    """Deferring is a claim about the environment; a vague word must not make it."""
    for word in ("skipped", "not run", "pending", "n/a", ""):
        assert _normalise_outcome(word) == "blocked", word


# ── the run's comment ───────────────────────────────────────────────────────
def test_a_deferred_case_is_not_a_partial_failure():
    report = test_report.render_comment(_tlla_run())
    assert (report.total, report.passed, report.blocked, report.pending) == (3, 3, 0, 1)
    assert "3/3 đạt" in report.html and "1 chờ deploy" in report.html
    assert "chưa chạy được</b>" not in report.html          # no "1/4 chưa chạy được"
    assert "Chờ deploy để kiểm tra" in report.html
    assert "Enable the setting on TLLA" in report.html      # the precondition survives


def test_a_run_with_only_deferred_cases_still_reports():
    report = test_report.render_comment([_TENANT_CASE])
    assert not report.is_empty
    assert report.total == 0 and report.pending == 1
    assert "Chưa có case nào chạy được trước khi deploy" in report.html


def test_a_real_block_is_still_reported_as_one():
    report = test_report.render_comment(
        [CaseOutcome(title="E2E login", outcome="blocked", note="browser crashed"),
         _TENANT_CASE]
    )
    assert report.blocked == 1 and report.pending == 1
    assert "1/1 chưa chạy được" in report.html


# ── when to hand off ────────────────────────────────────────────────────────
_CFG = Settings(board_testing_state=["Ready for Testing"], done_states=["Closed"])


def _item(state: str, **kw) -> WorkItemInfo:
    return WorkItemInfo(id=9001, title="Tính tồn theo lô", state=state, project="P",
                        work_item_type="Task", **kw)


def test_decide_waits_while_the_build_is_not_out():
    assert decide(_CFG, _item("Active")) is None
    assert decide(_CFG, _item("Ready for Review")) is None


def test_decide_hands_off_in_a_testing_or_deployed_state():
    assert decide(_CFG, _item("Ready for Testing")) == HANDOFF
    assert decide(_CFG, _item("ready for testing")) == HANDOFF        # ADO case drift
    deployed = Settings(on_deploy_state="Deployed", done_states=["Closed"])
    assert decide(deployed, _item("Deployed")) == HANDOFF


def test_decide_warns_when_the_item_closes_first():
    assert decide(_CFG, _item("Closed")) == UNVERIFIED


# ── end to end, on a real database ──────────────────────────────────────────
class _Tracker:
    def __init__(self, state: str) -> None:
        self.state = state
        self.comments: list[str] = []
        self.fail_comment = False

    async def get_work_items_by_ids(self, ids):
        return [_item(self.state)] if 9001 in ids else []

    async def add_comment(self, work_item_id, comment):
        if self.fail_comment:
            return False
        self.comments.append(comment)
        return True


async def _service(tmp_path, tracker, cfg=_CFG):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    await db.create_all()
    repo = DeferredVerificationRepository(db)
    return DeferredVerificationService(repo, cfg, lambda _project: tracker), repo, db


async def test_deferred_cases_reach_qc_only_once_the_item_is_deployed(tmp_path):
    tracker = _Tracker("Active")
    svc, repo, db = await _service(tmp_path, tracker)
    assert await svc.record(_item("Active"), _tlla_run()) == 1     # only the deferred one

    assert await svc.reconcile() == 0                # still Active: nothing to hand off
    assert tracker.comments == []

    tracker.state = "Ready for Testing"
    assert await svc.reconcile() == 1
    assert len(tracker.comments) == 1
    body = tracker.comments[0]
    assert test_report.HANDOFF_PREFIX in body and "Ready for Testing" in body
    assert "Enable the setting on TLLA" in body

    assert await svc.reconcile() == 0                # handed off once, not every cycle
    assert len(tracker.comments) == 1
    [row] = await repo.for_item(9001)
    assert row.status == repo.HANDED_OFF and row.released_state == "Ready for Testing"
    await db._engine.dispose()


async def test_a_rerun_does_not_list_the_same_case_twice(tmp_path):
    svc, repo, db = await _service(tmp_path, _Tracker("Active"))
    await svc.record(_item("Active"), _tlla_run())
    refreshed = CaseOutcome(title=_TENANT_CASE.title.upper(), outcome="pending_deploy",
                            note="Enable it on TLLA and on TLCL.")
    assert await svc.record(_item("Active"), [refreshed]) == 0
    [row] = await repo.pending_for(9001)
    assert row.note == "Enable it on TLLA and on TLCL."         # newest wording wins
    await db._engine.dispose()


async def test_a_hand_off_that_fails_to_post_is_retried(tmp_path):
    tracker = _Tracker("Ready for Testing")
    tracker.fail_comment = True
    svc, repo, db = await _service(tmp_path, tracker)
    await svc.record(_item("Active"), [_TENANT_CASE])

    assert await svc.reconcile() == 0
    assert len(await repo.pending_for(9001)) == 1               # not lost

    tracker.fail_comment = False
    assert await svc.reconcile() == 1
    assert len(tracker.comments) == 1
    await db._engine.dispose()


async def test_closing_with_unverified_cases_is_said_out_loud(tmp_path):
    tracker = _Tracker("Closed")
    svc, repo, db = await _service(tmp_path, tracker)
    await svc.record(_item("Active"), [_TENANT_CASE])

    assert await svc.reconcile() == 1
    assert test_report.UNVERIFIED_PREFIX in tracker.comments[0]
    [row] = await repo.for_item(9001)
    assert row.status == repo.UNVERIFIED
    await db._engine.dispose()


async def test_dry_run_records_but_never_comments(tmp_path):
    tracker = _Tracker("Ready for Testing")
    cfg = Settings(dry_run=True, board_testing_state=["Ready for Testing"])
    svc, repo, db = await _service(tmp_path, tracker, cfg)
    await svc.record(_item("Active"), [_TENANT_CASE])
    assert await svc.reconcile() == 0
    assert tracker.comments == [] and len(await repo.pending_for(9001)) == 1
    await db._engine.dispose()


# ── configuration and display ───────────────────────────────────────────────
def test_doctor_warns_when_nothing_can_release_a_deferred_case():
    assert not has_trigger(Settings())
    [finding] = doctor.check_deferred_handoff(Settings())
    assert finding.level == doctor.WARN
    assert has_trigger(Settings(board_testing_state=["Ready for Testing"]))
    [ok] = doctor.check_deferred_handoff(Settings(on_deploy_state="Deployed"))
    assert ok.level == doctor.OK


def test_the_task_room_lists_cases_waiting_for_a_deploy(tmp_path):
    settings = Settings(dry_run=True,
                        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    app = create_app(settings)
    with TestClient(app) as client:
        repo = app.state.container.deferred_repo
        client.portal.call(repo.add, _item("Active"), [_TENANT_CASE])
        page = client.get("/dashboard/task/9001")
    assert page.status_code == 200
    assert "Xác minh sau deploy" in page.text
    assert "chờ deploy" in page.text and "Enable the setting on TLLA" in page.text
