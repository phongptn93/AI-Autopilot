"""Tests for the auto-test-gate (TestGate + command detection)."""

from __future__ import annotations

import sys

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
    cmd = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    res = await TestGate(
        Settings(test_gate_enabled=True, test_command=cmd, test_timeout_seconds=1)
    ).run(str(tmp_path))
    assert res.ran is True and res.passed is False
    assert "timed out" in res.summary


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
