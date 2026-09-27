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
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.proc import spawn_kwargs, terminate_tree

# Keep only the tail of the test output — enough to see the failing assertions in
# a log / comment without carrying megabytes of passing noise.
_OUTPUT_TAIL_CHARS = 4000
# More distinct failures than this is a broken build, not a list anyone reads.
_MAX_FAILURES = 50


@dataclass
class TestResult:
    __test__ = False  # not a pytest test class (name starts with "Test")
    passed: bool = True
    ran: bool = False          # False = gate disabled OR no runner detected (skip)
    summary: str = ""
    output_tail: str = ""
    # What failed, one normalised line each (see :func:`failure_signatures`) — what a
    # reader needs to act on, and what makes two runs comparable.
    failures: list[str] = field(default_factory=list)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# .NET build: "path/File.cs(378,54): error CS1503: message [path/Project.csproj]"
_DOTNET_BUILD = re.compile(
    r"^(?P<path>[^\s(][^(]*?)\(\d+,\d+\):\s*error\s+(?P<code>[A-Z]+\d+):\s*(?P<msg>.*?)"
    r"(?:\s*\[[^\]]*\])?\s*$")
# .NET test: "  Failed Namespace.Class.Method [7 ms]"
_DOTNET_TEST = re.compile(r"^\s*Failed\s+(?P<name>[\w.`+<>,\[\]-]+?)(?:\s*\[[^\]]*\])?\s*$")
# pytest: "FAILED tests/test_x.py::test_name - AssertionError…"
_PYTEST = re.compile(r"^FAILED\s+(?P<name>\S+)")
# jest / vitest: "FAIL src/app.spec.ts"  ·  "  ● Suite › test"
_JEST_FILE = re.compile(r"^\s*FAIL\s+(?P<name>\S+)")
_JEST_TEST = re.compile(r"^\s*●\s+(?P<name>.+?)\s*$")
_TEST_PATTERNS = (_DOTNET_TEST, _PYTEST, _JEST_FILE, _JEST_TEST)


def failure_signatures(output: str, root: str = "") -> list[str]:
    """The distinct failures in a test run's output, normalised so two runs compare.

    A build error keeps file, code and message but not line/column — a merge shifts
    lines, and "the same error three lines lower" is still the same error. Paths are
    made relative to ``root`` because the target baseline and the resolved branch run
    in different worktrees. Order of first appearance, de-duplicated, capped.
    """
    roots = {r for r in (root, root.replace("\\", "/"), root.replace("/", "\\")) if r}
    out: list[str] = []
    seen: set[str] = set()

    def add(sig: str) -> None:
        sig = sig.strip()
        if sig and sig not in seen:
            seen.add(sig)
            out.append(sig)

    for raw in (output or "").splitlines():
        line = _ANSI.sub("", raw).rstrip()
        for r in roots:
            line = line.replace(r.rstrip("\\/") + "\\", "").replace(r.rstrip("\\/") + "/", "")
        if m := _DOTNET_BUILD.match(line.strip()):
            path = m["path"].strip().replace("\\", "/")
            add(f"build {m['code']} {path}: {m['msg'].strip()}")
        else:
            for pattern in _TEST_PATTERNS:
                if m := pattern.match(line):
                    add(f"test {m['name']}")
                    break
        if len(out) >= _MAX_FAILURES:
            break
    return out


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

            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
            script = str(scripts.get("test", ""))
            if script:
                # `ng test` defaults to WATCH mode and wants a real browser, so plain
                # `npm test` never exits: the gate then burns the whole timeout and
                # reports a failure, blocking every frontend PR for a reason that has
                # nothing to do with the change. Ask for the one-shot headless run
                # unless the script already settles it.
                if "ng test" in script and "--watch" not in script:
                    return "npm test -- --watch=false --browsers=ChromeHeadless"
                return "npm test --silent"
        except (OSError, ValueError):
            pass
    return None


