"""The adapter contract every scanner implements, and the subprocess plumbing they share.

A scanner is optional by construction: ``available()`` says whether its binary is on
this machine, and the runner records "skipped: not installed" rather than failing the
scan. That is the difference between a tool a team can adopt incrementally and one
that needs four installers before it says anything.

``run_json`` is the one place a scanner subprocess is spawned. It bounds the run with a
timeout, kills the whole tree on expiry (``proc.terminate_tree`` — scanners fork
workers), caps captured output, and turns every failure into a :class:`ToolStatus`
the report can show. No adapter raises.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ai_autopilot import proc
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.reports import Finding

_log = get_logger("security_scan.tools")

# A scanner that prints more than this is answering a different question than the one
# we asked (we always request JSON). Bounded so a runaway tool cannot exhaust memory.
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024


@dataclass
class ToolStatus:
    """How one scanner's run went — shown next to the findings so "0 findings" can be
    read as "clean" or "did not run", which are opposite answers."""

    name: str
    ran: bool = False
    skipped_reason: str = ""      # "not installed", "nothing to scan", "disabled"
    error: str = ""
    duration_seconds: float = 0.0
    findings: int = 0
    version: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.error:
            return f"error: {self.error}"
        if not self.ran:
            return f"skipped ({self.skipped_reason or 'n/a'})"
        # What was deliberately left out is part of the answer: "0 findings" and
        # "0 findings, 4 excused inline" are different statements about a codebase.
        excused = ", ".join(
            f"{self.extra[k]} {k.replace('_', ' ')}"
            for k in ("inline_ignored", "allowlisted") if self.extra.get(k)
        )
        base = f"{self.findings} finding(s) in {self.duration_seconds:.1f}s"
        return f"{base} ({excused})" if excused else base


@dataclass
class ToolRun:
    findings: list[Finding]
    status: ToolStatus


class ScannerAdapter(Protocol):
    name: str

    def available(self) -> bool: ...

    async def run(self, repo: str, files: list[str] | None = None) -> ToolRun: ...


def which(binary: str) -> str:
    """Absolute path of ``binary`` on PATH, or "" — the availability check."""
    return shutil.which(binary) or ""


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.error


async def run_process(
    argv: list[str], cwd: str, *, timeout_seconds: float, env: dict | None = None,
) -> ProcResult:
    """Run ``argv`` in ``cwd`` and capture its output. Never raises.

    ``returncode`` is reported, not judged: semgrep/gitleaks/npm audit all exit non-zero
    when they FIND something, so "non-zero" is what a successful scan looks like.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **proc.spawn_kwargs(),
        )
    except (OSError, ValueError) as exc:
        return ProcResult(-1, "", "", error=describe_exc(exc))
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await proc.terminate_tree(process)
        return ProcResult(
            -1, "", "", timed_out=True, error=f"timed out after {timeout_seconds:.0f}s"
        )
    except Exception as exc:  # noqa: BLE001
        await proc.terminate_tree(process)
        return ProcResult(-1, "", "", error=describe_exc(exc))
    return ProcResult(
        process.returncode if process.returncode is not None else -1,
        out[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        err[:65536].decode("utf-8", errors="replace"),
    )


def parse_json(text: str) -> object | None:
    """``json.loads`` that tolerates a tool's banner before the document.

    ``npm audit`` and ``dotnet list`` both sometimes print a line of prose first. Find
    the first ``{`` or ``[`` and parse from there; None when nothing parses.
    """
    text = (text or "").strip()
    if not text:
        return None
    for opener in ("{", "["):
        idx = text.find(opener)
        if idx < 0:
            continue
        try:
            return json.loads(text[idx:])
        except (ValueError, TypeError):
            continue
    return None


def rel(repo: str, path: str) -> str:
    """``path`` relative to ``repo`` with forward slashes — the form fingerprints use."""
    try:
        p = Path(path)
        if p.is_absolute():
            p = p.resolve().relative_to(Path(repo).resolve())
        return strip_dot_slash(p.as_posix())
    except (ValueError, OSError):
        return strip_dot_slash(str(path).replace("\\", "/"))


def strip_dot_slash(path: str) -> str:
    """``./x`` → ``x`` — the PREFIX only. ``str.lstrip("./")`` strips characters, and
    turned ``.claude/hooks`` into ``claude/hooks``, which is a different file."""
    while path.startswith("./"):
        path = path[2:]
    return path


def clip(text: str, limit: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
