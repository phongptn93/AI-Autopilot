"""Hardening: the dashboard XSS paths, git that cannot hang, SQLite under contention,
and an ADO Retry-After the server cannot stretch into an outage."""

from __future__ import annotations

import asyncio
import sys

import httpx
from sqlalchemy import text
from starlette.testclient import TestClient

from ai_autopilot.ado.client import _MAX_RETRY_AFTER_SECONDS, AdoClient
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.data.database import Database
from ai_autopilot.execution import claude_executor
from ai_autopilot.execution.claude_executor import ClaudeExecutor, GitError


def test_preview_is_sandboxed_even_when_opened_outside_the_iframe(tmp_path):
    """The iframe sandbox only holds inside the iframe. Opened directly, agent-written
    HTML would run with the dashboard session — so the CSP itself must sandbox it and
    deny it the network."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "mock.html").write_text("<script>fetch('/dashboard/settings')</script>", "utf-8")
    settings = Settings(
        dry_run=True, workspace_directory=str(ws),
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}",
    )
    with TestClient(create_app(settings)) as client:
        csp = client.get("/dashboard/task/1/preview", params={"path": "mock.html"}).headers[
            "content-security-policy"
        ]
    directives = [d.strip() for d in csp.split(";")]
    assert "sandbox" in directives                 # no allow-scripts, no allow-same-origin
    assert "connect-src 'none'" in directives
    assert "form-action 'none'" in directives


def test_ado_comment_html_is_rendered_as_text():
    """A comment is written by anyone who can comment on the item; its HTML must not
    reach the page as markup."""
    from ai_autopilot.dashboard import _TEMPLATES

    tpl = _TEMPLATES.env.get_template("task_room.html")
    src = tpl.environment.loader.get_source(tpl.environment, "task_room.html")[0]
    assert "cmt.get('text', '') | safe" not in src
    assert "cmt.get('text', '') | striptags" in src


async def test_git_that_hangs_is_killed_and_reported(tmp_path, monkeypatch):
    """A stalled fetch or a credential prompt used to hold the repo lock and a
    concurrency slot forever."""
    ex = ClaudeExecutor(Settings(), None)
    seen_env = {}
    real_exec = asyncio.create_subprocess_exec

    async def slow_exec(*argv, **kw):
        seen_env.update(kw.get("env") or {})
        # Stand in for git with something that never finishes on its own.
        return await real_exec(
            sys.executable, "-c", "import time; time.sleep(60)", **{**kw, "env": None}
        )

    monkeypatch.setattr(claude_executor.asyncio, "create_subprocess_exec", slow_exec)

    assert await ex._git(["fetch"], str(tmp_path), check=False, timeout_seconds=0.5) == ""
    try:
        await ex._git(["fetch"], str(tmp_path), check=True, timeout_seconds=0.5)
    except GitError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected GitError")
    assert seen_env.get("GIT_TERMINAL_PROMPT") == "0"


async def test_sqlite_runs_in_wal_with_a_busy_timeout(tmp_path):
    """Six services write one file; without WAL + busy_timeout a contended write fails
    at once with "database is locked"."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    await db.create_all()
    async with db._engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        busy = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    await db._engine.dispose()
    assert str(mode).lower() == "wal"
    assert int(busy) >= 5000


async def test_retry_after_is_capped(monkeypatch):
    """Retry-After is the server's to set; an hour-long value must not stall a poll."""
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr("ai_autopilot.ado.client.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"})
        return httpx.Response(200, json={})

    cfg = Settings(ado_organization="https://dev.azure.com/org")
    client = AdoClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), auth=None, config=cfg
    )
    resp = await client._send("GET", "https://dev.azure.com/o/_apis/wit/x", retries=2)
    assert resp.status_code == 200
    assert slept == [_MAX_RETRY_AFTER_SECONDS]


# ── A: no password → localhost only ─────────────────────────────────────────
def _no_auth_app(tmp_path, **over):
    return create_app(Settings(
        dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", **over,
    ))


