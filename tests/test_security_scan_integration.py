"""Security scan — where it plugs into the rest of the autopilot: the pre-PR gate, the
scan loop, the ADO bug sync and the Security page."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import ScheduledLoop, Settings
from ai_autopilot.data import Database, SecurityRepository
from ai_autopilot.execution.auto_reviewer import AutoReviewer
from ai_autopilot.reports import Finding
from ai_autopilot.security_scan import ado_sync
from ai_autopilot.security_scan import fingerprint as fp_mod
from ai_autopilot.security_scan import suppressions as sup
from ai_autopilot.services.loop_scheduler import LoopScheduler

_KEY = "token = '" + "AKIA" + "IOSFODNN7QZXWVTR" + "'\n"   # built at runtime: no literal key


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "ws" / "app"
    (r / "src").mkdir(parents=True)
    (r / "src" / "leak.py").write_text(_KEY, encoding="utf-8")
    return r


# ── pre-PR gate ─────────────────────────────────────────────────────────────


async def test_reviewer_blocks_on_scanner_finding_even_when_model_says_clean(repo, monkeypatch):
    cfg = Settings(auto_review_enabled=True, block_on_severity="Critical,High")
    reviewer = AutoReviewer(cfg)

    async def _clean(_prompt, _dir, **_kw):
        return SimpleNamespace(text="- [None] no issues found")

    monkeypatch.setattr("ai_autopilot.execution.auto_reviewer.run_claude", _clean)
    # The diff cannot be computed (no git): the gate falls back to the explicit file
    # list an empty diff yields → nothing to scan. Give it the files directly.
    monkeypatch.setattr(
        "ai_autopilot.security_scan.runner.changed_files",
        lambda _repo, _base: _coro(["src/leak.py"]),
    )
    result = await reviewer.review(str(repo), "main")
    assert not result.passed
    assert any("AWS access key" in line and "[Critical]" in line for line in result.critical_issues)
    assert result.scanner_findings and result.scanner_status["builtin"].startswith("1 finding")


async def test_reviewer_seeds_the_model_with_scanner_findings(repo, monkeypatch):
    cfg = Settings(auto_review_enabled=True)
    reviewer = AutoReviewer(cfg)
    seen: dict = {}

    async def _capture(prompt, _dir, **_kw):
        seen["prompt"] = prompt
        return SimpleNamespace(text="- [None] no issues found")

    monkeypatch.setattr("ai_autopilot.execution.auto_reviewer.run_claude", _capture)
    monkeypatch.setattr(
        "ai_autopilot.security_scan.runner.changed_files",
        lambda _repo, _base: _coro(["src/leak.py"]),
    )
    await reviewer.review(str(repo), "main")
    assert "Already found by static scanners" in seen["prompt"]
    assert "src/leak.py" in seen["prompt"]


async def test_reviewer_gate_failure_does_not_block(repo, monkeypatch):
    cfg = Settings(auto_review_enabled=True)
    reviewer = AutoReviewer(cfg)

    async def _clean(_prompt, _dir, **_kw):
        return SimpleNamespace(text="- [None] no issues found")

    async def _boom(*_a, **_k):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr("ai_autopilot.execution.auto_reviewer.run_claude", _clean)
    monkeypatch.setattr("ai_autopilot.security_scan.runner.run_scan", _boom)
    result = await reviewer.review(str(repo), "main")
    assert result.passed and "error" in result.scanner_status.get("gate", "")


async def _coro(value):
    return value


# ── scan loop ───────────────────────────────────────────────────────────────


class _Repo:
    def __init__(self):
        self.started: list = []
        self.completed: list = []

    async def start_execution(self, item, prompt, profile=""):
        self.started.append((item.title, prompt))
        return 7

    async def complete_execution(self, record_id, result):
        self.completed.append((record_id, result))


class _Tracker:
    async def track(self, *_a):
        pass


class _Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, msg):
        self.sent.append(msg)


class _Ado:
    def __init__(self):
        self.created: list[dict] = []
        self.comments: list = []

    async def create_work_item(self, **kw):
        self.created.append(kw)
        return 900 + len(self.created)

    async def add_comment(self, wid, text):
        self.comments.append((wid, text))
        return True


async def test_scan_loop_runs_scanner_stores_and_files_bugs(repo, tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    await db.create_all()
    cfg = Settings(
        workspace_directory=str(repo.parent), repo_working_directory=str(repo),
        autonomy_level="assisted",
        scheduled_loops=[ScheduledLoop(
            name="secrets", mode="scan", cron="3 5 * * *", prompt="",
            scan_tools=["builtin"], scan_ai_mode="off",
        )],
    )
    cfg.security_scan.file_bugs = True
    ado = _Ado()
    c = SimpleNamespace(
        config=cfg, execution_repo=_Repo(), cost_tracker=_Tracker(), notifier=_Notifier(),
        executor=None, security_repo=SecurityRepository(db),
        loop_report_repo=SimpleNamespace(save=_save), ado_for=lambda _p: ado,
    )
    try:
        sched = LoopScheduler(c)
        assert await sched.run_now("secrets")
        assert c.execution_repo.started[0][0] == "[scan] secrets"
        _, result = c.execution_repo.completed[0]
        assert not result.success and "1 new" in (result.error or result.output)
        rows = await c.security_repo.list_findings(repo=str(repo.resolve()))
        assert len(rows) == 1 and rows[0].ado_bug_id == 901
        assert ado.created[0]["item_type"] == "Bug"
        assert "[Security][critical][CWE-798]" in ado.created[0]["title"]
        assert c.notifier.sent

        # Second run: nothing new → no new Bug, gate passes.
        assert await sched.run_now("secrets")
        _, result2 = c.execution_repo.completed[1]
        assert result2.success and len(ado.created) == 1

        # Fixed → the Bug is told.
        (repo / "src" / "leak.py").unlink()
        await sched.run_now("secrets")
        assert ado.comments and ado.comments[0][0] == 901
    finally:
        await db.dispose()


async def _save(report, html_path="", error=""):
    return 1


async def test_ado_sync_respects_report_autonomy(tmp_path):
    cfg = Settings(autonomy_level="report")
    cfg.security_scan.file_bugs = True
    out = await ado_sync.file_new_findings(
        ado=_Ado(), security_repo=None, config=cfg, repo="r", project="", scan_id=1,
        fingerprints=["x"],
    )
    assert out.skipped == "autonomy_level is report" and not out.filed


def test_scan_loop_preset_has_scan_fields():
    from ai_autopilot.dashboard import loop_presets

    loop = ScheduledLoop(**loop_presets.by_key("security-audit-weekly"))
    assert loop.is_scan and loop.is_report and loop.scan_ai_mode == "deep"
    assert "semgrep" in loop.scan_tools


# ── Security page ───────────────────────────────────────────────────────────


def _client(tmp_path, **overrides):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", **overrides)
    return TestClient(create_app(settings))


def test_security_page_lists_findings_and_suppresses_to_file(tmp_path, repo):
    with _client(tmp_path, workspace_directory=str(repo.parent)) as client:
        c = client.app.state.container
        f = Finding(severity="high", title="SQL concat", file="src/a.cs", line=3, tool="builtin",
                    rule_id="cs-sql-concat", cwe="CWE-89", snippet="x")
        fp_mod.stamp([f])
        client.portal.call(c.security_repo.upsert_scan, str(repo), "", 0, [f])

        page = client.get("/dashboard/security").text
        assert "SQL concat" in page and "CWE-89" in page and "cs-sql-concat" in page
        assert 'href="/dashboard/security"' in page  # nav

        fid = re.search(r"/dashboard/security/(\d+)/suppress", page).group(1)
        r = client.post(f"/dashboard/security/{fid}/suppress",
                        data={"reason": "legacy module, tracked in #123", "until": "2099-01-01"},
                        follow_redirects=False)
        assert r.status_code in (302, 303)
        saved = sup.load(repo.parent)
        assert saved.reason_for(f.fingerprint) == "legacy module, tracked in #123"
        row = client.portal.call(c.security_repo.get, int(fid))
        assert row.status == "suppressed"

        # No reason → refused.
        r = client.post(f"/dashboard/security/{fid}/reopen", follow_redirects=False)
        assert r.status_code in (302, 303)
        assert sup.load(repo.parent).entries == []
        page = client.get("/dashboard/security?status=open").text
        assert "SQL concat" in page


def test_loops_page_saves_scan_mode(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    with _client(tmp_path, workspace_directory=str(tmp_path)) as client:
        r = client.post("/dashboard/loops", data={
            "loop_0_name": "secret-scan-daily", "loop_0_prompt": "", "loop_0_cron": "3 5 * * *",
            "loop_0_interval": "", "loop_0_mode": "scan", "loop_0_scan_tools": "builtin, gitleaks",
            "loop_0_scan_ai": "off", "loop_0_scan_scope": "full", "loop_0_project": "",
            "loop_0_repo": "/srv/repo", "loop_0_base": "main", "loop_0_enabled": "on",
            "loop_0_html": "on",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
        loop = client.app.state.container.config.scheduled_loops[0]
        assert loop.is_scan and loop.scan_tools == ["builtin", "gitleaks"]
        assert loop.scan_ai_mode == "off"
        page = client.get("/dashboard/loops").text
        assert 'value="scan" selected' in page
