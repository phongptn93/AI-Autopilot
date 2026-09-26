"""Security scan — the parts added to make it USABLE: rule/path ignores, live progress,
and the Security pages (finding detail, scan detail, export, settings, progress)."""

from __future__ import annotations

import json
import re
from functools import partial
from pathlib import Path

import yaml
from starlette.testclient import TestClient

from ai_autopilot import activity
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.reports import Finding
from ai_autopilot.security_scan import fingerprint as fp_mod
from ai_autopilot.security_scan import progress
from ai_autopilot.security_scan.runner import ScanRequest, apply_ignores, run_scan

_KEY = "token = '" + "AKIA" + "IOSFODNN7QZXWVTR" + "'\n"   # built at runtime: no literal key


# ── ignores ─────────────────────────────────────────────────────────────────

def _f(rule, file):
    return Finding(severity="low", title="t", file=file, rule_id=rule, tool="builtin")


def test_apply_ignores_by_rule_and_path():
    rows = [
        _f("ts-target-blank", "src/a.html"),
        _f("cs-sql-concat", "src/Migrations/2024_init.cs"),
        _f("cs-sql-concat", "src/Foo.Designer.cs"),
        _f("py-eval-exec", "vendor/x/y.py"),
        _f("cs-sql-concat", "src/Orders.cs"),
    ]
    kept, dropped = apply_ignores(
        rows, ["ts-target-blank"],
        ["**/Migrations/**", "**/*.Designer.cs", "vendor/**"],
    )
    assert dropped == 4 and [f.file for f in kept] == ["src/Orders.cs"]


def test_apply_ignores_noop_when_empty():
    rows = [_f("x", "a")]
    assert apply_ignores(rows, [], []) == (rows, 0)


async def test_runner_applies_ignores_and_streams_progress(tmp_path):
    repo = tmp_path / "ws" / "app"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "leak.py").write_text(_KEY, encoding="utf-8")
    (repo / "src" / "a.html").write_text('<a href="x" target="_blank">', encoding="utf-8")
    lines: list[str] = []
    result = await run_scan(ScanRequest(
        repo=str(repo), workspace=str(repo.parent), tools=["builtin"], ai_mode="off",
        store=False, write_html=False, disabled_rules=["ts-target-blank"],
        on_progress=lines.append,
    ))
    assert result.filtered == 1
    assert {f.rule_id for f in result.findings} == {"secret-aws-access-key"}
    joined = "\n".join(lines)
    assert "security scan started" in joined and "builtin:" in joined and "gate FAILED" in joined
    # A store=False run streams to the caller only — nothing is written to the feed
    # (that is for stored scans the dashboard watches) …
    assert activity.read(str(repo.parent), activity.security_key("app")) == ""
    # … and the progress registry is clean afterwards.
    assert progress.get(str(repo.resolve())) is None


def test_progress_label():
    p = progress.start("/r", "cli")
    p.stage, p.tools = "ai", {"builtin": "3 finding(s) in 0.1s"}
    assert "AI review running" in p.label() and "builtin 3 finding" in p.label()
    progress.finish("/r")
    assert progress.get("/r") is None


# ── pages ───────────────────────────────────────────────────────────────────

def _client(tmp_path, **overrides):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", **overrides)
    return TestClient(create_app(settings))


def _seed(client, repo: Path):
    c = client.app.state.container
    f = Finding(severity="high", title="SQL concat in OrderService", file="src/OrderService.cs",
                line=88, tool="builtin", rule_id="cs-sql-concat", cwe="CWE-89", owasp="API8:2023",
                detail="raw SQL built with +", snippet='FromSqlRaw("SELECT " + code)')
    fp_mod.stamp([f])
    call = client.portal.call
    scan_id = call(partial(c.security_repo.start_scan, repo=str(repo), trigger="cli",
                           scope="full", ai_mode="off", fail_on="high"))
    call(c.security_repo.upsert_scan, str(repo), "", scan_id, [f])
    call(partial(
        c.security_repo.finish_scan, scan_id, status="success", high_count=1, new_count=1,
        gate_passed=False, new_json=json.dumps([f.fingerprint]),
        tools_json=json.dumps({"builtin": "1 finding(s) in 0.1s",
                               "semgrep": "skipped (not installed)"}),
    ))
    return f, scan_id


def test_security_page_has_readiness_scan_form_and_links(tmp_path):
    repo = tmp_path / "ws" / "app"
    repo.mkdir(parents=True)
    with _client(tmp_path, workspace_directory=str(repo.parent),
                 repo_working_directory=str(repo)) as client:
        page = client.get("/dashboard/security").text
        # Readiness rows and the scan form are there before any scan exists.
        assert "Readiness" in page and "builtin" in page and "always available" in page
        assert 'action="/dashboard/security/rescan"' in page and 'name="tools"' in page
        assert "No scans yet" in page
        f, scan_id = _seed(client, repo)
        page = client.get(f"/dashboard/security?repo={repo}").text
        assert "/dashboard/security/f/" in page
        assert f"/dashboard/security/scans/{scan_id}" in page
        assert "⬇ SARIF" in page
        # Search narrows.
        assert "SQL concat" in client.get(f"/dashboard/security?repo={repo}&q=OrderService").text
        assert "SQL concat" not in client.get(f"/dashboard/security?repo={repo}&q=zzz-nope").text