def test_dashboard_without_password_refuses_remote_clients(tmp_path):
    """No password + reachable from the network = anyone rewrites the PAT. So until a
    password is set the dashboard answers this machine only — probes stay open."""
    app = _no_auth_app(tmp_path, dashboard_allow_remote_without_auth=False)
    with TestClient(app, client=("10.0.0.7", 5555)) as remote:
        assert remote.get("/dashboard").status_code == 403
        assert remote.get("/health").status_code != 403
    with TestClient(app, client=("127.0.0.1", 5555)) as local:
        assert local.get("/dashboard").status_code == 200


def test_remote_dashboard_without_password_is_an_explicit_choice(tmp_path):
    app = _no_auth_app(tmp_path, dashboard_allow_remote_without_auth=True)
    with TestClient(app, client=("10.0.0.7", 5555)) as remote:
        assert remote.get("/dashboard").status_code == 200


def test_a_password_replaces_the_localhost_gate(tmp_path):
    """With a password the normal login applies to everyone — no 403 by address."""
    app = _no_auth_app(tmp_path, dashboard_allow_remote_without_auth=False,
                       dashboard_auth_token="pw")
    with TestClient(app, client=("10.0.0.7", 5555)) as remote:
        resp = remote.get("/dashboard", auth=("", "pw"))
        assert resp.status_code == 200


# ── E: webhook secret in the header only ────────────────────────────────────
def test_webhook_secret_is_not_accepted_from_the_query_string(tmp_path):
    """A query string lands in every proxy and access log on the way."""
    app = _no_auth_app(tmp_path, webhook_secret="s3cret")
    with TestClient(app) as client:
        assert client.post("/api/webhook/x", params={"secret": "s3cret"}).status_code == 401
        resp = client.post("/api/webhook/x", headers={"X-Webhook-Secret": "s3cret"})
        assert resp.status_code != 401


# ── B: bypassPermissions is opt-in ──────────────────────────────────────────
def _launched_args(monkeypatch, **over) -> list[str]:
    seen: list[str] = []
    # A launch pre-trusts its folder in ~/.claude.json — keep that off the real one.
    monkeypatch.setattr(claude_executor, "pretrust_claude_dir", lambda _p: True)

    class _P:
        pid = 1

    def fake_popen(argv, **_kw):
        seen.extend(argv)
        return _P()

    monkeypatch.setattr(claude_executor.subprocess, "Popen", fake_popen)
    ex = ClaudeExecutor(Settings(**over), None)
    assert ex._launch_console(".", "s", "do it") == 1
    return seen


def test_interactive_sessions_do_not_bypass_permissions_by_default(monkeypatch):
    """The brief comes from work-item text; skipping every prompt by default meant a
    prompt injection in a ticket ran commands on the operator's machine unasked."""
    args = _launched_args(monkeypatch)
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"


def test_interactive_bypass_is_honoured_when_the_operator_opts_in(monkeypatch):
    args = _launched_args(monkeypatch, interactive_bypass_permissions=True)
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"


# ── D: a suite that cannot start ────────────────────────────────────────────
async def test_unrunnable_suite_passes_by_default_and_blocks_on_request(tmp_path, monkeypatch):
    from ai_autopilot.execution.test_gate import TestGate

    (tmp_path / "App.csproj").write_text("<Project/>", encoding="utf-8")
    monkeypatch.setattr("ai_autopilot.execution.test_gate.shutil.which", lambda _b: None)

    lenient = await TestGate(Settings(test_gate_enabled=True)).run(str(tmp_path))
    assert lenient.passed and not lenient.ran

    strict = await TestGate(
        Settings(test_gate_enabled=True, test_gate_block_when_not_run=True)
    ).run(str(tmp_path))
    assert not strict.passed and not strict.ran


async def test_no_runner_at_all_never_blocks(tmp_path):
    """A repo with no tests (docs, infra) is not "a runner that could not start"."""
    from ai_autopilot.execution.test_gate import TestGate

    r = await TestGate(
        Settings(test_gate_enabled=True, test_gate_block_when_not_run=True)
    ).run(str(tmp_path))
    assert r.passed and not r.ran
