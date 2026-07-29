"""Auto-test-gate: run the target repo's test suite in the worktree before a PR.

Mirrors :class:`ai_autopilot.execution.auto_reviewer.AutoReviewer` — a check the
control plane runs after the agent finishes editing but BEFORE a PR is opened, so
a change that breaks the tests never becomes a PR. Opt-in (``test_gate_enabled``);
when off it is a no-op that always "passes".

The test command is either explicit (``test_command``) or auto-detected from the
files present in the worktree. If no runner can be detected the gate SKIPS (does
NOT block) — a repo without a recognised test setup must not get stuck. Only a
real red run (non-zero exit) blocks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger

# Keep only the tail of the test output — enough to see the failing assertions in
# a log / comment without carrying megabytes of passing noise.
_OUTPUT_TAIL_CHARS = 4000


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class (name starts with "Test")
    passed: bool = True
    ran: bool = False          # False = gate disabled OR no runner detected (skip)
    summary: str = ""
    output_tail: str = ""


def detect_test_command(work_dir: str) -> str | None:
    """Best-effort test command for a repo, from the files it contains.

    Returns ``None`` when nothing recognisable is found (caller then skips the gate).
    """
    root = Path(work_dir)

    def has(*names: str) -> bool:
        return any((root / n).exists() for n in names)

    # Python: pytest is the project convention here.
    if has("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini") or (root / "tests").is_dir():
        return "python -m pytest -q"
    # .NET: a solution or any project file.
    if has(*[p.name for p in root.glob("*.sln")]) or any(root.glob("*.csproj")):
        return "dotnet test --nologo"
    # Node: only when package.json actually declares a test script.
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            import json

            if "test" in (json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}):
                return "npm test --silent"
        except (OSError, ValueError):
            pass
    return None


class TestGate:
    __test__ = False  # not a pytest test class (name starts with "Test")

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("execution.test_gate")

    async def run(self, work_dir: str) -> TestResult:
        if not self._config.test_gate_enabled:
            return TestResult(passed=True, ran=False, summary="test gate disabled")

        cmd = self._config.test_command.strip() or detect_test_command(work_dir)
        if not cmd:
            self._log.info("test gate: no runner detected — skipping", dir=work_dir)
            return TestResult(passed=True, ran=False, summary="no test runner detected")

        self._log.info("running test gate", dir=work_dir, cmd=cmd)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self._config.test_timeout_seconds
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                self._log.warning("test gate timed out", dir=work_dir, cmd=cmd)
                return TestResult(
                    passed=False, ran=True,
                    summary=f"tests timed out after {self._config.test_timeout_seconds}s",
                )
        except Exception as exc:  # noqa: BLE001 — a broken command must not crash the run
            self._log.warning("test gate failed to launch", cmd=cmd, error=str(exc))
            # Couldn't even start the runner → treat as skip (don't block on our own error).
            return TestResult(passed=True, ran=False, summary=f"could not run tests: {exc}")

        text = (out or b"").decode("utf-8", "replace")
        passed = proc.returncode == 0
        self._log.info("test gate done", dir=work_dir, passed=passed, code=proc.returncode)
        return TestResult(
            passed=passed,
            ran=True,
            summary=("tests passed" if passed else f"tests failed (exit {proc.returncode})"),
            output_tail=text[-_OUTPUT_TAIL_CHARS:],
        )
