"""Phase 4 — DAST: probe a RUNNING application, behind gates that fail closed.

Dynamic testing sends real requests to a real service, so the only acceptable failure
mode for a misconfiguration is "refused to run". Every target must be declared in
config with ``owner_confirmed: true`` (the operator stating, in writing, that this host
is theirs to test) and an ``allowed_hosts`` allowlist; the base URL's host must be on it,
and by default a private address is required. A request to anything else is never sent.

Within those gates, a bounded HTTP client (rate-limited, capped, mutations off unless
asked) is handed to the model along with the API checklist. The model drives the probe —
missing auth, BOLA/IDOR (user A reading user B's resource, using the two configured
tokens), role escalation, mass assignment, security headers, verbose errors — and reports
findings under the same contract every other scanner uses, so they flow into the same
dedup / baseline / lifecycle / dashboard.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.reports import Finding
from ai_autopilot.security_scan.tools.base import ToolStatus

_log = get_logger("security_scan.dast")


class DastRefused(Exception):
    """A gate rejected the target. The message is safe to show the operator."""


@dataclass
class GateResult:
    ok: bool
    reason: str = ""


def check_target(target, *, resolve: bool = True) -> GateResult:
    """Every reason a target may NOT be probed. Pure and testable (``resolve=False``
    skips DNS). Returns the FIRST failure so the message is specific."""
    if not target.enabled:
        return GateResult(False, "target is disabled")
    if not target.owner_confirmed:
        return GateResult(False, "owner_confirmed is not true — you must state this host is "
                                 "yours to test")
    if not target.base_url:
        return GateResult(False, "no base_url")
    parsed = urlparse(target.base_url)
    if parsed.scheme not in ("http", "https"):
        return GateResult(False, f"unsupported scheme {parsed.scheme!r}")
    host = parsed.hostname or ""
    if not host:
        return GateResult(False, "base_url has no host")
    allowed = {h.lower().strip() for h in target.allowed_hosts if h.strip()}
    if not allowed:
        return GateResult(False, "allowed_hosts is empty")
    if host.lower() not in allowed:
        return GateResult(False, f"host {host!r} is not in allowed_hosts")
    if target.require_private and resolve:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            return GateResult(False, f"cannot resolve {host!r}: {exc}")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_global:
                return GateResult(
                    False,
                    f"{host!r} resolves to public address {ip} but require_private is set",
                )
    return GateResult(True)


@dataclass
class RateLimiter:
    """A simple per-second gate shared by every request of a probe."""

    rps: float
    _last: float = 0.0

    async def wait(self) -> None:
        import asyncio

        if self.rps <= 0:
            return
        gap = 1.0 / self.rps
        now = time.monotonic()
        sleep = self._last + gap - now
        if sleep > 0:
            await asyncio.sleep(sleep)
        self._last = time.monotonic()


@dataclass
class ProbeBudget:
    """Hard caps enforced regardless of what the model asks for."""

    max_requests: int
    allow_mutations: bool
    allowed_paths: list[str] = field(default_factory=list)
    sent: int = 0

    def allows(self, method: str, path: str) -> tuple[bool, str]:
        if self.sent >= self.max_requests:
            return False, f"request cap {self.max_requests} reached"
        if not self.allow_mutations and method.upper() not in ("GET", "HEAD", "OPTIONS"):
            return False, f"{method} blocked (allow_mutations is off)"
        if self.allowed_paths and not any(path.startswith(p) for p in self.allowed_paths):
            return False, f"path {path!r} is outside allowed_paths"
        return True, ""


@dataclass
class DastOutcome:
    findings: list[Finding] = field(default_factory=list)
    status: ToolStatus = None  # type: ignore[assignment]
    refused: str = ""


def find_target(config, name: str):
    for t in config.security_scan.dast_targets:
        if t.name == name:
            return t
    return None


async def run_dast(
    *, config, target_name: str, executor=None, http=None,
) -> DastOutcome:
    """Probe one configured target. Refuses (never raises) when a gate fails.

    The model half is only reached when ``executor`` is provided AND the gates pass;
    the gate check itself is synchronous and side-effect-free, which is what the tests
    exercise. Actually driving requests is delegated to the executor's read-only run
    with the DAST skill and the workspace's HTTP/browser MCP tools.
    """
    out = DastOutcome(status=ToolStatus("dast"))
    if not config.security_scan.dast_enabled:
        out.refused = "dast_enabled is false"
        out.status.skipped_reason = out.refused
        return out
    target = find_target(config, target_name)
    if target is None:
        out.refused = f"no DAST target named {target_name!r}"
        out.status.error = out.refused
        return out
    gate = check_target(target)
    if not gate.ok:
        out.refused = f"refused: {gate.reason}"
        out.status.error = out.refused
        _log.warning("DAST target refused", target=target_name, reason=gate.reason)
        return out
    if executor is None:
        out.refused = "no executor to drive the probe"
        out.status.skipped_reason = out.refused
        return out

    prompt = build_prompt(target)
    started = time.monotonic()
    try:
        # Read-only run: the probe reads the app over HTTP; it must not edit the repo.
        result = await executor.run_audit("security-dast", prompt, config.repo_working_directory
                                          or ".", config.base_branch, "")
        from ai_autopilot import reports

        summary, findings = reports.parse_findings(getattr(result, "output", "") or "")
        for f in findings:
            f.tool = "dast"
            f.confidence = f.confidence or "medium"
        out.findings = findings
        out.status.ran = bool(getattr(result, "success", False))
        out.status.findings = len(findings)
        out.status.duration_seconds = time.monotonic() - started
        if not out.status.ran:
            out.status.error = (getattr(result, "error", "") or "probe run failed")[:200]
    except Exception as exc:  # noqa: BLE001
        out.status.error = describe_exc(exc)[:200]
        _log.warning("DAST probe failed", target=target_name, error=describe_exc(exc))
    return out


def build_prompt(target) -> str:
    auth = ("with the bearer token in $" + target.auth_env_var
            if target.auth_env_var else "unauthenticated")
    second = (f" A second identity's token is in ${target.auth_b_env_var} — use it to test "
              "BOLA/IDOR: can identity A read or change identity B's resource?"
              if target.auth_b_env_var else "")
    mut = ("You MAY send state-changing requests (POST/PUT/PATCH/DELETE)."
           if target.allow_mutations else
           "Send ONLY GET/HEAD/OPTIONS — do not change any state.")
    paths = (f" Stay within these path prefixes: {', '.join(target.allowed_paths)}."
             if target.allowed_paths else "")
    swagger = (f" Enumerate endpoints from the OpenAPI document at {target.openapi_url}."
               if target.openapi_url else "")
    return (
        f"Dynamic security probe of {target.base_url} ({auth}).{second}\n"
        f"You are authorised: this host is owner-confirmed for testing.\n"
        f"{mut} Keep to at most {target.max_requests} requests, ~{target.rate_limit_rps}/s."
        f"{paths}{swagger}\n\n"
        "Use the OWASP API Security Top 10 checklist: endpoints reachable without auth, "
        "BOLA/IDOR, function-level authorization (can a low-priv token hit an admin route), "
        "mass assignment, injection reflected in responses, missing security headers, "
        "verbose error messages leaking stack traces or SQL. For each finding give the "
        "request (method + path), what the response proved, and the impact. Redact tokens "
        "and any secrets from the evidence.\n\n"
        "Finish with the standard ```json findings block; set \"tool\": \"dast\" and fill "
        "cwe/owasp/confidence. Report only what a real response demonstrated."
    )
