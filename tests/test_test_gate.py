"""Tests for the auto-test-gate (TestGate + command detection)."""

from __future__ import annotations

import sys
import time

from ai_autopilot.config import Settings
from ai_autopilot.execution.test_gate import TestGate, detect_test_command


# ── command detection ────────────────────────────────────────────────────────
def test_detect_pytest_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert detect_test_command(str(tmp_path)) == "python -m pytest -q"


def test_detect_pytest_from_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    assert detect_test_command(str(tmp_path)) == "python -m pytest -q"


def test_detect_dotnet_from_csproj(tmp_path):
    (tmp_path / "App.csproj").write_text("<Project/>", encoding="utf-8")
    assert detect_test_command(str(tmp_path)) == "dotnet test --nologo"


def test_detect_npm_only_when_test_script(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
    assert detect_test_command(str(tmp_path)) == "npm test --silent"


def test_detect_npm_skipped_without_test_script(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"build": "tsc"}}', encoding="utf-8")
    assert detect_test_command(str(tmp_path)) is None


def test_detect_none_for_empty_dir(tmp_path):
    assert detect_test_command(str(tmp_path)) is None


# ── gate behaviour ───────────────────────────────────────────────────────────
async def test_gate_disabled_is_noop(tmp_path):
    res = await TestGate(Settings(test_gate_enabled=False)).run(str(tmp_path))
    assert res.passed is True and res.ran is False


async def test_gate_skips_when_no_runner(tmp_path):
    # Enabled but nothing to detect → skip, never blocks.
    res = await TestGate(Settings(test_gate_enabled=True)).run(str(tmp_path))
    assert res.passed is True and res.ran is False


async def test_detected_runner_missing_from_path_is_a_skip_not_a_failure(tmp_path, monkeypatch):
    # cmd.exe answers an unknown command with exit 1 — without this check a .NET repo
    # on a machine whose PATH lacks dotnet reported "tests failed (exit 1)" and every
    # conflict resolution was escalated although no test had run.
    (tmp_path / "App.csproj").write_text("<Project/>", encoding="utf-8")
    monkeypatch.setattr("ai_autopilot.execution.test_gate.shutil.which", lambda _b: None)
    gate = TestGate(Settings(test_gate_enabled=True, test_command=""))
    r = await gate.run(str(tmp_path))
    assert r.passed and not r.ran
    assert "'dotnet' not found on PATH" in r.summary


async def test_configured_command_is_not_second_guessed(tmp_path, monkeypatch):
    # An operator's command may start with a shell builtin `which` cannot see.
    monkeypatch.setattr("ai_autopilot.execution.test_gate.shutil.which", lambda _b: None)
    gate = TestGate(Settings(test_gate_enabled=True,
                             test_command=f'"{sys.executable}" -c "import sys; sys.exit(0)"'))
    r = await gate.run(str(tmp_path))
    assert r.ran and r.passed


async def test_gate_passes_on_zero_exit(tmp_path):
    res = await TestGate(
        Settings(test_gate_enabled=True, test_command="exit 0")
    ).run(str(tmp_path))
    assert res.ran is True and res.passed is True


async def test_gate_fails_on_nonzero_exit(tmp_path):
    res = await TestGate(
        Settings(test_gate_enabled=True, test_command="exit 1")
    ).run(str(tmp_path))
    assert res.ran is True and res.passed is False
    assert "exit 1" in res.summary


async def test_gate_times_out(tmp_path):
    """The timeout must BOUND the gate, not merely describe what happened.

    The runner is the shell's grandchild, so killing the shell alone leaves it alive
    holding our stdout pipe: the gate then returned only when the runner finished by
    itself — 25s measured against a 1s timeout, and never at all for a runner that
    does not exit (`ng test` in watch mode), which is the case the timeout exists for.
    Asserting the summary text alone passed happily through all of that, so assert the
    clock: returning fast is the only proof the whole tree actually died.
    """
    cmd = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    started = time.monotonic()
    res = await TestGate(
        Settings(test_gate_enabled=True, test_command=cmd, test_timeout_seconds=1)
    ).run(str(tmp_path))
    elapsed = time.monotonic() - started

    assert res.ran is True and res.passed is False
    assert "timed out" in res.summary
    assert elapsed < 15, (
        f"gate returned after {elapsed:.1f}s for a 1s timeout — the runner outlived "
        "the kill and the gate waited for it"
    )


