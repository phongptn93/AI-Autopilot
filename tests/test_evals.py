"""Tests for the eval harness.

The harness is what will be trusted to say whether a change to a skill made the agent
better or worse, so its own failure modes matter more than most: a harness that scores
an empty suite as perfect, or passes a case that asserts nothing, produces a green gate
that checks air. Every test here is aimed at one of those.

No model is called — the runner is injected.
"""

from __future__ import annotations

import json

import pytest

from ai_autopilot.evals import (
    Check,
    EvalCase,
    SuiteResult,
    apply_checks,
    format_report,
    load_cases,
    run_case,
    run_suite,
)


def _runner(text: str):
    async def _run(prompt, cwd, tools, timeout_seconds):
        return text
    return _run


def _boom(exc: Exception):
    async def _run(prompt, cwd, tools, timeout_seconds):
        raise exc
    return _run


# ── the two failure modes that would make the suite worthless ──────────────────

def test_an_empty_suite_scores_zero_not_one():
    """A suite that loaded nothing has proved nothing. Scoring it 100% is how a bad
    path or a wrong glob becomes a green gate checking air."""
    assert SuiteResult().pass_rate == 0.0
    assert SuiteResult().total == 0


async def test_a_case_with_no_checks_fails():
    """"The agent said something" is not a standard. A case asserting nothing would
    quietly lift the pass rate while testing nothing."""
    case = EvalCase(name="empty", prompt="hi", checks=[])
    failures = await apply_checks(case, "some output", "")
    assert failures and "asserts nothing" in failures[0]


# ── the check kinds ───────────────────────────────────────────────────────────

async def test_contains_and_not_contains_ignore_case():
    """An eval must not fail because the model chose a capital letter — a check that
    sensitive is asserting style, which does not survive a model upgrade."""
    case = EvalCase("c", "p", [Check("contains", "OWASP"), Check("not_contains", "ZZZ")])
    assert await apply_checks(case, "we follow owasp guidance", "") == []


async def test_contains_reports_the_check_that_failed():
    case = EvalCase("c", "p", [Check("contains", "OWASP")])
    failures = await apply_checks(case, "nothing relevant here", "")
    assert len(failures) == 1 and "OWASP" in failures[0]


async def test_regex_matches_across_lines():
    case = EvalCase("c", "p", [Check("regex", r"step 1.*reproduce")])
    assert await apply_checks(case, "Step 1\nthen REPRODUCE the bug", "") == []


async def test_file_checks_resolve_under_the_case_cwd(tmp_path):
    (tmp_path / "made.txt").write_text("hello there", encoding="utf-8")
    case = EvalCase("c", "p", [
        Check("file_exists", "made.txt"),
        Check("file_absent", "nope.txt"),
        Check("file_contains", "HELLO", path="made.txt"),
    ])
    assert await apply_checks(case, "", str(tmp_path)) == []


async def test_a_missing_file_is_a_failure(tmp_path):
    case = EvalCase("c", "p", [Check("file_exists", "nope.txt")])
    assert len(await apply_checks(case, "", str(tmp_path))) == 1


async def test_shell_check_uses_the_exit_code(tmp_path):
    case = EvalCase("c", "p", [Check("shell", "python -c \"raise SystemExit(0)\"")])
    assert await apply_checks(case, "", str(tmp_path)) == []
    bad = EvalCase("c", "p", [Check("shell", "python -c \"raise SystemExit(3)\"")])
    assert len(await apply_checks(bad, "", str(tmp_path))) == 1


async def test_an_unknown_check_kind_fails_rather_than_passing_silently():
    """A typo in `kind` must not read as "nothing to check, therefore fine"."""
    case = EvalCase("c", "p", [Check("contians", "x")])
    failures = await apply_checks(case, "x", "")
    assert failures and "unknown check kind" in failures[0]


# ── running cases ─────────────────────────────────────────────────────────────

async def test_a_case_that_crashes_is_a_case_that_failed():
    """A model timeout or a transport error is not an excuse — a suite that skipped
    those would report a pass rate it had not earned."""
    got = await run_case(EvalCase("c", "p", [Check("contains", "x")]), _boom(RuntimeError("boom")))
    assert got.passed is False and "boom" in got.error


async def test_the_suite_reports_a_pass_rate():
    cases = [
        EvalCase("good", "p", [Check("contains", "yes")]),
        EvalCase("bad", "p", [Check("contains", "absent")]),
    ]
    result = await run_suite(cases, _runner("yes"))
    assert result.total == 2 and result.passed == 1
    assert result.pass_rate == pytest.approx(0.5)


# ── loading ───────────────────────────────────────────────────────────────────

def test_cases_load_from_yaml_and_json(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "name: from-yaml\nprompt: do a thing\nchecks:\n  - kind: contains\n    value: ok\n",
        encoding="utf-8")
    (tmp_path / "b.json").write_text(
        json.dumps({"name": "from-json", "prompt": "do another",
                    "checks": [{"kind": "contains", "value": "ok"}]}), encoding="utf-8")
    cases = load_cases(tmp_path)
    assert [c.name for c in cases] == ["from-json", "from-yaml"]
    assert cases[0].checks[0].kind == "contains"


def test_one_malformed_file_does_not_hide_the_others(tmp_path):
    """One bad case must not take down the verdict of the other forty."""
    (tmp_path / "broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    (tmp_path / "fine.yaml").write_text(
        "name: fine\nprompt: p\nchecks:\n  - kind: contains\n    value: ok\n", encoding="utf-8")
    assert [c.name for c in load_cases(tmp_path)] == ["fine"]


def test_the_shorthand_check_form_is_understood(tmp_path):
    (tmp_path / "s.yaml").write_text(
        "name: short\nprompt: p\nchecks:\n  - 'contains: OWASP'\n", encoding="utf-8")
    check = load_cases(tmp_path)[0].checks[0]
    assert check.kind == "contains" and check.value == "OWASP"


def test_a_missing_directory_loads_nothing_rather_than_raising(tmp_path):
    assert load_cases(tmp_path / "does-not-exist") == []


def test_an_entry_without_a_prompt_is_not_a_case(tmp_path):
    (tmp_path / "x.yaml").write_text("name: no-prompt\nchecks: []\n", encoding="utf-8")
    assert load_cases(tmp_path) == []


# ── the report ────────────────────────────────────────────────────────────────

def test_the_report_says_an_empty_suite_proves_nothing():
    text = format_report(SuiteResult(), 1.0)
    assert "0/0" in text and "proves nothing" in text


async def test_the_report_names_the_failed_check():
    cases = [EvalCase("nope", "p", [Check("contains", "OWASP")])]
    result = await run_suite(cases, _runner("unrelated"))
    text = format_report(result, 1.0)
    assert "[FAIL] nope" in text and "OWASP" in text
