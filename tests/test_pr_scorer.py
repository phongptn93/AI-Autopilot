"""Tests for the run/PR scorer (the 'get a score' checker stage)."""

from __future__ import annotations

from ai_autopilot.execution.pr_scorer import ScoreInput, score_badge_html, score_run


def test_perfect_run_scores_A_and_auto():
    s = score_run(ScoreInput(
        completed=True, has_pr=True, files_changed=4,
        review_passed=True, review_critical=0, review_warnings=0, ci_passed=True,
    ))
    assert s.score == 100 and s.grade == "A" and s.gate == "auto"


def test_unverified_run_caps_at_review_not_auto():
    # completed + PR + files changed, but no auto-review and unknown CI.
    s = score_run(ScoreInput(completed=True, has_pr=True, files_changed=3))
    assert s.score == 78                      # 35 + 18 + 10 + 15
    assert s.grade == "C" and s.gate == "review"   # cannot reach auto without evidence


def test_critical_issues_tank_review_component():
    s = score_run(ScoreInput(
        completed=True, has_pr=True, files_changed=2,
        review_passed=False, review_critical=1, ci_passed=True,
    ))
    assert s.components["review"] == 15       # 30 - 15
    assert any("critical" in r for r in s.reasons)


def test_ci_red_zeroes_ci_component():
    s = score_run(ScoreInput(
        completed=True, has_pr=True, files_changed=2,
        review_passed=True, ci_passed=False,
    ))
    assert s.components["ci"] == 0
    assert any("CI" in r for r in s.reasons)


def test_no_files_changed_zeroes_scope():
    s = score_run(ScoreInput(completed=True, has_pr=True, files_changed=0))
    assert s.components["scope"] == 0
    assert any("file" in r for r in s.reasons)


def test_unresolved_threads_penalise_scope():
    s = score_run(ScoreInput(completed=True, has_pr=True, files_changed=5, unresolved_threads=2))
    assert s.components["scope"] == 15 - 4    # 2 threads × 2, capped at 6


def test_incomplete_run_gets_zero_delivery_and_escalates():
    s = score_run(ScoreInput(completed=False, has_pr=False, files_changed=0, had_error=True))
    assert s.components["delivery"] == 0
    assert s.gate == "escalate" and s.grade == "F"


def test_report_run_without_pr_scores_partial_delivery():
    s = score_run(ScoreInput(completed=True, has_pr=False, files_changed=0))
    assert s.components["delivery"] == 20
    assert any("PR" in r for r in s.reasons)


def test_thresholds_are_honoured():
    inp = ScoreInput(completed=True, has_pr=True, files_changed=3)  # 78
    assert score_run(inp, auto_min=75).gate == "auto"              # lower bar → auto
    assert score_run(inp, review_min=80).gate == "escalate"        # raise bar → escalate


def test_badge_html_shows_score_grade_and_gate():
    s = score_run(ScoreInput(completed=True, has_pr=True, files_changed=3))
    html = score_badge_html(s)
    assert "78/100 (C)" in html and "review" in html and "Run score" in html
