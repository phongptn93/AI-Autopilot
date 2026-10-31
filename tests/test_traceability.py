"""Tests for ai_autopilot.traceability — AC coverage, failing detection, unlinked cases."""

from __future__ import annotations

import pytest

from ai_autopilot import traceability as t


def _acs(*ids: str) -> list[dict[str, str]]:
    return [{"id": i, "text": f"text of {i}"} for i in ids]


@pytest.mark.parametrize("raw", ["ac-1", "AC 01", "AC_001", "Ac-1", " AC-01 ", "ac1"])
def test_normalise_id_variants(raw: str) -> None:
    """Spelling variants of one criterion must compare equal or links silently miss."""
    assert t.normalise_id(raw) == "AC-01"


def test_normalise_id_keeps_large_numbers_and_non_ac() -> None:
    """Only AC ids are reshaped; others are stripped/upper-cased, and AC-12 stays AC-12."""
    assert t.normalise_id("ac-12") == "AC-12"
    assert t.normalise_id("  fr-3 ") == "FR-3"
    assert t.normalise_id("") == ""


def test_link_ids_from_field_mixed_separators() -> None:
    """Testers separate ids with commas, semicolons, spaces or slashes; all must work."""
    assert t.link_ids({"title": "x", "ac": "AC-1, ac 2;AC_003 AC-4/AC-5"}) == [
        "AC-01", "AC-02", "AC-03", "AC-04", "AC-05"]


def test_link_ids_bare_number_in_field() -> None:
    """A field of just '3' is a shorthand for AC-03."""
    assert t.link_ids({"title": "x", "ac": "3, 7"}) == ["AC-03", "AC-07"]


def test_link_ids_from_title_only() -> None:
    """Title mentions count even when the optional field is absent."""
    assert t.link_ids({"title": "Kiểm tra AC-02: đăng nhập sai"}) == ["AC-02"]


def test_link_ids_union_deduplicated_order_preserved() -> None:
    """Field and title naming the same AC must not double-count; field comes first."""
    case = {"title": "AC-1 và AC-3 hoạt động", "ac": "AC-02, ac-1"}
    assert t.link_ids(case) == ["AC-02", "AC-01", "AC-03"]


def test_link_ids_none() -> None:
    """A case with no reference yields an empty list, not an error."""
    assert t.link_ids({"title": "Đăng nhập thành công"}) == []
    assert t.link_ids({"title": "", "ac": ""}) == []


def test_substring_safety_ac1_not_in_ac12() -> None:
    """AC-1 must NOT match inside AC-12, or a test of AC-12 hides a gap in AC-01."""
    assert t.link_ids({"title": "Kiểm tra AC-12"}) == ["AC-12"]
    cov = t.coverage(_acs("AC-01", "AC-12"), [{"title": "Kiểm tra AC-12"}])
    assert [r.status for r in cov.rows] == [t.UNCOVERED, t.COVERED]


def test_token_not_glued_to_word() -> None:
    """'MAC-1' or 'BAC1' are not acceptance criteria."""
    assert t.link_ids({"title": "MAC-1 và BAC2"}) == []


def test_ac1_equals_ac01_in_coverage() -> None:
    """A case tagged 'AC-1' covers a spec criterion written 'AC-01'."""
    cov = t.coverage(_acs("AC-01"), [{"title": "tc", "ac": "AC-1"}])
    assert cov.rows[0].status == t.COVERED
    assert cov.unlinked_cases == []


def test_unknown_ac_case_is_unlinked() -> None:
    """A case naming only an AC that is not in the spec traces to nothing."""
    cov = t.coverage(_acs("AC-01"), [{"title": "tc99", "ac": "AC-99"}, {"title": "no ref"}])
    assert cov.unlinked_cases == ["tc99", "no ref"]
    assert cov.rows[0].status == t.UNCOVERED


def test_case_with_known_and_unknown_is_linked() -> None:
    """Naming one known AC is enough to be linked, even alongside a stale id."""
    cov = t.coverage(_acs("AC-01"), [{"title": "tc", "ac": "AC-01, AC-99"}])
    assert cov.unlinked_cases == []


def test_all_covered_pct_100() -> None:
    """Every AC with a case gives 100%."""
    cov = t.coverage(_acs("AC-01", "AC-02"), [{"title": "a", "ac": "AC-01"},
                                              {"title": "b", "ac": "AC-02"}])
    assert (cov.total, cov.covered, cov.uncovered, cov.failing) == (2, 2, 0, 0)
    assert cov.pct == 100.0
    assert t.conformance(cov) == 1.0


def test_none_covered_pct_0() -> None:
    """No cases at all gives 0%, not None."""
    cov = t.coverage(_acs("AC-01", "AC-02"), [])
    assert cov.pct == 0.0
    assert cov.uncovered == 2
    assert t.conformance(cov) == 0.0


