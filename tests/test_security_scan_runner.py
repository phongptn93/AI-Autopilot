"""Security scan — the pipeline: runner, persistence lifecycle, CLI exit codes.

A fake executor stands in for the model and the builtin scanner stands in for the
external ones, so what is under test is the plumbing: that findings get fingerprints,
that the baseline turns a second run's known findings into "persisting", that a
suppression removes a finding from the gate but not from the record, that a fixed
finding is closed and a returning one reopened, and that the exit code follows.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ai_autopilot.data import Database, SecurityRepository
from ai_autopilot.models import ExecutionResult
from ai_autopilot.reports import Finding
from ai_autopilot.security_scan import ai_sast
from ai_autopilot.security_scan import suppressions as sup
from ai_autopilot.security_scan.cli import render
from ai_autopilot.security_scan.cli import run as cli_run
from ai_autopilot.security_scan.runner import ScanRequest, run_scan

_VULN_PY = "import subprocess\nsubprocess.run(cmd, shell=True)\n"
_KEY_PY = "token = '" + "AKIA" + "IOSFODNN7QZXWVTR" + "'\n"   # built at runtime: no literal key


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    r = ws / "app"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.py").write_text(_VULN_PY, encoding="utf-8")
    (r / "src" / "b.py").write_text(_KEY_PY, encoding="utf-8")
    return r


@pytest.fixture
async def security_repo(tmp_path: Path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'sec.db'}")
    await db.create_all()
    try:
        yield SecurityRepository(db)
    finally:
        await db.dispose()


def _req(repo: Path, **kw) -> ScanRequest:
    base = dict(repo=str(repo), workspace=str(repo.parent), tools=["builtin"],
                ai_mode="off", fail_on="high", write_html=False)
    base.update(kw)
    return ScanRequest(**base)


class _FakeExecutor:
    def __init__(self, text: str, success: bool = True):
        self.text, self.success, self.calls = text, success, []

    async def run_audit(self, name, prompt, repo, base, project=""):
        self.calls.append(prompt)
        make = ExecutionResult.ok if self.success else ExecutionResult.fail
        r = make(0, prompt, self.text)
        r.cost_tokens = 123
        return r


async def test_scan_without_store_finds_and_fails_gate(repo):
    result = await run_scan(_req(repo, store=False))
    assert result.tools["builtin"].ran
    rules = {f.rule_id for f in result.findings}
    assert {"py-shell-true", "secret-aws-access-key"} <= rules
    assert all(f.fingerprint for f in result.findings)
    assert not result.baseline_known and result.diff.new == result.findings
    assert not result.passed and result.gate_findings


async def test_fail_on_threshold(repo):
    result = await run_scan(_req(repo, store=False, fail_on="critical"))
    # shell=True is high; the AWS key is critical → still fails.
    assert not result.passed
    (repo / "src" / "b.py").unlink()
    result = await run_scan(_req(repo, store=False, fail_on="critical"))
    assert result.passed and result.findings  # high remains, but under the bar


async def test_lifecycle_new_persisting_fixed_reopened(repo, security_repo):
    first = await run_scan(_req(repo), security_repo=security_repo)
    assert not first.baseline_known and len(first.diff.new) == 2
    assert first.scan_id > 0

    second = await run_scan(_req(repo), security_repo=security_repo)
    assert second.baseline_known
    assert second.diff.new == [] and len(second.diff.persisting) == 2
    assert second.passed  # nothing NEW → the gate passes even though findings exist

    (repo / "src" / "b.py").unlink()  # the key is removed
    third = await run_scan(_req(repo), security_repo=security_repo)
    assert len(third.diff.fixed) == 1
    rows = await security_repo.list_findings(repo=str(repo.resolve()), status="fixed")
    assert len(rows) == 1 and rows[0].rule_id == "secret-aws-access-key"

    (repo / "src" / "b.py").write_text(_KEY_PY, encoding="utf-8")  # …and comes back
    fourth = await run_scan(_req(repo), security_repo=security_repo)
    # A fixed finding that returns is a regression: NEW for the gate (it fails again),
    # while the table keeps its history — same row, reopened, seen three times.
    assert [f.rule_id for f in fourth.diff.new] == ["secret-aws-access-key"]
    assert not fourth.passed
    reopened = await security_repo.by_fingerprint(str(repo.resolve()), rows[0].fingerprint)
    assert reopened.status == "open" and reopened.times_seen == 3

    scans = await security_repo.recent_scans(repo=str(repo.resolve()))
    assert len(scans) == 4 and scans[0].status == "success"
    assert scans[-1].new_count == 2 and scans[-1].gate_passed is False
    assert scans[2].gate_passed is True and scans[0].gate_passed is False


async def test_diff_scope_does_not_close_findings_elsewhere(repo, security_repo):
    await run_scan(_req(repo), security_repo=security_repo)
    # A diff scan that only looks at a.py must not mark b.py's finding as fixed.
    result = await run_scan(
        _req(repo, scope="diff", files=["src/a.py"]), security_repo=security_repo,
    )
    assert result.diff.fixed == []
    open_rows = await security_repo.list_findings(repo=str(repo.resolve()), status="open")
    assert len(open_rows) == 2


async def test_suppression_removes_from_gate_but_keeps_record(repo, security_repo):
    first = await run_scan(_req(repo, store=False))
    key = next(f for f in first.findings if f.rule_id == "secret-aws-access-key")
    s = sup.Suppressions(path=sup.file_for(repo.parent))
    s.add(key.fingerprint, "fixture key for docs", by="qa", expires=date(2099, 1, 1))
    sup.save(s)

    result = await run_scan(_req(repo, fail_on="critical"), security_repo=security_repo)
    assert [f.fingerprint for f in result.suppressed] == [key.fingerprint]
    assert key.fingerprint not in {f.fingerprint for f in result.findings}
    assert result.passed  # the only critical is suppressed
    assert result.diff.fixed == []  # suppressed ≠ fixed — it is still in the code
    row = await security_repo.by_fingerprint(str(repo.resolve()), key.fingerprint)
    assert row.status == "suppressed"

    # Expired → reported again, and the run says so.
    s.add(key.fingerprint, "fixture key for docs", by="qa", expires=date(2000, 1, 1))
    sup.save(s)
    again = await run_scan(_req(repo, fail_on="critical"), security_repo=security_repo)
    assert again.expired_suppressions == [key.fingerprint]
    assert key.fingerprint in {f.fingerprint for f in again.findings}
    # It was in the baseline (suppressed counts as known), so it is persisting, not new:
    # the gate does not fail on it, but the table shows it open again.
    assert key.fingerprint in {f.fingerprint for f in again.diff.persisting}
    row = await security_repo.by_fingerprint(str(repo.resolve()), key.fingerprint)
    assert row.status == "open"


async def test_ai_pass_merges_and_triages(repo):
    ai_text = (
        "Reviewed.\n```json\n" + json.dumps({
            "summary": "one BOLA",
            "findings": [
                {"severity": "high", "title": "BOLA: order id not checked against caller",
                 "file": "src/orders.py", "line": 9, "cwe": "CWE-639", "owasp": "API1:2023",
                 "rule_id": "bola", "confidence": "high"},
                {"severity": "info", "title": "FALSE POSITIVE: cmd is a constant",
                 "file": "src/a.py", "line": 2, "rule_id": "triage"},
            ],
        }) + "\n```"
    )
    ex = _FakeExecutor(ai_text)
    req = _req(repo, store=False, ai_mode="fast", ai_agents=["agent-security-reviewer"])
    result = await run_scan(req, executor=ex)
    assert result.tools["ai"].ran and result.cost_tokens == 123
    assert "ALREADY FOUND" in ex.calls[0] and "py-shell-true" in ex.calls[0]
    by_rule = {f.rule_id: f for f in result.findings}
    assert by_rule["bola"].tool == "ai" and by_rule["bola"].cwe == "CWE-639"
    assert "triage" not in by_rule
    assert result.ai_demoted == 1
    demoted = by_rule["py-shell-true"]
    assert demoted.severity == "low" and "false positive" in demoted.detail
    assert result.ai_summary == "one BOLA"


async def test_ai_failure_is_recorded_not_raised(repo):
    result = await run_scan(
        _req(repo, store=False, ai_mode="deep"), executor=_FakeExecutor("boom", success=False),
    )
    assert result.tools["ai"].error and not result.tools["ai"].ran
    assert result.findings  # the scanners' half still arrived


def test_split_triage_and_apply():
    own, verdicts = ai_sast.split_triage([
        Finding(title="CONFIRMED: reachable from /api", file="a.cs", line=3, rule_id="triage"),
        Finding(title="real", file="b.cs", line=1, rule_id="x", severity="high"),
    ])
    assert [f.title for f in own] == ["real"] and own[0].tool == "ai"
    target = Finding(title="t", file="a.cs", line=3, severity="high", confidence="medium")
    assert ai_sast.apply_triage([target], verdicts) == 0
    assert target.confidence == "high" and "confirmed" in target.detail


async def test_render_formats(repo):
    result = await run_scan(_req(repo, store=False))
    table = render(result, "table")
    assert "FAIL" in table and "py-shell-true" not in table and "shell=True" in table
    data = json.loads(render(result, "json"))
    assert data["passed"] is False and data["findings"][0]["new"] is True
    doc = json.loads(render(result, "sarif", repo=str(repo)))
    assert doc["runs"][0]["results"]
    assert "<table>" in render(result, "html", repo=str(repo))
    assert "| Sev |" in render(result, "md")


def test_cli_exit_codes(repo, monkeypatch, tmp_path):
    from ai_autopilot import config as config_mod

    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.setattr(config_mod, "_yaml_path", lambda: tmp_path / "none.yaml")
    out = tmp_path / "scan.json"
    code = cli_run(["--repo", str(repo), "--no-ai", "--no-store", "--no-html", "--tools", "builtin",
                    "--format", "json", "--out", str(out), "-q"])
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["counts"]["critical"] == 1
    # Everything below the bar → exit 0.
    code = cli_run(["--repo", str(repo), "--no-ai", "--no-store", "--no-html", "--tools", "builtin",
                    "--fail-on", "critical", "-q"])
    assert code == 1  # the AWS key is critical
    (repo / "src" / "b.py").unlink()
    code = cli_run(["--repo", str(repo), "--no-ai", "--no-store", "--no-html", "--tools", "builtin",
                    "--fail-on", "critical", "-q"])
    assert code == 0
    assert cli_run(["--repo", str(tmp_path / "missing"), "--no-store"]) == 2
    assert cli_run(["--repo", str(repo), "--tools", "nope", "--no-store"]) == 2
