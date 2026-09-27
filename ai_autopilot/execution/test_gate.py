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
import os
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
# The name runs up to the trailing "[7 ms]"; an xUnit [Theory] carries its arguments in
# it — "Method(qty: 3.073, unit: 168000)" — and each case is its own failure.
_DOTNET_TEST = re.compile(r"^\s*Failed\s+(?P<name>[\w.`+<>]\S*(?:\(.*\))?)(?:\s+\[[^\]]*\])?\s*$")
# pytest: "FAILED tests/test_x.py::test_name - AssertionError…"
_PYTEST = re.compile(r"^FAILED\s+(?P<name>\S+)")
# jest / vitest: "FAIL src/app.spec.ts"  ·  "  ● Suite › test"
_JEST_FILE = re.compile(r"^\s*FAIL\s+(?P<name>\S+)")
_JEST_TEST = re.compile(r"^\s*●\s+(?P<name>.+?)\s*$")
_TEST_PATTERNS = (_DOTNET_TEST, _PYTEST, _JEST_FILE, _JEST_TEST)
# TypeScript / Angular CLI: "error TS500: Error: ENOENT: … lstat '<path>'"
_TS_ERROR = re.compile(r"error (?P<code>TS\d+): (?P<msg>.+?)\s*$")

# Karma's ChromeHeadless needs a Chromium binary. Every Windows machine has Edge, which
# Karma drives as Chrome through CHROME_BIN — used only when Chrome itself is absent and
# the operator has not pointed CHROME_BIN anywhere.
_CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
_EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _browser_env() -> dict[str, str] | None:
    """A process env with CHROME_BIN → Edge when Chrome is missing, else None (inherit)."""
    if os.environ.get("CHROME_BIN") or any(Path(p).is_file() for p in _CHROME_PATHS):
        return None
    edge = next((p for p in _EDGE_PATHS if Path(p).is_file()), "")
    return {**os.environ, "CHROME_BIN": edge} if edge else None

# The run could not START its tests — a tool or browser is missing. That is the
# machine's state, not the change's: reported as a skip with the reason, never as a
# failing test (which escalated every Angular conflict resolution: a fresh worktree
# has no node_modules, so `ng` did not exist).
_ENV_FAILURES = (
    (re.compile(r"'([^']+)' is not recognized as an internal or external command"),
     "'{0}' not found — dependencies/tools not installed in the worktree"),
    (re.compile(r"(?:^|\s)([\w.-]+): (?:command )?not found\s*$", re.MULTILINE),
     "'{0}' not found — dependencies/tools not installed in the worktree"),
    (re.compile(r"No binary for (\w+) browser"),
     "{0} is not installed (Karma needs it; set CHROME_BIN)"),
    (re.compile(r"Cannot start (ChromeHeadless|Chrome|FirefoxHeadless)"),
     "{0} could not start on this machine"),
)


def environment_failure(output: str) -> str:
    """Why the tests could not even start, or "" when they did run."""
    text = _ANSI.sub("", output or "")
    for pattern, reason in _ENV_FAILURES:
        if m := pattern.search(text):
            return reason.format(m.group(1))
    return ""


def _node_install_commands(work_dir: str) -> list[str]:
    """How to give a Node worktree its dependencies — empty when it has them already
    (or is not a Node project). ``npm ci`` first: exact, and fast from the cache. A lock
    out of sync with package.json makes it refuse, so ``npm install`` is the fallback —
    in a throwaway worktree the lock it rewrites is never committed."""
    root = Path(work_dir)
    if not (root / "package.json").is_file() or (root / "node_modules").is_dir():
        return []
    flags = "--no-audit --no-fund"
    if (root / "package-lock.json").is_file():
        return [f"npm ci {flags}", f"npm install {flags}"]
    return [f"npm install {flags}"]


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
        elif m := _TS_ERROR.search(line):
            add(f"build {m['code']}: {m['msg'].replace(chr(92), '/')}")
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

        # A fresh worktree of a Node repo has no node_modules, so its runner (`ng`, `jest`)
        # does not exist yet. Install first — real evidence beats a skip. Only for the
        # detected command: an operator's own command owns its own setup.
        if not configured and self._config.test_install_dependencies:
            problem = await self._install_node_deps(work_dir, repo, timeout)
            if problem:
                return TestResult(passed=True, ran=False,
                                  summary=f"skipped — test environment not ready: {problem}")

        self._log.info("running test gate", dir=work_dir, repo=repo, cmd=cmd, timeout=timeout)
        try:
            env = _browser_env() if not configured else None
            code, text = await self._shell(cmd, work_dir, timeout, env=env)
        except TimeoutError:
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

        passed = code == 0
        if not passed and (why := environment_failure(text)):
            # Red because the suite could not START — the machine, not the change.
            self._log.warning("test gate: environment not ready — skipping", dir=work_dir,
                              repo=repo, reason=why)
            return TestResult(passed=True, ran=False,
                              summary=f"skipped — test environment not ready: {why}",
                              output_tail=text[-_OUTPUT_TAIL_CHARS:])
        failures = [] if passed else failure_signatures(text, work_dir)
        self._log.info("test gate done", dir=work_dir, passed=passed, code=code,
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
            summary = f"tests failed (exit {code}): " + ", ".join(parts)
        else:
            summary = f"tests failed (exit {code})"
        return TestResult(
            passed=passed,
            ran=True,
            summary=summary,
            output_tail=text[-_OUTPUT_TAIL_CHARS:],
            failures=failures,
        )

    async def _shell(
        self, cmd: str, cwd: str, limit_s: float, env: dict | None = None,
    ) -> tuple[int, str]:
        """Run ``cmd`` in a shell; ``(exit code, combined output)``. Raises TimeoutError
        after killing the WHOLE tree — the runner is the shell's child, so killing the
        shell alone would leave it holding our stdout pipe. See ai_autopilot.proc."""
        # A shell on purpose: the command is the operator's `test_command` from config
        # (or a fixed detected one) — never read from the branch the agent wrote.
        # autopilot:ignore[py-shell-true] operator-configured test command
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, **spawn_kwargs(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=limit_s)
        except TimeoutError:
            await terminate_tree(proc)
            raise
        code = proc.returncode if proc.returncode is not None else -1
        return code, (out or b"").decode("utf-8", "replace")

    async def _install_node_deps(self, work_dir: str, repo: str, limit_s: float) -> str:
        """Install a Node worktree's dependencies when it has none. "" when ready (or not
        a Node project); otherwise why it could not be made ready."""
        commands = _node_install_commands(work_dir)
        if not commands:
            return ""
        if not shutil.which("npm"):
            return "'npm' not found on PATH"
        last = ""
        for cmd in commands:
            self._log.info("test gate: installing dependencies", dir=work_dir, repo=repo,
                           cmd=cmd)
            try:
                code, text = await self._shell(cmd, work_dir, limit_s)
            except TimeoutError:
                return f"`{cmd}` timed out after {limit_s:.0f}s"
            except Exception as exc:  # noqa: BLE001
                return f"`{cmd}` could not start: {describe_exc(exc)}"
            if code == 0:
                return ""
            # Keep the one line that says why (npm prints a long usage block after it).
            reason = next((ln.strip() for ln in text.splitlines()
                           if "npm error" in ln and len(ln.strip()) > 12
                           and "code " not in ln), f"exit {code}")
            last = f"`{cmd.split(' --')[0]}` failed: {reason[:200]}"
            self._log.warning("test gate: dependency install failed", dir=work_dir,
                              repo=repo, cmd=cmd, reason=reason[:200])
        return last