def test_finding_page_shows_everything_and_scan_page_lists_new(tmp_path):
    repo = tmp_path / "ws" / "app"
    repo.mkdir(parents=True)
    with _client(tmp_path, workspace_directory=str(repo.parent)) as client:
        f, scan_id = _seed(client, repo)
        listing = client.get(f"/dashboard/security?repo={repo}").text
        fid = re.search(r"/dashboard/security/f/(\d+)", listing).group(1)
        page = client.get(f"/dashboard/security/f/{fid}").text
        assert "SQL concat in OrderService" in page
        assert "cwe.mitre.org/data/definitions/89.html" in page       # CWE link
        assert "API8:2023" in page and "FromSqlRaw" in page             # class + snippet
        assert f.fingerprint in page and "Suppress / accept risk" in page
        assert f"/dashboard/security/scans/{scan_id}" in page          # history
        scan = client.get(f"/dashboard/security/scans/{scan_id}").text
        assert "New in this scan" in scan and "SQL concat in OrderService" in scan
        assert "semgrep" in scan and "skipped (not installed)" in scan
        assert "gate failed" in scan


def test_export_sarif_and_json(tmp_path):
    repo = tmp_path / "ws" / "app"
    repo.mkdir(parents=True)
    with _client(tmp_path, workspace_directory=str(repo.parent)) as client:
        _seed(client, repo)
        r = client.get(f"/dashboard/security/export?repo={repo}&fmt=sarif")
        assert r.status_code == 200 and "sarif" in r.headers["content-disposition"]
        doc = r.json()
        assert doc["version"] == "2.1.0"
        assert doc["runs"][0]["results"][0]["ruleId"] == "cs-sql-concat"
        r = client.get(f"/dashboard/security/export?repo={repo}&fmt=json")
        assert r.json()[0]["cwe"] == "CWE-89"


def test_settings_form_saves_yaml_and_applies_live(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    with _client(tmp_path) as client:
        r = client.post("/dashboard/security/settings", data={
            "enabled": "1", "ai_mode": "deep", "fail_on": "critical",
            "tools": ["builtin", "gitleaks"], "pr_gate_tools": ["builtin"],
            "file_bugs": "1", "file_bugs_from": "high", "verify_max_per_scan": "3",
            "disabled_rules": "ts-target-blank, cfg-http-not-https",
            "ignore_paths": "**/Migrations/**\n**/*.min.js",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
        sec = client.app.state.container.config.security_scan
        assert sec.ai_mode == "deep" and sec.fail_on == "critical"
        assert sec.tools == ["builtin", "gitleaks"] and sec.file_bugs is True
        assert sec.disabled_rules == ["ts-target-blank", "cfg-http-not-https"]
        assert sec.ignore_paths == ["**/Migrations/**", "**/*.min.js"]
        saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert saved["security_scan"]["ai_mode"] == "deep"
        assert saved["security_scan"]["verify_max_per_scan"] == 3


def test_progress_endpoint_and_rescan_busy(tmp_path):
    repo = tmp_path / "ws" / "app"
    repo.mkdir(parents=True)
    with _client(tmp_path, workspace_directory=str(repo.parent)) as client:
        assert client.get(f"/dashboard/security/progress?repo={repo}").text == "idle"
        key = str(repo.resolve())
        progress.start(key, "test")
        try:
            assert "starting" in client.get(f"/dashboard/security/progress?repo={repo}").text
            r = client.post("/dashboard/security/rescan", data={"repo": key, "scope": "full"},
                            follow_redirects=False)
            assert r.status_code in (302, 303)
            assert "sec_scan_busy" in r.headers.get("set-cookie", "")
        finally:
            progress.finish(key)


def test_verify_route_refuses_without_worktrees(tmp_path):
    repo = tmp_path / "ws" / "app"
    repo.mkdir(parents=True)
    with _client(tmp_path, workspace_directory=str(repo.parent), use_worktrees=False) as client:
        _seed(client, repo)
        listing = client.get(f"/dashboard/security?repo={repo}").text
        fid = re.search(r"/dashboard/security/f/(\d+)", listing).group(1)
        r = client.post(f"/dashboard/security/{fid}/verify", follow_redirects=False)
        assert "err_sec_no_worktrees" in r.headers.get("set-cookie", "")


async def test_no_store_scan_leaves_no_files_behind(tmp_path):
    """A --no-store scan (CI) must not write an activity feed into the repo's parent."""
    repo = tmp_path / "ws" / "app"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "leak.py").write_text(_KEY, encoding="utf-8")
    await run_scan(ScanRequest(repo=str(repo), tools=["builtin"], ai_mode="off",
                               store=False, write_html=False))
    assert not (tmp_path / "ws" / ".autopilot").exists()
