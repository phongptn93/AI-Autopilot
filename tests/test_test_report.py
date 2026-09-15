"""The QC result comment — what a reader of the work item actually sees.

Pure rendering, so it is tested without a poller or an ADO account: the verdict, the
order of the rows, and the two things agent text must never be able to do (break the
comment's markup, or make it too long to read).
"""

from __future__ import annotations

from ai_autopilot import test_report
from ai_autopilot.execution.result_contract import CaseOutcome


def _o(title, outcome="pass", note=""):
    return CaseOutcome(title=title, outcome=outcome, note=note)


def test_nothing_executed_renders_nothing():
    """An empty table would read as a verdict, and "no cases run" is not one."""
    report = test_report.render_comment([])
    assert report.is_empty and report.html == ""


def test_a_failure_leads_and_is_counted():
    report = test_report.render_comment([
        _o("A"), _o("B"), _o("C", "fail", "timeout after 30s"),
    ])
    assert (report.total, report.passed, report.failed, report.blocked) == (3, 2, 1, 0)
    assert not report.all_passed
    assert "1/3 không đạt" in report.html
    assert "timeout after 30s" in report.html
    # Failures first — a reader who stops after one row saw the one that decides.
    assert report.html.index("<td>C</td>") < report.html.index("<td>A</td>")


def test_a_clean_run_says_so_without_hedging():
    report = test_report.render_comment([_o("A"), _o("B")])
    assert report.all_passed
    assert "2/2 đạt" in report.html
    # No call to action when there is nothing to act on.
    assert "cần được xử lý" not in report.html


def test_blocked_is_reported_as_its_own_verdict_not_as_a_pass():
    """"Could not run it" is neither a pass nor a failure, and reporting it as either
    is how a run that tested nothing reads as green."""
    report = test_report.render_comment([_o("A"), _o("B", "blocked", "no permission")])
    assert (report.passed, report.failed, report.blocked) == (1, 0, 1)
    assert not report.all_passed
    assert "1/2 chưa chạy được" in report.html


def test_blocked_rows_sort_between_failures_and_passes():
    report = test_report.render_comment([
        _o("pass-one"), _o("blocked-one", "blocked"), _o("fail-one", "fail"),
    ])
    h = report.html
    assert h.index("fail-one") < h.index("blocked-one") < h.index("pass-one")


def test_agent_text_cannot_break_the_comment():
    """Notes and titles land verbatim in an ADO comment (HTML)."""
    report = test_report.render_comment([
        _o("<script>alert(1)</script>", "fail", "broke <b>everything</b>"),
    ])
    assert "<script>" not in report.html
    assert "&lt;script&gt;" in report.html
    assert "broke &lt;b&gt;everything&lt;/b&gt;" in report.html


def test_a_very_long_note_is_bounded():
    report = test_report.render_comment([_o("A", "fail", "x" * 5000)])
    assert len(report.html) < 2000
    assert "…" in report.html


def test_hundreds_of_cases_do_not_produce_a_comment_nobody_reads():
    report = test_report.render_comment([_o(f"case {i}") for i in range(200)])
    assert report.total == 200                 # counted in full
    assert report.html.count("<tr>") <= 61     # 60 rows + the header
    assert "và 140 case nữa" in report.html


def test_the_dashboard_link_is_optional():
    assert "Theo dõi" not in test_report.render_comment([_o("A")]).html
    linked = test_report.render_comment([_o("A")], dashboard_url="https://x.test/d")
    assert 'href="https://x.test/d"' in linked.html
