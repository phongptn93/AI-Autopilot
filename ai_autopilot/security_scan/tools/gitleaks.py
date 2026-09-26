"""gitleaks adapter — secrets in the working tree, and in history when asked.

``detect --no-git`` scans files on disk (what a full scan wants). With ``log_opts`` it
walks commits instead — that is how the pre-PR gate asks "did THIS branch add a secret",
which a tree scan cannot answer once the secret was committed and then deleted.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from ai_autopilot.reports import Finding
from ai_autopilot.security_scan.tools.base import (
    ToolRun,
    ToolStatus,
    clip,
    parse_json,
    rel,
    run_process,
    which,
)

_TIMEOUT = 10 * 60


class GitleaksScanner:
    name = "gitleaks"

    def __init__(self, log_opts: str = "", timeout: float = _TIMEOUT) -> None:
        self._log_opts = log_opts
        self._timeout = timeout

    def available(self) -> bool:
        return bool(which("gitleaks"))

    async def run(self, repo: str, files: list[str] | None = None) -> ToolRun:
        started = time.monotonic()
        if not self.available():
            return ToolRun([], ToolStatus(self.name, skipped_reason="not installed"))
        # gitleaks only writes JSON to a file; it prints a banner to stdout.
        fd, report = tempfile.mkstemp(prefix="gitleaks-", suffix=".json")
        os.close(fd)
        try:
            argv = ["gitleaks", "detect", "--source", ".", "--report-format", "json",
                    "--report-path", report, "--exit-code", "0", "--no-banner"]
            if self._log_opts:
                argv += ["--log-opts", self._log_opts]
            else:
                argv.append("--no-git")
            res = await run_process(argv, repo, timeout_seconds=self._timeout)
            status = ToolStatus(self.name, duration_seconds=time.monotonic() - started)
            if not res.ok:
                status.error = res.error
                return ToolRun([], status)
            try:
                data = parse_json(Path(report).read_text(encoding="utf-8"))  # noqa: ASYNC240
            except OSError:
                data = None
            if data is None:
                if res.returncode not in (0, 1):
                    status.error = clip(res.stderr or f"exit {res.returncode}", 200)
                    return ToolRun([], status)
                data = []
            findings = parse_results(data if isinstance(data, list) else [], repo)
            if files:
                wanted = {rel(repo, f) for f in files}
                findings = [f for f in findings if f.file in wanted]
            status.ran, status.findings = True, len(findings)
            return ToolRun(findings, status)
        finally:
            with __import__("contextlib").suppress(OSError):
                os.unlink(report)


def parse_results(rows: list, repo: str = "") -> list[Finding]:
    out: list[Finding] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        secret = str(r.get("Secret") or "")
        line_text = str(r.get("Line") or r.get("Match") or "")
        if secret and len(secret) >= 8:
            line_text = line_text.replace(secret, secret[:4] + "…" + secret[-2:])
        commit = str(r.get("Commit") or "")
        out.append(Finding.from_dict({
            "severity": "critical",
            "title": f"Secret detected: {r.get('Description') or r.get('RuleID') or 'unknown'}",
            "file": rel(repo, str(r.get("File") or "")) if repo else str(r.get("File") or ""),
            "line": r.get("StartLine"),
            "detail": (f"Committed in {commit[:10]} by {r.get('Author') or '?'}. "
                       if commit else "") + "Rotate the credential; removing the line does "
                       "not un-leak it.",
            "tool": "gitleaks",
            "rule_id": str(r.get("RuleID") or "secret"),
            "cwe": "CWE-798",
            "owasp": "A07:2021",
            "confidence": "high",
            "snippet": clip(line_text.strip(), 300),
        }))
    return out
