"""A QC verdict has to have authority over the pipeline.

The reported symptom was narrower than the defect. It was filed as "a failing case does
not create a Bug on ADO", and that is true — nothing on the run path calls
create_work_item with a Bug type. But a failing case did not change ANYTHING:

  * `_report_test_results` posted a comment and returned;
  * `ScoreInput` had no field for executed test cases, so the score — and therefore the
    gate that already holds weak runs for a human — could not see the verdict;
  * the run then went down the success branch, tagged the item `autopilot-done`, and the
    autopilot never looked at it again.

So a QC run that found a real defect ended with the item marked handled and the only
record of the defect a rendered comment somebody had to read. Filing a Bug on top of
that would have produced an open Bug attached to an item the automation had already
written off — traceability restored while the workflow still lied.

The fix gives the verdict authority through machinery that already exists (the score
gate) rather than adding a parallel one, and records the counts as DATA so "which items
have a failing case right now" is a question something can answer.
"""

from __future__ import annotations

from ai_autopilot.execution.pr_scorer import ScoreInput, score_run

# A QC role: it does its job without opening a pull request, so the rubric is told not
# to expect one. This is the shape that used to sail through.
QC = {"completed": True, "has_pr": False, "files_changed": 0, "expected_pr": False}


def test_a_clean_qc_run_is_not_held():
    """The control. Holding every QC run would be its own defect."""
    score = score_run(ScoreInput(**QC, tests_failed=0, tests_blocked=0))
    assert score.gate != "escalate"


def test_one_failed_case_holds_the_run_for_a_human():
    """Case #7038 — a wrong total, 'Chênh lệch +131,6%'. Found, reported, and then the
    item was tagged handled anyway."""
    score = score_run(ScoreInput(**QC, tests_failed=1))
    assert score.gate == "escalate"
    assert any("KHÔNG ĐẠT" in r for r in score.reasons)


def test_a_blocked_case_holds_it_too():
    """A case that could not be run is not a case that passed — the same reading the
    comment renderer already uses."""
    score = score_run(ScoreInput(**QC, tests_blocked=2))
    assert score.gate == "escalate"
    assert any("chưa chạy được" in r for r in score.reasons)


def test_the_score_is_not_lowered_by_finding_a_defect():
    """The score asks "how well did this run go"; the gate asks "must a human look".
    Conflating them was the original mistake. A QC run that finds a real defect went
    WELL — marking it down would penalise the agent for doing its job."""
    clean = score_run(ScoreInput(**QC))
    found = score_run(ScoreInput(**QC, tests_failed=3))
    assert found.score == clean.score
    assert found.gate == "escalate" and clean.gate != "escalate"


def test_a_failing_case_outranks_an_otherwise_perfect_run():
    """Even a run that would have gated 'auto' is held. There is no score high enough to
    earn passage with a known failing case."""
    perfect = ScoreInput(
        completed=True, has_pr=True, files_changed=9,
        review_passed=True, ci_passed=True, tests_failed=1,
    )
    assert score_run(perfect).gate == "escalate"
    assert score_run(ScoreInput(
        completed=True, has_pr=True, files_changed=9,
        review_passed=True, ci_passed=True,
    )).gate == "auto"


def test_no_executed_cases_changes_nothing():
    """Most runs are not QC runs. Defaults of 0 must leave the existing rubric alone."""
    dev = ScoreInput(completed=True, has_pr=True, files_changed=4,
                     review_passed=True, ci_passed=True)
    assert score_run(dev).gate == score_run(
        ScoreInput(completed=True, has_pr=True, files_changed=4,
                   review_passed=True, ci_passed=True, tests_failed=0, tests_blocked=0)
    ).gate


def test_the_reason_travels_so_the_reader_is_not_sent_to_the_score():
    """The held-for-human comment used to say "điểm dưới ngưỡng review" unconditionally.
    For a verdict hold that is untrue — the score is fine — and it sends whoever opens
    the item to look at the wrong number."""
    score = score_run(ScoreInput(**QC, tests_failed=2, tests_blocked=1))
    joined = "; ".join(score.reasons)
    assert "2 test case KHÔNG ĐẠT" in joined
    assert "1 test case chưa chạy được" in joined


