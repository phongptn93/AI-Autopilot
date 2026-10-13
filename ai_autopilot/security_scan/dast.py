"""Phase 4 — DAST: probe a RUNNING application, behind gates that fail closed.

Dynamic testing sends real requests to a real service, so the only acceptable failure
mode for a misconfiguration is "refused to run". Every target must be declared in
config with ``owner_confirmed: true`` (the operator stating, in writing, that this host
is theirs to test) and an ``allowed_hosts`` allowlist; the base URL's host must be on it,
and by default a private address is required. A request to anything else is never sent.

Within those gates the model gets ONE network tool, ``http_request`` (an in-process MCP
server built here), and nothing else that can reach the network: Bash, WebFetch,
WebSearch, sub-agents and the workspace's MCP servers are denied for the run. Every
request therefore passes ``probe_request``, which pins the host, takes only a path,
never follows a redirect, and enforces the request cap, the rate limit, the mutation
switch and ``allowed_paths`` in code — the prompt states the limits, it does not carry
them. The tokens are injected here and redacted from responses; the model never sees
them. The model drives the probe —
missing auth, BOLA/IDOR (user A reading user B's resource, using the two configured
tokens), role escalation, mass assignment, security headers, verbose errors — and reports
findings under the same contract every other scanner uses, so they flow into the same
dedup / baseline / lifecycle / dashboard.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import posixpath
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import httpx

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


# Everything else that can reach the network is denied for a DAST run, so the gate
# below is the only way out. Sub-agents are denied too: they would not inherit it.
DAST_DENY = ("Bash", "WebFetch", "WebSearch", "Task", "Agent")
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
_BODY_LIMIT = 4000
_REQUEST_TIMEOUT = 20.0


def _text(payload: object, *, error: bool = False) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    out: dict = {"content": [{"type": "text", "text": body}]}
    if error:
        out["is_error"] = True
    return out


async def probe_request(
    args: Mapping, *, target, budget: ProbeBudget, limiter: RateLimiter,
    http: httpx.AsyncClient, env: Mapping[str, str] | None = None,
) -> dict:
    """Send ONE request on the model's behalf — or refuse it, saying why.

    The model supplies a path, never a URL, so the host cannot be changed; a
    scheme-relative ``//host`` or a full URL is refused. Redirects are returned, not
    followed (a 302 to another host would leave the allowlist). ``identity`` picks
    which configured token to attach: "a", "b" or "none".
    """
    env = os.environ if env is None else env
    method = str(args.get("method") or "GET").upper()
    path = str(args.get("path") or "")
    if method not in _SAFE_METHODS:
        return _text(f"REFUSED: method {method!r} is not supported", error=True)
    bare = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/") or path.startswith("//") or "://" in bare:
        return _text("REFUSED: give a path starting with '/', not a URL", error=True)
    # `/api/../admin` passes a startswith("/api") prefix check and the server resolves it
    # to /admin, so a path must already be in normal form — encoded dots included.
    decoded = unquote(bare)
    if posixpath.normpath(decoded) != decoded.rstrip("/") and decoded != "/":
        return _text("REFUSED: path must not contain '.', '..' or '//' segments", error=True)
    ok, reason = budget.allows(method, decoded)
    if not ok:
        return _text(f"REFUSED: {reason}", error=True)

    url = target.base_url.rstrip("/") + path
    if (urlparse(url).hostname or "").lower() != (urlparse(target.base_url).hostname or "").lower():
        return _text("REFUSED: request would leave the target host", error=True)

    headers = {str(k): str(v) for k, v in dict(args.get("headers") or {}).items()}
    identity = str(args.get("identity") or "a").lower()
    var = {"a": target.auth_env_var, "b": target.auth_b_env_var}.get(identity, "")
    secrets = [env.get(v, "") for v in (target.auth_env_var, target.auth_b_env_var) if v]
    if var and env.get(var):
        headers.setdefault(target.auth_header, f"{target.auth_scheme} {env[var]}".strip())

    await limiter.wait()
    budget.sent += 1
    body = args.get("body")
    try:
        resp = await http.request(
            method, url, headers=headers, timeout=_REQUEST_TIMEOUT, follow_redirects=False,
            **({"content": body} if isinstance(body, str) else
               {"json": body} if body is not None else {}),
        )
    except httpx.HTTPError as exc:
        return _text(f"request failed: {describe_exc(exc)}", error=True)

    def redact(text: str) -> str:
        for s in secrets:
            if s:
                text = text.replace(s, "[REDACTED]")
        return text

    return _text({
        "status": resp.status_code,
        "headers": {k: redact(v) for k, v in resp.headers.items()},
        "body": redact(resp.text[:_BODY_LIMIT]),
        "truncated": len(resp.text) > _BODY_LIMIT,
        "requests_left": budget.max_requests - budget.sent,
    })


def http_gate(target, http: httpx.AsyncClient, *, env: Mapping[str, str] | None = None):
    """The in-process MCP server exposing ``http_request``, and the budget it spends."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    budget = ProbeBudget(target.max_requests, target.allow_mutations, list(target.allowed_paths))
    limiter = RateLimiter(target.rate_limit_rps)
    schema = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": list(_SAFE_METHODS)},
            "path": {"type": "string", "description": "Path + query under the target, e.g. "
                                                      "/api/orders/7?x=1"},
            "identity": {"type": "string", "enum": ["a", "b", "none"],
                         "description": "Which configured token to send (default a)"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "body": {"description": "JSON value, or a raw string"},
        },
        "required": ["method", "path"],
    }

    @tool("http_request", f"Send one HTTP request to {target.base_url}. The only way to "
          "reach the target; limits are enforced and refusals say why.", schema)
    async def http_request(args: dict) -> dict:
        return await probe_request(args, target=target, budget=budget, limiter=limiter,
                                   http=http, env=env)

    return create_sdk_mcp_server("dast", tools=[http_request]), budget


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
        # A caller's client stays open; one made here is closed with the run.
        async with contextlib.AsyncExitStack() as stack:
            client = http or await stack.enter_async_context(httpx.AsyncClient())
            server, budget = http_gate(target, client)
            # Read-only run (it must not edit the repo) whose ONLY network path is the gate.
            result = await executor.run_audit(
                "security-dast", prompt, config.repo_working_directory or ".",
                config.base_branch, "", mcp_servers={"dast": server}, extra_disallowed=DAST_DENY,
            )
        _log.info("DAST probe finished", target=target_name, requests=budget.sent)
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
    auth = ("as identity A (its token is attached for you)"
            if target.auth_env_var else "unauthenticated")
    second = (" A second identity is available — pass identity \"b\" to test BOLA/IDOR: "
              "can identity A read or change identity B's resource?"
              if target.auth_b_env_var else "")
    mut = ("You MAY send state-changing requests (POST/PUT/PATCH/DELETE)."
           if target.allow_mutations else
           "Send ONLY GET/HEAD/OPTIONS — do not change any state.")
    paths = (f" Stay within these path prefixes: {', '.join(target.allowed_paths)}."
             if target.allowed_paths else "")
    spec = urlparse(target.openapi_url) if target.openapi_url else None
    same_host = spec is not None and spec.hostname == urlparse(target.base_url).hostname
    swagger = (f" Enumerate endpoints from the OpenAPI document — fetch {spec.path or '/'}"
               f"{'?' + spec.query if spec.query else ''} with the tool first."
               if spec is not None and same_host else
               " (The configured OpenAPI document is on another host and cannot be fetched "
               "in this run; discover endpoints by probing.)" if spec is not None else "")
    return (
        f"Dynamic security probe of {target.base_url} ({auth}).{second}\n"
        f"You are authorised: this host is owner-confirmed for testing.\n"
        "Send every request with the mcp__dast__http_request tool (method + path; "
        "identity \"none\" sends no token). It is your only network access — there is no "
        "shell or web tool in this run — and it enforces the limits below, telling you "
        "when a request is refused.\n"
        f"{mut} At most {target.max_requests} requests, ~{target.rate_limit_rps}/s."
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
