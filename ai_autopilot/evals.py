"""Regression tests for the agent's own configuration.

This repository has over a thousand tests for its Python and none at all for the thing
that actually decides what the agent produces: the skills, rules and CLAUDE.md under
``.claude``. Editing one of those changes the product's behaviour, and until now the
only way to find out whether it changed for the better was to ship it.

An eval is a real task plus the checks that say what an acceptable answer looks like.
The suite runs the agent non-interactively over every case and reports a pass rate, so
a change to a skill can be gated the way a change to code is: run it, compare, review
the drop before it merges.

Deliberately narrow in what it asserts. A check is a FACT about the run — text that must
appear, a file that must exist, a command that must exit zero — never a judgement about
style. Facts survive a model upgrade; judgements do not, and an eval that fails for the
wrong reason is worse than no eval, because the team learns to ignore it.

Cases live as YAML or JSON under ``evals/``. Nothing here reaches the network on its
own: the runner is injected, so the whole harness is testable without spending a token.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_autopilot.logging_config import describe_exc, get_logger

_log = get_logger("evals")

# A check that shells out gets its own ceiling: one hung case must not hold the whole
# suite, which is expected to run in CI where nobody is watching it.
_CHECK_TIMEOUT_SECONDS = 300


@dataclass
class Check:
    """One fact that must hold about a run.

    ``kind`` is the fact being asserted:

    - ``contains`` / ``not_contains`` — ``value`` appears (or does not) in the output
    - ``regex`` — ``value`` matches the output
    - ``file_exists`` / ``file_absent`` — ``value`` is a path under the case's cwd
    - ``file_contains`` — ``path`` exists and holds ``value``
    - ``shell`` — ``value`` runs in the case's cwd and exits zero

    Text comparison is case-insensitive. An eval must not fail because a model chose a
    capital letter; a check sensitive to that is asserting style, not behaviour.
    """

    kind: str
    value: str = ""
    path: str = ""

    def describe(self) -> str:
        target = self.path or self.value
        return f"{self.kind}: {target[:120]}"


@dataclass
class EvalCase:
    """One task, with the checks that define an acceptable outcome."""

    name: str
    prompt: str
    checks: list[Check] = field(default_factory=list)
    cwd: str = ""                    # where the agent works; blank = the case file's dir
    allowed_tools: list[str] | None = None
    timeout_seconds: int = 900
    source: str = ""                 # file it came from, for the report

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str = "") -> EvalCase:
        checks: list[Check] = []
        for raw in data.get("checks") or []:
            if isinstance(raw, str):        # "contains: something" shorthand
                kind, _, value = raw.partition(":")
                checks.append(Check(kind.strip(), value.strip()))
            elif isinstance(raw, dict):
                checks.append(Check(
                    str(raw.get("kind", "")).strip(),
                    str(raw.get("value", "")),
                    str(raw.get("path", "")),
                ))
        return cls(
            name=str(data.get("name") or Path(source).stem or "unnamed"),
            prompt=str(data.get("prompt") or ""),
            checks=checks,
            cwd=str(data.get("cwd") or ""),
            allowed_tools=data.get("allowed_tools"),
            timeout_seconds=int(data.get("timeout_seconds") or 900),
            source=source,
        )


@dataclass
class CaseResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    error: str = ""
    duration_seconds: float = 0.0
    output: str = ""


@dataclass
class SuiteResult:
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def pass_rate(self) -> float:
        """Share of cases that passed, 0.0-1.0. An EMPTY suite is 0.0, not 1.0.

        A suite that loaded nothing has proved nothing, and reporting that as a clean
        run is how a bad path or a wrong glob becomes a green gate checking air.
        """
        return (self.passed / self.total) if self.total else 0.0


def load_cases(directory: str | Path) -> list[EvalCase]:
    """Every case under ``directory`` (``*.yaml``, ``*.yml``, ``*.json``), by name.

    A file that will not parse is reported and skipped rather than taking the suite down
    with it: one malformed case must not hide the verdict of the other forty.
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    out: list[EvalCase] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in (".yaml", ".yml", ".json") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if path.suffix.lower() == ".json" else _yaml_load(text)
        except Exception as exc:  # noqa: BLE001 — one bad file must not hide the rest
            _log.warning("eval case failed to parse - skipped",
                         path=str(path), error=describe_exc(exc))
            continue
        for entry in (data if isinstance(data, list) else [data]):
            if isinstance(entry, dict) and entry.get("prompt"):
                out.append(EvalCase.from_dict(entry, source=str(path)))
    return sorted(out, key=lambda c: c.name)


def _yaml_load(text: str) -> Any:
    import yaml

    return yaml.safe_load(text)