# ── The verdict as data, not as rendered HTML ────────────────────────────────

def test_the_run_record_carries_the_counts(tmp_path):
    """"Which items have a failing test case right now" had no answer: the numbers
    existed only inside an ADO comment. Nothing could query them — not the dashboard,
    not a report, not an alert."""
    import asyncio
    from types import SimpleNamespace

    from ai_autopilot.data import Database, ExecutionRepository
    from ai_autopilot.models import WorkItemInfo
    from ai_autopilot.models.execution import ExecutionResult

    async def go():
        db = Database(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
        await db.create_all()          # also proves the column migration applies
        repo = ExecutionRepository(db)
        item = WorkItemInfo(id=7038, title="Báo cáo chênh lệch",
                            work_item_type="Task", state="Active", tags=[])
        record_id = await repo.start_execution(item, skill="qc", trigger_tag="t")
        result = ExecutionResult(work_item_id=7038, success=True, output="done")
        result.test_results = [
            SimpleNamespace(outcome="pass"),
            SimpleNamespace(outcome="fail"),      # #7038: sai tổng Chênh lệch +131,6%
            SimpleNamespace(outcome="blocked"),
        ]
        await repo.complete_execution(record_id, result)
        rows = await repo.get_by_work_item(7038)
        await db.dispose()
        return rows[0]

    row = asyncio.run(go())
    assert (row.tests_total, row.tests_failed, row.tests_blocked) == (3, 1, 1)


# ── Filing the Bug ───────────────────────────────────────────────────────────
#
# Off by default, because a failing case is not always a product defect — it is just as
# often a wrong test, stale data or a broken environment, and those Bugs land on a board
# somebody has to clean. On, it is the right answer for a process that requires
# Requirement → TC + Bug, where a defect living only in a comment counts as not found.


class _FakeAdo:
    """Just enough ADO to exercise _file_bugs. ``refuse_child`` reproduces the process
    templates that reject Bug-under-Task outright."""

    def __init__(self, children=(), related_items=(), refuse_child=False):
        self._children = list(children)
        self._related = list(related_items)
        self.refuse_child = refuse_child
        self.created: list[tuple[str, str]] = []      # (title, link kind)

    async def get_children(self, _parent_id):
        return list(self._children)

    async def get_work_item_links(self, ids):
        return ({}, {ids[0]: {w.id for w in self._related}} if self._related else {})

    async def get_work_items_by_ids(self, ids):
        return [w for w in self._related if w.id in set(ids)]

    async def create_bug(self, *, title, description, parent_id, repro_steps="",
                         tag="", project=""):
        link = "related" if self.refuse_child else "child"
        self.created.append((title, link))
        return (9000 + len(self.created), link)


def _case(title: str, outcome: str = "fail", note: str = ""):
    from types import SimpleNamespace
    return SimpleNamespace(title=title, outcome=outcome, note=note)


def _bug(item_id: int, title: str):
    from ai_autopilot.models import WorkItemInfo
    return WorkItemInfo(id=item_id, title=title, work_item_type="Bug",
                        state="New", tags=[])


def _poller(ado, **over):
    """A Poller with only what _file_bugs touches — the rest of the service is not
    under test here and wiring it would test the wiring instead."""
    import asyncio
    from types import SimpleNamespace

    from ai_autopilot.config import Settings
    from ai_autopilot.services.poller import AdoPollerService

    cfg = Settings(**{"qc_create_bug_items": True, "dry_run": False, **over})
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    svc._provider = lambda _project=None: ado
    return svc, asyncio


def _run(svc, asyncio_mod, item, result):
    return asyncio_mod.run(svc._file_bugs(item, result))


def _item():
    from ai_autopilot.models import WorkItemInfo
    return WorkItemInfo(id=7038, title="Báo cáo chênh lệch", work_item_type="Task",
                        state="Active", tags=[])


def _result(cases):
    from ai_autopilot.models.execution import ExecutionResult
    r = ExecutionResult(work_item_id=7038, success=True, output="")
    r.test_results = list(cases)
    return r


def test_one_bug_per_failing_case():
    """One per case, not one listing them all: they are fixed by different people at
    different times, and a single Bug holding nine defects is closed when the easiest
    one is done."""
    ado = _FakeAdo()
    svc, aio = _poller(ado)
    filed = _run(svc, aio, _item(), _result([
        _case("Tổng Chênh lệch sai", note="+131,6%"),
        _case("Cột Ngày trống"),
        _case("Xuất Excel đúng", outcome="pass"),
    ]))
    assert filed == 2
    assert [t for t, _ in ado.created] == ["[QC] Tổng Chênh lệch sai", "[QC] Cột Ngày trống"]


def test_a_blocked_case_does_not_become_a_bug():
    """Blocked means QC could not reach a verdict. Filing a Bug for it asserts a defect
    nobody has established — the run is still held, which is the honest answer."""
    ado = _FakeAdo()
    svc, aio = _poller(ado)
    assert _run(svc, aio, _item(), _result([_case("Chưa chạy được", outcome="blocked")])) == 0
    assert ado.created == []


def test_re_running_does_not_file_the_bug_twice():
    """The whole reason this reads the item's links instead of trusting a flag."""
    ado = _FakeAdo(children=[_bug(9001, "[QC] Tổng Chênh lệch sai")])
    svc, aio = _poller(ado)
    assert _run(svc, aio, _item(), _result([_case("Tổng Chênh lệch sai")])) == 0
    assert ado.created == []


def test_a_bug_linked_as_related_also_counts_as_already_filed():
    """Where the template refused the child link the Bug lands as Related. Reading only
    children would miss it and file a second one on every re-run."""
    ado = _FakeAdo(related_items=[_bug(9002, "[QC] Tổng Chênh lệch sai")])
    svc, aio = _poller(ado)
    assert _run(svc, aio, _item(), _result([_case("Tổng Chênh lệch sai")])) == 0


def test_two_failing_cases_with_the_same_title_file_one_bug():
    """The guard has to hold WITHIN a run too, not only across runs."""
    ado = _FakeAdo()
    svc, aio = _poller(ado)
    assert _run(svc, aio, _item(), _result([
        _case("Tổng Chênh lệch sai"), _case("Tổng Chênh lệch sai"),
    ])) == 1


def test_the_link_falls_back_when_the_template_refuses_a_child():
    """Bug-under-Task is refused about as widely as Test-Case-under-Requirement, which
    create_test_case already ships the lesson for. A refused link costs the whole work
    item, not just the link — so an unlinkable Bug must still be filed."""
    ado = _FakeAdo(refuse_child=True)
    svc, aio = _poller(ado)
    assert _run(svc, aio, _item(), _result([_case("Tổng Chênh lệch sai")])) == 1
    assert ado.created[0][1] == "related"


def test_nothing_is_filed_while_the_flag_is_off():
    ado = _FakeAdo()
    svc, aio = _poller(ado, qc_create_bug_items=False)
    assert _run(svc, aio, _item(), _result([_case("Tổng Chênh lệch sai")])) == 0
    assert ado.created == []


def test_nothing_is_filed_in_dry_run():
    ado = _FakeAdo()
    svc, aio = _poller(ado, dry_run=True)
    assert _run(svc, aio, _item(), _result([_case("Tổng Chênh lệch sai")])) == 0


def test_an_unreadable_link_list_does_not_stop_the_filing():
    """If the duplicate check cannot run, a missing Bug is the worse failure — but say
    so is not possible here, so it errs towards filing."""
    class _Broken(_FakeAdo):
        async def get_children(self, _parent_id):
            raise RuntimeError("ADO unreachable")

        async def get_work_item_links(self, ids):
            raise RuntimeError("ADO unreachable")

    ado = _Broken()
    svc, aio = _poller(ado)
    assert _run(svc, aio, _item(), _result([_case("Tổng Chênh lệch sai")])) == 1
