"""semgrep adapter — rule-based SAST with real dataflow, when it is installed.

Invoked with ``--json`` and a ruleset list (default: the OWASP pack plus the language
packs for this workspace's stack). Exit codes: 0 clean, 1 findings, anything else an
error — but the JSON is authoritative either way, so the code is only logged.
"""

from __future__ import annotations

import re
import time

from ai_autopilot.reports import Finding, normalise_severity
from ai_autopilot.security_scan.tools.base import (
    ToolRun,
    ToolStatus,
    clip,
    parse_json,
    rel,
    run_process,
    which,
)

DEFAULT_RULESETS = ("p/owasp-top-ten", "p/secrets", "p/csharp", "p/typescript", "p/python")
_TIMEOUT = 20 * 60

_SEV = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
_CONF = {"HIGH": "high", "MEDIUM": "medium", "LOW": "low"}


class SemgrepScanner:
    name = "semgrep"

    def __init__(self, rulesets: list[str] | None = None, timeout: float = _TIMEOUT) -> None:
        self._rulesets = list(rulesets or DEFAULT_RULESETS)
        self._timeout = timeout

    def available(self) -> bool:
        return bool(which("semgrep"))

    async def run(self, repo: str, files: list[str] | None = None) -> ToolRun:
        started = time.monotonic()
        if not self.available():
            return ToolRun([], ToolStatus(self.name, skipped_reason="not installed"))
        argv = ["semgrep", "scan", "--json", "--quiet", "--metrics=off", "--no-git-ignore"]
        for rs in self._rulesets:
            argv += ["--config", rs]
        argv += files if files else ["."]
        res = await run_process(argv, repo, timeout_seconds=self._timeout)
        status = ToolStatus(self.name, duration_seconds=time.monotonic() - started)
        if not res.ok:
            status.error = res.error
            return ToolRun([], status)
        data = parse_json(res.stdout)
        if not isinstance(data, dict):
            status.error = clip(res.stderr or "no JSON output", 200)
            return ToolRun([], status)
        findings = parse_results(data, repo)
        status.ran, status.findings = True, len(findings)
        status.version = str(data.get("version") or "")
        errors = data.get("errors") or []
        if errors:
            status.extra["errors"] = len(errors)
        return ToolRun(findings, status)


def parse_results(data: dict, repo: str = "") -> list[Finding]:
    """The ``results`` array of semgrep's JSON → findings. Tolerant of missing keys."""
    out: list[Finding] = []
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        extra = r.get("extra") or {}
        meta = extra.get("metadata") or {}
        sev_meta = str(meta.get("severity") or meta.get("impact") or "").upper()
        severity = normalise_severity(sev_meta) if sev_meta else _SEV.get(
            str(extra.get("severity") or "").upper(), "medium"
        )
        if sev_meta and severity == "info" and sev_meta not in ("INFO",):
            severity = _SEV.get(str(extra.get("severity") or "").upper(), "medium")
        cwe = meta.get("cwe") or ""
        owasp = meta.get("owasp") or ""
        if isinstance(owasp, list):
            owasp = owasp[0] if owasp else ""
        start = r.get("start") or {}
        out.append(Finding.from_dict({
            "severity": severity,
            "title": clip(str(extra.get("message") or r.get("check_id") or ""), 160),
            "file": rel(repo, str(r.get("path") or "")) if repo else str(r.get("path") or ""),
            "line": start.get("line"),
            "detail": clip(str(extra.get("message") or ""), 600),
            "tool": "semgrep",
            "rule_id": str(r.get("check_id") or "").rsplit(".", 1)[-1],
            "cwe": cwe,
            "owasp": _short_owasp(str(owasp)),
            "confidence": _CONF.get(str(meta.get("confidence") or "").upper(), "medium"),
            "snippet": clip(str(extra.get("lines") or ""), 300),
        }))
    return out


_OWASP_ID = re.compile(r"\b(A\d{2}:\d{4}|API\d{1,2}:\d{4}|M\d{1,2}:\d{4})\b")


def _short_owasp(text: str) -> str:
    """``"A03:2021 - Injection"`` → ``"A03:2021"``; "" when there is no id in it."""
    m = _OWASP_ID.search(text or "")
    return m.group(1) if m else ""
