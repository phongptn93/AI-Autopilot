"""Tests for the auto-review issue parser — the gate that decides pass/block."""

from __future__ import annotations

from ai_autopilot.execution.auto_reviewer import ReviewResult, _parse_issues

_BLOCKED = {"CRITICAL", "HIGH"}


def _parse(output: str) -> ReviewResult:
    r = ReviewResult()
    _parse_issues(output, r, _BLOCKED)
    return r


def test_prose_mentioning_severities_is_not_a_finding():
    # The exact false positive that used to block legit fixes: a summary line that says
    # there are NO Critical/High issues must NOT be counted as a blocking finding.
    output = "\n".join([
        "Bottom line: this branch has no Critical/High/Medium security issues.",
        "The only finding is Low — a missing .gitignore leaks local dev paths.",
    ])
    r = _parse(output)
    assert r.critical_issues == []          # nothing blocks
    assert r.warnings == []                 # prose isn't a tagged finding either


def test_only_bracket_tagged_lines_count():
    output = "\n".join([
        "Summary: reviewed the migration and the upload handler.",
        "- [Low] committed build artifacts leak local dev paths",
        "- [High] SQL injection in the raw query builder",
        "  * [Critical] hardcoded secret in appsettings",
    ])
    r = _parse(output)
    assert len(r.critical_issues) == 2      # [High] + [Critical] block
    assert all("[" in i for i in r.critical_issues)
    assert len(r.warnings) == 1             # [Low] is advisory
    assert "[Low]" in r.warnings[0]


def test_none_means_clean():
    r = _parse("- [None] no issues found")
    assert r.critical_issues == [] and r.warnings == []


def test_passed_is_true_when_no_blocking_findings():
    r = _parse("- [Low] minor style nit\n- [Medium] add input validation")
    r.passed = len(r.critical_issues) == 0   # mirrors AutoReviewer.review()
    assert r.passed is True
    assert len(r.warnings) == 2
