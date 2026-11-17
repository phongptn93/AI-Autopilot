"""Sign the Azure DevOps MCP server in with the autopilot's PAT — no browser popup.

``@azure-devops/mcp`` defaults to ``--authentication interactive``: an MSAL login whose
token lives only in that one process's memory. Every agent run starts a fresh server per
organization, so every run asked a human to sign in again — the popup appeared on the
desktop while nobody was watching, and the run stalled on it until it timed out.

The autopilot already holds a PAT for each organization it talks to (the machine's
connection, plus any workspace that declares its own). This module hands that PAT to the
server through ``--authentication pat`` and the ``PERSONAL_ACCESS_TOKEN`` variable of
THAT ONE child process:

* the workspace's ``.mcp.json`` is never rewritten — it is shared through git, and a
  teammate without the variable would lose ADO entirely;
* nothing is exported machine-wide — the token reaches only the server that needs it;
* a server whose author chose ``azcli``/``env``/``pat`` explicitly is left alone; only the
  interactive default is replaced.

Overriding by the SAME server name is what makes this work: a server passed through the
SDK replaces the project's ``.mcp.json`` entry of that name (verified against the CLI),
and the tool names skills refer to (``mcp__ado-newoceanis__wit_*``) stay unchanged.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

PACKAGE = "@azure-devops/mcp"
PAT_ENV = "PERSONAL_ACCESS_TOKEN"
_AUTH_FLAGS = ("-a", "--authentication")
#: Auth modes that need a human at the screen — the ones worth replacing.
_INTERACTIVE = {"", "interactive"}


@dataclass(frozen=True)
class AdoMcpServer:
    """What an ``.mcp.json`` entry says about one Azure DevOps MCP server."""

    name: str
    organization: str       # as written in the args: "newoceanis"
    auth: str               # "" when unset, which means interactive
    version: str            # "latest", "2.11.0", or "" when unpinned

    @property
    def interactive(self) -> bool:
        return self.auth in _INTERACTIVE


def organization_name(url_or_name: str) -> str:
    """``https://dev.azure.com/acme/`` → ``acme``; ``https://acme.visualstudio.com`` →
    ``acme``; a bare ``acme`` stays as is. Lower-cased: ADO org names are not
    case-sensitive and the config and ``.mcp.json`` are typed by different people."""
    raw = (url_or_name or "").strip().rstrip("/")
    if not raw:
        return ""
    if "://" not in raw:
        return raw.lower()
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host.endswith(".visualstudio.com"):
        return host.split(".", 1)[0]
    segments = [s for s in parsed.path.split("/") if s]
    return segments[0].lower() if segments else ""


def _package_index(args: list[str]) -> int:
    for i, arg in enumerate(args):
        if arg == PACKAGE or arg.startswith(PACKAGE + "@"):
            return i
    return -1


def describe(name: str, server: Any) -> AdoMcpServer | None:
    """The ADO view of one MCP entry, or ``None`` when it is some other server."""
    if not isinstance(server, dict):
        return None
    args = [str(a) for a in (server.get("args") or [])]
    at = _package_index(args)
    if at < 0:
        return None
    spec = args[at]
    version = spec[len(PACKAGE) + 1:] if spec.startswith(PACKAGE + "@") else ""
    organization, auth = "", ""
    rest = args[at + 1:]
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in _AUTH_FLAGS:
            auth = rest[i + 1].strip().lower() if i + 1 < len(rest) else ""
            i += 2
            continue
        if arg.startswith("--authentication="):
            auth = arg.split("=", 1)[1].strip().lower()
        elif not arg.startswith("-") and not organization:
            organization = arg
        i += 1
    return AdoMcpServer(name=name, organization=organization, auth=auth, version=version)


def pats_by_organization(config: Any) -> dict[str, str]:
    """Every (organization → PAT) pair the autopilot holds.

    The config passed in comes first, so a run's workspace-scoped settings win over the
    root for their own org. A workspace that declares a PAT but no organization shares the
    machine's org; ``setdefault`` keeps the first (most specific) one found.
    """
    out: dict[str, str] = {}
    root_org = organization_name(getattr(config, "ado_organization", "") or "")

    def add(org: str, pat: str) -> None:
        org, pat = organization_name(org), (pat or "").strip()
        if org and pat:
            out.setdefault(org, pat)

    add(root_org, getattr(config, "ado_pat", "") or "")
    for ws in getattr(config, "effective_workspaces", None) or []:
        add(getattr(ws, "ado_organization", "") or root_org, getattr(ws, "ado_pat", "") or "")
    return out


def _basic_value(pat: str) -> str:
    # The server sends this as-is after ``Basic``: base64 of ``user:pat``, user ignored.
    return base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")


def _without_auth_flags(args: list[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in _AUTH_FLAGS:
            skip = True
            continue
        if arg.startswith("--authentication="):
            continue
        out.append(arg)
    return out


def with_pat_auth(servers: dict | None, config: Any) -> dict | None:
    """``servers`` with each interactive ADO MCP entry switched to PAT auth.

    Returns a NEW dict — the caller's (often a cached ``.mcp.json`` read) is never
    mutated, so the PAT cannot leak back into anything that is later written to disk.
    Entries with no PAT for their organization are returned unchanged: they still prompt,
    and ``doctor`` says so.
    """
    if not servers or not bool(getattr(config, "ado_mcp_pat_auth", True)):
        return servers
    pats = pats_by_organization(config)
    pin = (getattr(config, "ado_mcp_version", "") or "").strip()
    out: dict = {}
    for name, server in servers.items():
        info = describe(name, server)
        pat = pats.get(organization_name(info.organization)) if info else None
        if info is None or not info.interactive or not pat:
            out[name] = server
            continue
        args = _without_auth_flags([str(a) for a in server.get("args") or []])
        if pin and info.version in ("", "latest"):
            # ``@latest`` makes npx resolve against the registry on every start — slow,
            # and a new major can change the tool surface mid-sprint.
            args[_package_index(args)] = f"{PACKAGE}@{pin}"
        out[name] = {
            **server,
            "args": [*args, "--authentication", "pat"],
            "env": {**(server.get("env") or {}), PAT_ENV: _basic_value(pat)},
        }
    return out


@dataclass(frozen=True)
class McpAuthStatus:
    """One ADO MCP server and how it will sign in on an autopilot run."""

    server: AdoMcpServer
    mode: str               # "pat" (autopilot's PAT) | "own" (author's choice) | "popup"

    @property
    def will_prompt(self) -> bool:
        return self.mode == "popup"


def audit(servers: dict | None, config: Any) -> list[McpAuthStatus]:
    """How each ADO MCP server in ``servers`` will sign in — for doctor and the UI.
    Pure: reads nothing, writes nothing, never touches a secret's value."""
    enabled = bool(getattr(config, "ado_mcp_pat_auth", True))
    pats = pats_by_organization(config)
    out: list[McpAuthStatus] = []
    for name, server in (servers or {}).items():
        info = describe(name, server)
        if info is None:
            continue
        if not info.interactive:
            mode = "own"
        elif enabled and organization_name(info.organization) in pats:
            mode = "pat"
        else:
            mode = "popup"
        out.append(McpAuthStatus(server=info, mode=mode))
    return out