def test_angular_is_not_detected_as_a_watch_run(tmp_path):
    """`ng test` defaults to WATCH mode and wants a real browser, so plain `npm test`
    never exits: the gate burns the whole timeout and reports a failure, blocking every
    frontend PR for a reason that has nothing to do with the change."""
    import json

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "ng test"}}), encoding="utf-8")
    cmd = detect_test_command(str(tmp_path))
    assert "--watch=false" in cmd and "ChromeHeadless" in cmd


def test_a_script_that_already_settles_watch_is_left_alone(tmp_path):
    import json

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "ng test --watch=false"}}), encoding="utf-8")
    assert detect_test_command(str(tmp_path)) == "npm test --silent"


def test_a_plain_node_test_script_is_unchanged(tmp_path):
    import json

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    assert detect_test_command(str(tmp_path)) == "npm test --silent"


# ── failure signatures: what failed, comparable across worktrees ─────────────
from ai_autopilot.execution.test_gate import failure_signatures  # noqa: E402

_ERR = (r"error CS1503: Argument 1: cannot convert from 'decimal?' to 'decimal' "
        r"[C:\wt\conflict-1\Plugins\Fac\Fac.csproj]")
_DOTNET = "\n".join([          # real `dotnet test` shape: same error on two lines
    "  Restore complete (2.1s)",
    r"C:\wt\conflict-1\Plugins\Fac\Report.cs(378,54): " + _ERR,
    r"C:\wt\conflict-1\Plugins\Fac\Report.cs(381,53): " + _ERR,
    "  Failed Nois.UnitTest.AiAgent.MetricDateTimezoneTests"
    ".An_offset_does_not_move_the_day [7 ms]",
    "Failed!  - Failed:     1, Passed:  1219, Skipped:     0, Total:  1220",
])


def test_dotnet_build_errors_ignore_line_numbers_and_worktree_path():
    sigs = failure_signatures(_DOTNET, r"C:\wt\conflict-1")
    assert sigs == [
        "build CS1503 Plugins/Fac/Report.cs: Argument 1: cannot convert from 'decimal?' "
        "to 'decimal'",
        "test Nois.UnitTest.AiAgent.MetricDateTimezoneTests.An_offset_does_not_move_the_day",
    ]
    # The same failures from another worktree compare equal — the whole point.
    other = _DOTNET.replace(r"C:\wt\conflict-1", r"C:\wt\conflict-1-base")
    assert failure_signatures(other, r"C:\wt\conflict-1-base") == sigs


def test_pytest_and_jest_failures_and_ansi_colour():
    out = ("FAILED tests/test_x.py::test_total - AssertionError\n"
           "\x1b[31m FAIL \x1b[0m src/app.spec.ts\n  ● Cart › keeps both discounts\n")
    assert failure_signatures(out) == [
        "test tests/test_x.py::test_total", "test src/app.spec.ts",
        "test Cart › keeps both discounts"]


async def test_a_red_run_says_what_failed(tmp_path):
    script = tmp_path / "t.py"
    script.write_text("print('  Failed App.Tests.It_breaks [1 ms]'); raise SystemExit(1)\n",
                      encoding="utf-8")
    gate = TestGate(Settings(test_gate_enabled=True,
                             test_command=f'"{sys.executable}" "{script}"'))
    r = await gate.run(str(tmp_path))
    assert not r.passed and r.failures == ["test App.Tests.It_breaks"]
    assert r.summary == "tests failed (exit 1): 1 failing test(s)"
