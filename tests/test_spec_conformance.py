"""AC coverage on a run: cases name their criteria, the score reads the coverage."""

from __future__ import annotations

from ai_autopilot import spec_readiness
from ai_autopilot.execution import result_contract as rc
from ai_autopilot.execution.pr_scorer import ScoreInput, score_run
from ai_autopilot.execution.result_contract import (
    CaseOutcome,
    _parse_case_outcomes,
    _parse_test_cases,
)
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.services.poller import AdoPollerService as Poller

AC_HTML = "<ol><li>Khi bấm Lưu thì hiển thị OK</li><li>Khi nhập sai thì báo lỗi</li><li>Khi xoá thì hỏi lại</li></ol>"


def _item(ac: str | None = AC_HTML) -> WorkItemInfo:
    return WorkItemInfo(id=1, title="t", acceptance_criteria=ac)


def _result(cases=(), outcomes=()) -> ExecutionResult:
    r = ExecutionResult.ok(1, "agent", "done")
    r.test_cases = list(cases)
    r.test_results = list(outcomes)
    return r


def _score_input(**kw) -> ScoreInput:
    base = {"completed": True, "has_pr": True, "files_changed": 3, "ci_passed": True,
            "review_passed": True}
    base.update(kw)
    return ScoreInput(**base)


# ── the cases name their criteria ──────────────────────────────────────────────


def test_a_case_keeps_the_criteria_it_names():
    [c] = _parse_test_cases([{"title": "Lưu", "ac": "AC-01, AC-02"}])
    assert c.ac == "AC-01, AC-02"


def test_criteria_written_as_a_list_or_under_a_nearby_name_are_still_read():
    """Models write this field as a string, a list, or from memory under another name;
    all of them mean the same thing and none should be lost to its shape."""
    assert _parse_test_cases([{"title": "a", "ac": ["AC-01", "AC-03"]}])[0].ac == "AC-01, AC-03"
    assert _parse_test_cases([{"title": "a", "acs": "AC-02"}])[0].ac == "AC-02"
    assert _parse_test_cases([{"title": "a", "acceptance_criteria": ["AC-04"]}])[0].ac == "AC-04"


def test_a_case_without_criteria_is_still_a_case():
    [c] = _parse_test_cases([{"title": "Lưu"}, "chỉ có tiêu đề"][:1])
    assert c.title == "Lưu" and c.ac == ""
    assert _parse_test_cases(["chỉ có tiêu đề"])[0].ac == ""


def test_a_result_can_name_its_criteria_too():
    [o] = _parse_case_outcomes([{"title": "Lưu", "outcome": "pass", "ac": "AC-01"}])
    assert (o.outcome, o.ac) == ("pass", "AC-01")


# ── numbering the item's own criteria ──────────────────────────────────────────


def test_an_unnamed_list_is_numbered_by_position():
    acs = spec_readiness.numbered_acs_from_html(AC_HTML)
    assert [a["id"] for a in acs] == ["AC-01", "AC-02", "AC-03"]
    assert acs[1]["text"].startswith("Khi nhập sai")


def test_a_named_criterion_keeps_its_own_id_and_the_rest_count_by_position():
    acs = spec_readiness.numbered_acs_from_html(
        "<ul><li>AC-07: Khi a thì b</li><li>Khi c thì d</li></ul>")
    assert [a["id"] for a in acs] == ["AC-07", "AC-02"]


def test_an_empty_or_missing_field_gives_no_criteria():
    assert spec_readiness.numbered_acs_from_html("") == []
    assert spec_readiness.numbered_acs_from_html(None) == []  # type: ignore[arg-type]


# ── the poller's reading of a run ──────────────────────────────────────────────


def test_coverage_counts_criteria_with_a_passing_case():
    r = _result(
        cases=[rc.TestCase(title="Lưu OK", ac="AC-01"), rc.TestCase(title="Sai", ac="AC-02")],
        outcomes=[CaseOutcome(title="Lưu OK", outcome="pass"),
                  CaseOutcome(title="Sai", outcome="pass")],
    )
    assert abs(Poller._spec_conformance(_item(), r) - 2 / 3) < 1e-9


def test_a_failing_case_does_not_count_as_coverage():
    r = _result(
        cases=[rc.TestCase(title="Lưu OK", ac="AC-01"), rc.TestCase(title="Sai", ac="AC-02")],
        outcomes=[CaseOutcome(title="Lưu OK", outcome="pass"),
                  CaseOutcome(title="Sai", outcome="fail")],
    )
    assert abs(Poller._spec_conformance(_item(), r) - 1 / 3) < 1e-9


def test_the_case_and_its_result_are_joined_on_title_ignoring_case():
    r = _result(cases=[rc.TestCase(title="Lưu OK", ac="AC-01")],
                outcomes=[CaseOutcome(title="  lưu ok ", outcome="pass")])
    assert abs(Poller._spec_conformance(_item(), r) - 1 / 3) < 1e-9


def test_a_result_that_names_the_criteria_itself_is_enough():
    r = _result(outcomes=[CaseOutcome(title="x", outcome="pass", ac="AC-03")])
    assert abs(Poller._spec_conformance(_item(), r) - 1 / 3) < 1e-9


def test_not_measured_is_none_never_zero():
    """No criteria on the item, or no cases in the run, means "unknown" — reporting 0%
    would hold every non-QC run for a coverage it was never asked to have."""
    assert Poller._spec_conformance(None, _result()) is None
    assert Poller._spec_conformance(_item(None), _result(cases=[rc.TestCase(title="a")])) is None
    assert Poller._spec_conformance(_item(""), _result(cases=[rc.TestCase(title="a")])) is None
    assert Poller._spec_conformance(_item(), _result()) is None


# ── the score ──────────────────────────────────────────────────────────────────


def test_conformance_is_off_by_default():
    s = score_run(_score_input(spec_conformance=0.1))
    assert s.gate == "auto"


def test_thin_coverage_withholds_the_automatic_path():
    s = score_run(_score_input(spec_conformance=0.33), conformance_min=60)
    assert s.gate == "review"
    assert any("33%" in r and "60%" in r for r in s.reasons)


def test_coverage_never_changes_the_score_itself():
    """Like a failed case it only decides what may happen next; the score keeps saying
    how well the run went."""
    base = score_run(_score_input())
    low = score_run(_score_input(spec_conformance=0.0), conformance_min=90)
    assert low.score == base.score and low.components == base.components


def test_enough_coverage_leaves_the_gate_alone():
    assert score_run(_score_input(spec_conformance=0.8), conformance_min=60).gate == "auto"
    assert score_run(_score_input(spec_conformance=0.6), conformance_min=60).gate == "auto"


def test_unmeasured_coverage_is_silent_even_when_a_minimum_is_set():
    s = score_run(_score_input(spec_conformance=None), conformance_min=90)
    assert s.gate == "auto" and not any("tiêu chí" in r for r in s.reasons)


def test_coverage_cannot_soften_an_escalation():
    s = score_run(_score_input(tests_failed=1, spec_conformance=0.1), conformance_min=60)
    assert s.gate == "escalate"