async def repo_name_for(work_dir: str) -> str:
    """The repo a worktree belongs to, read from its ``origin`` remote.

    Not from the path: a worktree is named ``r<item>-<digest>`` and carries no trace of
    the repo, so per-repo settings had nothing to match on. Blank on any failure — the
    caller then uses the flat setting, which is what a single-repo install wants anyway.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", work_dir, "remote", "get-url", "origin",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception:  # noqa: BLE001 — identifying the repo must not break the gate
        return ""
    url = (out or b"").decode("utf-8", "replace").strip()
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


class TestGate:
    __test__ = False  # not a pytest test class (name starts with "Test")

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("execution.test_gate")

    async def run(self, work_dir: str) -> TestResult:
        if not self._config.test_gate_enabled:
            return TestResult(passed=True, ran=False, summary="test gate disabled")

        repo = await repo_name_for(work_dir)
        configured = self._config.test_command_for(repo)
        cmd = configured or detect_test_command(work_dir)
        timeout = self._config.test_timeout_for(repo)
        if not cmd:
            self._log.info("test gate: no runner detected — skipping", dir=work_dir, repo=repo)
            return TestResult(passed=True, ran=False, summary="no test runner detected")
        # A DETECTED runner whose binary is not on PATH must not reach the shell: cmd.exe
        # answers "'dotnet' is not recognized" with exit code 1, which read as "tests
        # failed" and escalated every conflict resolution on a .NET repo although no test
        # ever ran. Checked only for detected commands — an operator's own command may
        # start with a shell builtin (`cd x && …`) that `which` cannot see.
        if not configured:
            runner = cmd.split()[0]
            if not shutil.which(runner):
                self._log.warning("test gate: runner not on PATH — skipping", dir=work_dir,
                                  repo=repo, runner=runner,
                                  hint="add it to PATH, or set test_commands for this repo")
                return TestResult(
                    passed=True, ran=False,
                    summary=f"test runner '{runner}' not found on PATH "
                            f"(add it to PATH or set test_commands for {repo or 'this repo'})",
                )

        self._log.info("running test gate", dir=work_dir, repo=repo, cmd=cmd, timeout=timeout)
        try:
            # A shell on purpose: the command is the operator's `test_command` from config
            # (or a fixed detected one) — never read from the branch the agent wrote.
            # autopilot:ignore[py-shell-true] operator-configured test command
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **spawn_kwargs(),
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                # The runner is the SHELL's child, so killing the shell alone leaves it
                # running and holding our stdout pipe — the wait below would then last as
                # long as the suite we are trying to abandon. See ai_autopilot.proc.
                await terminate_tree(proc)
                # A timeout BLOCKS, so the note has to say which repo, which command and
                # which setting to raise. The two ways to get here are a runner that
                # never exits and a suite that genuinely needs longer than one global
                # number allowed — and the reader cannot tell them apart without this.
                self._log.warning("test gate timed out", dir=work_dir, repo=repo, cmd=cmd,
                                  timeout=timeout,
                                  hint="raise it for this repo under Quality gates → "
                                       "test_timeouts, or fix a runner that never exits")
                return TestResult(
                    passed=False, ran=True,
                    summary=(f"tests timed out after {timeout}s"
                             + (f" in {repo}" if repo else "")
                             + f" (command: {cmd})"),
                )
        except Exception as exc:  # noqa: BLE001 — a broken command must not crash the run
            self._log.warning("test gate failed to launch", cmd=cmd, error=describe_exc(exc))
            # Couldn't even start the runner → treat as skip (don't block on our own error).
            return TestResult(passed=True, ran=False, summary=f"could not run tests: {exc}")

        text = (out or b"").decode("utf-8", "replace")
        passed = proc.returncode == 0
        failures = [] if passed else failure_signatures(text, work_dir)
        self._log.info("test gate done", dir=work_dir, passed=passed, code=proc.returncode,
                       failures=len(failures))
        if passed:
            summary = "tests passed"
        elif failures:
            # Say WHAT failed. "tests failed (exit 1)" sent a reader to re-run the whole
            # suite by hand to learn what one line of output would have told them.
            kinds = {"build": 0, "test": 0}
            for f in failures:
                kinds[f.split(" ", 1)[0]] = kinds.get(f.split(" ", 1)[0], 0) + 1
            parts = [f"{kinds['build']} build error(s)"] if kinds["build"] else []
            if kinds["test"]:
                parts.append(f"{kinds['test']} failing test(s)")
            summary = f"tests failed (exit {proc.returncode}): " + ", ".join(parts)
        else:
            summary = f"tests failed (exit {proc.returncode})"
        return TestResult(
            passed=passed,
            ran=True,
            summary=summary,
            output_tail=text[-_OUTPUT_TAIL_CHARS:],
            failures=failures,
        )
