"""The Azure DevOps MCP server signs in with the autopilot's PAT, not a browser popup.

``@azure-devops/mcp`` defaults to an interactive login kept only in that process's
memory, so every agent run opened a Microsoft sign-in window and stalled on it. Runs now
pass the PAT of the server's organization to that one child process. What must hold:

* the right PAT for the right org — a second org's server must never get the first's;
* only the interactive default is replaced; an author's explicit choice stands;
* the caller's dict (a read of the shared .mcp.json) is never mutated;
* no PAT → unchanged, and doctor says the popup is coming.
"""

from __future__ import annotations

import base64
import json

from ai_autopilot.ado.mcp_auth import (
    PAT_ENV,
    audit,
    describe,
    organization_name,
    pats_by_organization,
    with_pat_auth,
)
from ai_autopilot.config import Settings, WorkspaceConfig

ROOT = "https://dev.azure.com/newoceanis"
OTHER = "https://dev.azure.com/Industrial-nois"


def _settings(**over) -> Settings:
    base = dict(dry_run=True, ado_organization=ROOT, ado_pat="ROOT-PAT", ado_project="DxFactory",
                database_url="sqlite+aiosqlite:///:memory:")
    base.update(over)
    return Settings(**base)


def _two_orgs(**over) -> Settings:
    return _settings(workspaces=[WorkspaceConfig(
        name="Industrial", ado_projects=["Khatoco"], ado_organization=OTHER,
        ado_pat="OTHER-PAT", workspace_directory="/tmp/ind",
    )], **over)


def _server(org: str, *extra: str, version: str = "latest") -> dict:
    return {"type": "stdio", "command": "npx",
            "args": ["-y", f"@azure-devops/mcp@{version}", org, *extra], "env": {}}


def _decoded(server: dict) -> str:
    return base64.b64decode(server["env"][PAT_ENV]).decode()


# ── parsing ──────────────────────────────────────────────────────────────────


def test_organization_name_accepts_every_form():
    assert organization_name("https://dev.azure.com/Acme/") == "acme"
    assert organization_name("https://acme.visualstudio.com") == "acme"
    assert organization_name("Acme") == "acme"
    assert organization_name("") == ""


def test_describe_reads_org_auth_and_version():
    info = describe("ado", _server("newoceanis", "-a", "azcli", version="2.11.0"))
    assert (info.organization, info.auth, info.version) == ("newoceanis", "azcli", "2.11.0")
    assert describe("ado", _server("x", "--authentication=pat")).auth == "pat"
    assert describe("ado", {"command": "npx", "args": ["-y", "@azure-devops/mcp", "x"]}).version == ""


def test_describe_ignores_other_servers():
    assert describe("pw", {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}) is None
    assert describe("odd", "not a dict") is None


# ── the rewrite ──────────────────────────────────────────────────────────────


def test_each_org_gets_its_own_pat():
    servers = {"ado-newoceanis": _server("newoceanis"), "ado-industrial": _server("Industrial-nois")}
    out = with_pat_auth(servers, _two_orgs())
    assert _decoded(out["ado-newoceanis"]) == ":ROOT-PAT"
    assert _decoded(out["ado-industrial"]) == ":OTHER-PAT"
    for server in out.values():
        assert server["args"][-2:] == ["--authentication", "pat"]


def test_latest_is_pinned_and_explicit_version_kept():
    out = with_pat_auth({"a": _server("newoceanis"), "b": _server("newoceanis", version="2.9.0")},
                        _settings())
    assert "@azure-devops/mcp@2.11.0" in out["a"]["args"]
    assert "@azure-devops/mcp@2.9.0" in out["b"]["args"]


def test_blank_pin_keeps_the_spec():
    out = with_pat_auth({"a": _server("newoceanis")}, _settings(ado_mcp_version=""))
    assert "@azure-devops/mcp@latest" in out["a"]["args"]


def test_explicit_interactive_flag_is_replaced_not_duplicated():
    out = with_pat_auth({"a": _server("newoceanis", "-a", "interactive")}, _settings())
    args = out["a"]["args"]
    assert "interactive" not in args and args.count("--authentication") == 1