async def _shell_ok(command: str, cwd: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_shell(
        command, cwd=cwd or None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"timed out after {_CHECK_TIMEOUT_SECONDS}s"
    return proc.returncode == 0, (out or b"").decode("utf-8", "replace")[-2000:]


async def apply_checks(case: EvalCase, output: str, cwd: str) -> list[str]:
    """Which of the case's checks did NOT hold — empty means it passed.

    A case with no checks FAILS. "The agent said something" is not a standard, and a
    case asserting nothing quietly lifts the pass rate while testing nothing.
    """
    if not case.checks:
        return ["case declares no checks - it asserts nothing"]

    low = (output or "").lower()
    root = Path(cwd) if cwd else Path.cwd()
    failures: list[str] = []
    for check in case.checks:
        kind = (check.kind or "").strip().lower()
        value = check.value or ""
        try:
            if kind == "contains":
                ok = value.lower() in low
            elif kind == "not_contains":
                ok = value.lower() not in low
            elif kind == "regex":
                ok = bool(re.search(value, output or "", re.I | re.S))
            elif kind == "file_exists":
                ok = (root / value).exists()
            elif kind == "file_absent":
                ok = not (root / value).exists()
            elif kind == "file_contains":
                target = root / (check.path or value)
                ok = target.is_file() and value.lower() in target.read_text(
                    encoding="utf-8", errors="replace").lower()
            elif kind == "shell":
                ok, detail = await _shell_ok(value, str(root))
                if not ok:
                    failures.append(f"{check.describe()} -> {detail.strip()[-300:]}")
                    continue
            else:
                failures.append(f"unknown check kind {kind!r}")
                continue
        except Exception as exc:  # noqa: BLE001 — a broken check is a failed check
            failures.append(f"{check.describe()} -> {describe_exc(exc)}")
            continue
        if not ok:
            failures.append(check.describe())
    return failures


# The runner is injected so the harness can be exercised without a model call.
# Signature: (prompt, cwd, allowed_tools, timeout_seconds) -> the agent's final text.
Runner = Callable[[str, str, "list[str] | None", int], Awaitable[str]]


async def run_case(case: EvalCase, runner: Runner) -> CaseResult:
    started = time.monotonic()
    cwd = case.cwd or str(Path(case.source).parent if case.source else Path.cwd())
    try:
        output = await runner(case.prompt, cwd, case.allowed_tools, case.timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — a case that crashes is a case that failed
        return CaseResult(case.name, False, error=describe_exc(exc),
                          duration_seconds=time.monotonic() - started)
    failures = await apply_checks(case, output, cwd)
    return CaseResult(
        name=case.name, passed=not failures, failures=failures,
        duration_seconds=time.monotonic() - started, output=(output or "")[-4000:],
    )


async def run_suite(cases: list[EvalCase], runner: Runner,
                    concurrency: int = 1) -> SuiteResult:
    """Run every case. ``concurrency`` > 1 runs that many at once.

    Serial by default: these are full agent sessions, and the suite competes for the
    same rate limit as the work it exists to protect.
    """
    result = SuiteResult()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(case: EvalCase) -> CaseResult:
        async with sem:
            _log.info("eval case starting", case=case.name)
            got = await run_case(case, runner)
            _log.info("eval case done", case=case.name, passed=got.passed,
                      failures=got.failures[:3])
            return got

    result.cases = list(await asyncio.gather(*(one(c) for c in cases)))
    return result


def claude_runner(config: Any) -> Runner:
    """A ``Runner`` driving the real agent with this machine's configuration."""

    async def _run(prompt: str, cwd: str, tools: list[str] | None,
                   timeout_seconds: int) -> str:
        from ai_autopilot.execution.claude_client import run_claude

        run = await run_claude(
            prompt, cwd,
            permission_mode=getattr(config, "claude_permission_mode", "acceptEdits"),
            allowed_tools=tools,
            model=getattr(config, "claude_model", "") or None,
            timeout_seconds=timeout_seconds,
            # The suite exists to test the configuration, so the filesystem sources that
            # carry it (skills, rules, CLAUDE.md) have to load — the SDK loads none by
            # default, which would leave the suite grading a stock agent.
            setting_sources=["project", "user"],
        )
        return run.text or ""

    return _run


def format_report(result: SuiteResult, min_pass_rate: float) -> str:
    """The suite as text for a terminal or a CI log."""
    lines = [
        "",
        f"  Eval suite - {result.passed}/{result.total} passed "
        f"({result.pass_rate * 100:.0f}%), threshold {min_pass_rate * 100:.0f}%",
        "",
    ]
    for case in result.cases:
        mark = "PASS" if case.passed else "FAIL"
        lines.append(f"  [{mark}] {case.name}  ({case.duration_seconds:.0f}s)")
        if case.error:
            lines.append(f"         error: {case.error}")
        for failure in case.failures[:5]:
            lines.append(f"         x {failure}")
    if not result.total:
        lines.append("  No cases loaded - an empty suite proves nothing, so this fails.")
    lines.append("")
    return "\n".join(lines)