def test_no_acs_pct_and_conformance_none() -> None:
    """Without criteria there is nothing to divide by; None, never ZeroDivisionError."""
    cov = t.coverage([], [{"title": "orphan"}])
    assert cov.pct is None
    assert t.conformance(cov) is None
    assert cov.unlinked_cases == ["orphan"]


@pytest.mark.parametrize("outcome", ["fail", "blocked", "FAIL", " Blocked "])
def test_failing_outcome_marks_ac_failing(outcome: str) -> None:
    """A failed or blocked case must surface; a green 'covered' would hide it."""
    cov = t.coverage(_acs("AC-01"), [{"title": "a", "ac": "AC-01", "outcome": outcome}])
    assert cov.rows[0].status == t.FAILING
    assert cov.failing == 1
    assert cov.covered == 1  # failing ACs still have a case


@pytest.mark.parametrize("outcome", ["pass", "pending_deploy", "", None])
def test_non_failing_outcomes_stay_covered(outcome: str | None) -> None:
    """Pass, pending_deploy and unknown outcomes must not fail the criterion."""
    case = {"title": "a", "ac": "AC-01"}
    if outcome is not None:
        case["outcome"] = outcome
    assert t.coverage(_acs("AC-01"), [case]).rows[0].status == t.COVERED


def test_conformance_excludes_failing() -> None:
    """Gate score counts only covered, non-failing ACs: 1 ok of 4 -> 0.25."""
    cov = t.coverage(_acs("AC-01", "AC-02", "AC-03", "AC-04"), [
        {"title": "a", "ac": "AC-01", "outcome": "pass"},
        {"title": "b", "ac": "AC-02", "outcome": "fail"},
        {"title": "c", "ac": "AC-03", "outcome": "blocked"},
    ])
    assert t.conformance(cov) == 0.25
    assert cov.pct == 75.0


def test_one_failing_case_among_passing_fails_ac() -> None:
    """Several cases for one AC: any failure wins over passes."""
    cov = t.coverage(_acs("AC-01"), [
        {"title": "a", "ac": "AC-01", "outcome": "pass"},
        {"title": "b", "ac": "AC-01", "outcome": "fail"},
    ])
    assert cov.rows[0].status == t.FAILING
    assert cov.rows[0].cases == ["a", "b"]


def test_one_case_covers_several_acs() -> None:
    """A case naming two ACs counts for both."""
    cov = t.coverage(_acs("AC-01", "AC-02"), [{"title": "wide", "ac": "AC-01; AC-02"}])
    assert [r.cases for r in cov.rows] == [["wide"], ["wide"]]


def test_ac_with_empty_id_is_uncovered() -> None:
    """An unnamed AC cannot be linked; it must be reported, not crash or be dropped."""
    cov = t.coverage([{"id": "", "text": "unnamed"}, {"id": "AC-01", "text": "x"}],
                     [{"title": "a", "ac": "AC-01"}])
    assert cov.rows[0].ac_id == ""
    assert cov.rows[0].status == t.UNCOVERED
    assert cov.total == 2


def test_acs_from_spec_wrapper() -> None:
    """The convenience wrapper feeds extract_acs output into coverage()."""
    spec = "## Acceptance Criteria\n- AC-01: Đăng nhập đúng thì vào trang chủ\n"
    acs = t.acs_from_spec(spec)
    assert acs and acs[0]["id"] == "AC-01"


def test_markdown_render_and_escaping() -> None:
    """Pipes in titles would break the table; summary line must be present."""
    cov = t.coverage([{"id": "AC-01", "text": "a|b"}],
                     [{"title": "x|y", "ac": "AC-01", "outcome": "fail"}, {"title": "lạc|"}])
    md = t.render_markdown(cov, "Spec A")
    assert "Spec A" in md
    assert "0/1" not in md and "1/1 AC có test case, 1 lỗi" in md
    assert "| AC | Trạng thái | Test case |" in md
    assert "a\\|b" in md and "x\\|y" in md
    assert "lạc\\|" in md


def test_html_render_escapes_everything() -> None:
    """User strings must never reach the page raw."""
    cov = t.coverage([{"id": "AC-01", "text": "<script>alert(1)</script>"}],
                     [{"title": "<b>t</b>", "ac": "AC-01"}, {"title": "<img src=x>"}])
    out = t.render_html(cov, "<i>T</i>")
    assert "<script>" not in out and "<b>" not in out and "<img" not in out
    assert "&lt;script&gt;" in out and "&lt;i&gt;T&lt;/i&gt;" in out
    assert out.count("<table>") == 1