def test_authors_explicit_choice_is_respected():
    server = _server("newoceanis", "-a", "azcli")
    assert with_pat_auth({"a": server}, _settings())["a"] is server


def test_no_pat_for_org_leaves_server_unchanged():
    server = _server("someone-else")
    assert with_pat_auth({"a": server}, _settings())["a"] is server


def test_switch_off_disables_everything():
    servers = {"a": _server("newoceanis")}
    assert with_pat_auth(servers, _settings(ado_mcp_pat_auth=False)) is servers


def test_input_is_never_mutated():
    servers = {"a": _server("newoceanis")}
    snapshot = json.dumps(servers)
    with_pat_auth(servers, _settings())
    assert json.dumps(servers) == snapshot


def test_existing_env_is_kept():
    server = _server("newoceanis")
    server["env"] = {"KEEP": "1"}
    out = with_pat_auth({"a": server}, _settings())
    assert out["a"]["env"]["KEEP"] == "1" and PAT_ENV in out["a"]["env"]


def test_workspace_pat_without_org_shares_machine_org_but_scoped_wins():
    cfg = _settings(workspaces=[WorkspaceConfig(name="W", ado_projects=["P"], ado_pat="WS-PAT",
                                                workspace_directory="/tmp/w")])
    assert pats_by_organization(cfg) == {"newoceanis": "ROOT-PAT"}
    scoped = cfg.scoped_for_project("P")
    assert pats_by_organization(scoped)["newoceanis"] == "WS-PAT"


# ── audit + doctor ───────────────────────────────────────────────────────────


def test_audit_modes():
    servers = {"p": _server("newoceanis"), "o": _server("newoceanis", "-a", "azcli"),
               "x": _server("unknown-org"), "pw": {"command": "npx", "args": ["@playwright/mcp"]}}
    modes = {s.server.name: s.mode for s in audit(servers, _settings())}
    assert modes == {"p": "pat", "o": "own", "x": "popup"}


def test_doctor_warns_on_popup_and_never_prints_the_pat(tmp_path):
    from ai_autopilot.doctor import WARN, check_ado_mcp_auth

    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "ado-newoceanis": _server("newoceanis"), "ado-industrial": _server("Industrial-nois"),
    }}), encoding="utf-8")
    findings = check_ado_mcp_auth(_settings(workspace_directory=str(tmp_path)))
    assert [f.level for f in findings] == [WARN]
    assert "ado-industrial" in findings[0].title
    assert "ROOT-PAT" not in repr(findings)


def test_doctor_ok_when_every_server_has_a_pat(tmp_path):
    from ai_autopilot.doctor import OK, check_ado_mcp_auth

    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "ado-newoceanis": _server("newoceanis")}}), encoding="utf-8")
    findings = check_ado_mcp_auth(_settings(workspace_directory=str(tmp_path)))
    assert [f.level for f in findings] == [OK]


def test_agent_loader_injects_pat(tmp_path):
    from ai_autopilot.execution.claude_executor import load_agent_mcp_servers

    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "ado-newoceanis": _server("newoceanis")}}), encoding="utf-8")
    out = load_agent_mcp_servers(str(tmp_path), _settings())
    assert _decoded(out["ado-newoceanis"]) == ":ROOT-PAT"
    # The file on disk is untouched — it is shared through git.
    assert PAT_ENV not in (tmp_path / ".mcp.json").read_text(encoding="utf-8")


def test_workspaces_page_status_per_workspace(tmp_path):
    from ai_autopilot import workspaces as workspaces_mod
    from ai_autopilot.dashboard.routes.workspaces import _ado_mcp_auth_by_workspace

    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "ado-newoceanis": _server("newoceanis"), "ado-industrial": _server("Industrial-nois"),
    }}), encoding="utf-8")
    cfg = _settings(workspace_directory=str(tmp_path))
    views = workspaces_mod.resolve(cfg)
    status = _ado_mcp_auth_by_workspace(cfg, views)
    modes = {m["name"]: m["mode"] for m in status[views[0].id]}
    assert modes == {"ado-newoceanis": "pat", "ado-industrial": "popup"}
    assert "ROOT-PAT" not in json.dumps(status)
