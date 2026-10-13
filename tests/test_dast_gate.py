"""The DAST HTTP gate: every request the model sends passes these checks in code."""

from __future__ import annotations

import json

import httpx
import pytest

from ai_autopilot.config import DastTarget, Settings
from ai_autopilot.security_scan import dast

ENV = {"TOK_A": "secret-a", "TOK_B": "secret-b"}


def _target(**kw) -> DastTarget:
    base = dict(name="staging", base_url="https://staging.internal", owner_confirmed=True,
                allowed_hosts=["staging.internal"], require_private=False,
                auth_env_var="TOK_A", auth_b_env_var="TOK_B", rate_limit_rps=0)
    base.update(kw)
    return DastTarget(**base)


class _Server:
    def __init__(self, response: httpx.Response | None = None):
        self.seen: list[httpx.Request] = []
        self.response = response or httpx.Response(200, text="ok")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        return self.response


async def _send(args, target=None, server=None, **budget_kw):
    target = target or _target()
    server = server or _Server()
    budget = dast.ProbeBudget(budget_kw.get("max_requests", target.max_requests),
                              target.allow_mutations, list(target.allowed_paths))
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
        out = await dast.probe_request(args, target=target, budget=budget,
                                       limiter=dast.RateLimiter(0), http=http, env=ENV)
    return out, server, budget


def _payload(out) -> str:
    return out["content"][0]["text"]


async def test_a_get_reaches_the_target_with_identity_a_token():
    out, server, budget = await _send({"method": "GET", "path": "/api/orders?x=1"})
    [req] = server.seen
    assert str(req.url) == "https://staging.internal/api/orders?x=1"
    assert req.headers["Authorization"] == "Bearer secret-a"
    assert json.loads(_payload(out))["status"] == 200 and budget.sent == 1


async def test_identity_b_and_none():
    _, server, _ = await _send({"method": "GET", "path": "/x", "identity": "b"})
    assert server.seen[0].headers["Authorization"] == "Bearer secret-b"
    _, server, _ = await _send({"method": "GET", "path": "/x", "identity": "none"})
    assert "Authorization" not in server.seen[0].headers


@pytest.mark.parametrize("path", [
    "https://evil.example/x", "//evil.example/x", "api/orders", "/api/../admin",
    "/api/%2e%2e/admin", "/a//b",
])
async def test_paths_that_could_leave_the_target_are_refused(path):
    out, server, budget = await _send({"method": "GET", "path": path})
    assert out.get("is_error") and _payload(out).startswith("REFUSED")
    assert server.seen == [] and budget.sent == 0


async def test_mutations_are_refused_unless_allowed():
    out, server, _ = await _send({"method": "DELETE", "path": "/api/orders/1"})
    assert out.get("is_error") and "allow_mutations" in _payload(out) and server.seen == []
    out, server, _ = await _send({"method": "DELETE", "path": "/api/orders/1"},
                                 target=_target(allow_mutations=True))
    assert not out.get("is_error") and len(server.seen) == 1


async def test_allowed_paths_and_the_request_cap_are_enforced():
    out, server, _ = await _send({"method": "GET", "path": "/admin"},
                                 target=_target(allowed_paths=["/api"]))
    assert "outside allowed_paths" in _payload(out) and server.seen == []

    target, srv = _target(max_requests=2), _Server()
    budget = dast.ProbeBudget(2, False, [])
    async with httpx.AsyncClient(transport=httpx.MockTransport(srv)) as http:
        outs = [await dast.probe_request({"method": "GET", "path": "/x"}, target=target,
                                         budget=budget, limiter=dast.RateLimiter(0),
                                         http=http, env=ENV) for _ in range(3)]
    assert [o.get("is_error", False) for o in outs] == [False, False, True]
    assert len(srv.seen) == 2


async def test_redirects_are_returned_not_followed_and_tokens_redacted():
    server = _Server(httpx.Response(302, headers={"Location": "https://evil.example/?t=secret-a"},
                                    text="echo secret-a secret-b"))
    out, server, _ = await _send({"method": "GET", "path": "/login"}, server=server)
    data = json.loads(_payload(out))
    assert len(server.seen) == 1 and data["status"] == 302
    assert "secret" not in json.dumps(data)


async def test_run_dast_hands_the_executor_only_the_gate():
    cfg = Settings()
    cfg.security_scan.dast_enabled = True
    cfg.security_scan.dast_targets = [_target()]
    seen: dict = {}

    class _Exec:
        async def run_audit(self, name, prompt, repo, base, project, **kw):
            seen.update(kw, prompt=prompt)
            from ai_autopilot.models import ExecutionResult
            return ExecutionResult.ok(0, prompt, "no findings")

    await dast.run_dast(config=cfg, target_name="staging", executor=_Exec())

    assert list(seen["mcp_servers"]) == ["dast"]
    assert {"Bash", "WebFetch", "WebSearch"} <= set(seen["extra_disallowed"])
    assert "mcp__dast__http_request" in seen["prompt"]
    assert "TOK_A" not in seen["prompt"]  # the model is never told where the token lives


async def test_a_supplied_tool_surface_replaces_and_denies_the_workspace_mcp(tmp_path, monkeypatch):
    from ai_autopilot.execution import claude_executor as ce

    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"playwright": {"command": "x"}}}))
    captured: dict = {}

    async def fake_run_claude(prompt, work_dir, **kw):
        captured.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(ce, "run_claude", fake_run_claude)
    ex = ce.ClaudeExecutor.__new__(ce.ClaudeExecutor)
    ex._root_config = Settings(workspace_directory=str(tmp_path))

    with pytest.raises(RuntimeError):
        await ex._run_claude("p", str(tmp_path), mcp_servers={"dast": {"type": "sdk"}},
                             disallowed_tools=["Bash"])
    assert captured["mcp_servers"] == {"dast": {"type": "sdk"}}
    assert "mcp__playwright" in captured["disallowed_tools"] and "Bash" in captured["disallowed_tools"]


async def test_user_scope_mcp_servers_are_denied_too(tmp_path, monkeypatch):
    """A browser MCP installed user-wide can open any host — it must not survive."""
    from ai_autopilot.execution import claude_executor as ce

    home = tmp_path / "home"
    home.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"browser": {}},
        "projects": {str(ws): {"mcpServers": {"fetcher": {}}}, "/elsewhere": {"mcpServers": {"x": {}}}},
    }))
    monkeypatch.setattr(ce.Path, "home", classmethod(lambda cls: home))
    captured: dict = {}

    async def fake_run_claude(prompt, work_dir, **kw):
        captured.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(ce, "run_claude", fake_run_claude)
    ex = ce.ClaudeExecutor.__new__(ce.ClaudeExecutor)
    ex._root_config = Settings(workspace_directory=str(ws))
    with pytest.raises(RuntimeError):
        await ex._run_claude("p", str(ws), mcp_servers={"dast": {}})
    denied = captured["disallowed_tools"]
    assert "mcp__browser" in denied and "mcp__fetcher" in denied
    assert "mcp__x" not in denied and "mcp__dast" not in denied
