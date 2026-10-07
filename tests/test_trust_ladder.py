"""Earned autonomy: the ladder's rules, its repository reads, and the poller's use of it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ai_autopilot import trust
from ai_autopilot.data import PipelineState
from ai_autopilot.data.database import Database
from ai_autopilot.data.entities import QualityKind
from ai_autopilot.data.repository import (
    ExecutionRepository,
    QualityRepository,
    SyncStateRepository,
)
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.trust import TrustRun, compute_level
from tests.test_poller_agent import _poller


def _good(i: int = 1) -> TrustRun:
    return TrustRun(work_item_id=i, succeeded=True, had_pr=True, merged=True, setback=False)


def _bad(i: int = 1) -> TrustRun:
    return TrustRun(work_item_id=i, succeeded=False, had_pr=False, merged=False, setback=False)


def _pending(i: int = 1) -> TrustRun:
    return TrustRun(work_item_id=i, succeeded=True, had_pr=True, merged=False, setback=False)


# ── pure rules ─────────────────────────────────────────────────────────────────


def test_a_new_scope_starts_at_draft_prs_not_at_the_top():
    level, reason = compute_level([], ceiling=3, max_level=3)
    assert level == 1 and "chưa đủ" in reason


def test_ten_clean_merges_earn_one_rung():
    level, reason = compute_level([_good()] * 10, ceiling=2, max_level=2)
    assert level == 2 and "lên mức 2" in reason


def test_nine_are_not_enough():
    assert compute_level([_good()] * 9, ceiling=2, max_level=2)[0] == 1


def test_the_rate_must_reach_the_threshold():
    runs = [_good()] * 7 + [_bad(), _good(), _good()]   # 9/10 >= 0.8
    assert compute_level(runs, ceiling=2, max_level=2)[0] == 2
    mostly_bad = [_good(), _bad(), _good(), _bad(), _good(), _bad(), _good(), _bad(),
                  _good(), _bad()]
    assert compute_level(mostly_bad, ceiling=2, max_level=2)[0] == 1


def test_two_bad_in_a_row_cost_a_rung():
    runs = [_good()] * 10 + [_bad(), _bad()]
    level, reason = compute_level(runs, ceiling=2, max_level=2)
    assert level == 1 and "hạ xuống" in reason


def test_one_bad_run_alone_is_forgiven():
    runs = [_good()] * 10 + [_bad(), _good()]
    assert compute_level(runs, ceiling=2, max_level=2)[0] == 2


def test_an_open_pr_neither_proves_nor_disproves():
    # Nine merged + many still open: not ten decided runs, so no promotion yet.
    runs = [_good()] * 9 + [_pending()] * 5
    assert compute_level(runs, ceiling=2, max_level=2)[0] == 1


def test_the_machine_setting_is_the_ceiling_and_max_level_caps_below_it():
    many = [_good()] * 40
    assert compute_level(many, ceiling=trust.ceiling_for("unattended"), max_level=2)[0] == 2
    assert compute_level(many, ceiling=trust.ceiling_for("unattended"), max_level=3)[0] == 3
    assert compute_level(many, ceiling=trust.ceiling_for("assisted"), max_level=3)[0] == 2
    assert compute_level(many, ceiling=trust.ceiling_for("report"), max_level=3)[0] == 0


def test_a_demoted_scope_can_climb_back_with_plans():
    # At rung 0 runs are plans (no PR): a successful one is good, so the scope recovers.
    plan = TrustRun(work_item_id=1, succeeded=True, had_pr=False, merged=False, setback=False)
    runs = [_bad(), _bad()] + [plan] * 10
    assert compute_level(runs, ceiling=2, max_level=2)[0] == 1


def test_a_setback_after_the_run_makes_it_bad():
    reworked = TrustRun(work_item_id=1, succeeded=True, had_pr=True, merged=True, setback=True)
    assert trust.judge(reworked) == trust.BAD


def test_rungs_map_to_executor_arguments():
    assert trust.rung(0).autonomy == "report"
    assert (trust.rung(1).autonomy, trust.rung(1).draft_pr) == ("assisted", True)
    assert (trust.rung(2).autonomy, trust.rung(2).draft_pr, trust.rung(2).review_first) == (
        "assisted", False, True)
    assert (trust.rung(3).autonomy, trust.rung(3).review_first) == ("unattended", False)


def test_build_runs_attributes_a_setback_only_to_runs_before_it():
    t0 = datetime(2026, 1, 1)
    rows = [
        SimpleNamespace(work_item_id=5, status=SimpleNamespace(value="Success"),
                        started_at=t0, pr_url="u"),
        SimpleNamespace(work_item_id=5, status=SimpleNamespace(value="Success"),
                        started_at=t0 + timedelta(days=2), pr_url="u"),
    ]
    runs = trust.build_runs(rows, {5}, [(5, t0 + timedelta(days=1))])
    assert [r.setback for r in runs] == [True, False]
    assert all(r.merged for r in runs)


# ── repository reads ───────────────────────────────────────────────────────────


async def test_level_for_reads_the_scope_from_the_database(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    await db.create_all()
    execs, sync, quality = ExecutionRepository(db), SyncStateRepository(db), QualityRepository(db)
    for n in range(12):
        item = WorkItemInfo(id=100 + n, title="x", project="P")
        rid = await execs.start_execution(item, "agent")
        r = ExecutionResult.ok(item.id, "agent", "done")
        r.pr_url = f"https://x/_git/R/pullrequest/{n}"
        await execs.complete_execution(rid, r)
        await sync.mark_merged_pr(n + 1, item.id, "Done")
    # Another project's runs must not count.
    other = WorkItemInfo(id=999, title="x", project="Other")
    await execs.complete_execution(await execs.start_execution(other, "agent"),
                                   ExecutionResult.fail(999, "agent", "x"))

    rows = await execs.scope_history("P", str(WorkItemInfo(id=1).category))
    assert len(rows) == 12 and rows[0].work_item_id == 100   # oldest first
    assert await sync.merged_work_item_ids({100, 101, 999}) == {100, 101}

    c = SimpleNamespace(
        config=SimpleNamespace(autonomy_level="assisted", trust_max_level=2,
                               trust_min_runs=10, trust_promote_rate=0.8),
        execution_repo=execs, sync_repo=sync, quality_events=quality,
    )
    probe = WorkItemInfo(id=1, title="x", project="P")
    assert (await trust.level_for(c, probe))[0] == 2

    # A rejection on the newest item after its run turns it bad; with the previous one
    # sent back for a revision too, that is two in a row → one rung down.
    await quality.record(work_item_id=111, kind=QualityKind.REVIEW_VOTE, value=-10)
    await quality.record(work_item_id=110, kind=QualityKind.PR_REVISION, value=1)
    await quality.record(work_item_id=109, kind=QualityKind.REVIEW_VOTE, value=10)  # approval
    await quality.record(work_item_id=109, kind=QualityKind.SDLC_ITERATION, value=1)
    assert {i for i, _ in await quality.setbacks_for_items({109, 110, 111})} == {110, 111}
    level, reason = await trust.level_for(c, probe)
    assert level == 1 and "hạ" in reason


# ── poller wiring ─────────────────────────────────────────────────────────────


class _Repo:
    """execution_repo + sync_repo + quality_events doubles for level_for."""

    def __init__(self, runs: int):
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        self.rows = [
            SimpleNamespace(work_item_id=i, status=SimpleNamespace(value="Success"),
                            started_at=t0 + timedelta(hours=i), pr_url="u")
            for i in range(runs)
        ]

    async def scope_history(self, project, category, limit=200):
        return self.rows

    async def merged_work_item_ids(self, ids):
        return set(ids)

    async def setbacks_for_items(self, ids):
        return []


class _Exec:
    def __init__(self, result):
        self.calls: list[dict] = []
        self._result = result

    async def run_agent(self, item, **kw):
        self.calls.append(kw)
        return self._result


async def _run_headless(svc, c, item):
    c.router = SimpleNamespace(classify=lambda i: i)
    c.plugins = SimpleNamespace(run_post_processors=_noop)
    await svc._process_agent(item, item)


async def _noop(*_a, **_k):
    return None


async def test_an_earned_rung_reaches_the_executor_and_the_outcome():
    svc, c = _poller(trust_ladder_enabled=True, execution_mode="headless")
    repo = _Repo(10)
    c.execution_repo.scope_history = repo.scope_history
    c.sync_repo = repo
    c.quality_events = repo
    result = ExecutionResult.ok(7, "agent", "done")
    result.pr_url = "https://dev.azure.com/o/P/_git/R/pullrequest/1"
    c.executor = _Exec(result)
    item = WorkItemInfo(id=7, title="t", work_item_type="Task")

    await _run_headless(svc, c, item)

    assert c.executor.calls == [{"autonomy": "assisted", "draft_pr": False}]
    assert trust.TRUST_LEVEL_KIND in c.quality_repo.kinds()
    # Rung 2 still waits for a person: review, not done — and says it is not a draft.
    assert (7, PipelineState.IN_REVIEW) in c.state_repo.calls
    assert any("ready for review" in text for _i, text in c.ado.comments)


async def test_a_broken_history_read_falls_back_to_the_configured_autonomy():
    # The fake repositories have no scope_history at all.
    svc, c = _poller(trust_ladder_enabled=True, execution_mode="headless")
    c.executor = _Exec(ExecutionResult.ok(7, "agent", "done"))
    await _run_headless(svc, c, WorkItemInfo(id=7, title="t", work_item_type="Task"))
    assert c.executor.calls == [{"autonomy": "assisted", "draft_pr": True}]


async def test_ladder_off_changes_nothing():
    svc, c = _poller(execution_mode="headless")
    c.executor = _Exec(ExecutionResult.ok(7, "agent", "done"))
    await _run_headless(svc, c, WorkItemInfo(id=7, title="t", work_item_type="Task"))
    assert c.executor.calls == [{"autonomy": "assisted", "draft_pr": True}]
    assert trust.TRUST_LEVEL_KIND not in c.quality_repo.kinds()


async def test_a_level_change_is_logged_once():
    svc, c = _poller(trust_ladder_enabled=True)
    lines: list[dict] = []
    svc._log = SimpleNamespace(info=lambda msg, **kw: lines.append({"msg": msg, **kw}),
                               warning=lambda *a, **k: None, error=lambda *a, **k: None)
    item = WorkItemInfo(id=7, title="t", project="P")
    await svc._note_trust(item, 1, "r")
    await svc._note_trust(item, 1, "r")
    await svc._note_trust(item, 2, "r")
    assert [ln["level"] for ln in lines if ln["msg"] == "trust level"] == [1, 2]
