"""Compliance evidence pack: one period's changes, tests, scans, accepted risk, audit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

from ai_autopilot import evidence_pack as ep
from ai_autopilot.flaky_tests import FlakyTest

END = datetime(2026, 11, 22, tzinfo=UTC)
START = END - timedelta(days=30)
IN = END - timedelta(days=3)
OUT = END - timedelta(days=60)


def _run(wid, when, pr="", total=0, failed=0, title="t"):
    return NS(work_item_id=wid, title=title, pr_url=pr, completed_at=when, started_at=when,
              status=NS(value="success"), tests_total=total, tests_failed=failed,
              tests_blocked=0)


def _scan(when, gate=True):
    return NS(started_at=when, repo="C:/ws/api", scope="full",
              tools_json=json.dumps({"gitleaks": "ok", "semgrep": "ok"}), critical_count=0,
              high_count=1, medium_count=2, gate_passed=gate, fail_on="high")


def _finding(sev, *, reason="", by="", until=None, title="SQLi"):
    return NS(severity=sev, title=title, rule_id="r", file="a.py", suppress_reason=reason,
              suppressed_by=by, suppress_until=until)


def _input(**kw):
    base = dict(start=START, end=END, version="2.96.0")
    base.update(kw)
    return ep.EvidenceInput(**base)


def test_only_the_period_is_reported_and_each_section_is_present():
    md = ep.render(_input(
        runs=[_run(1, IN, pr="https://x/pr/1", total=4, failed=1, title="Phiếu | lĩnh"),
              _run(2, OUT, pr="https://x/pr/2")],
        scans=[_scan(IN), _scan(IN, gate=False), _scan(OUT)],
        open_findings=[_finding("high"), _finding("high"), _finding("low")],
        audit=[NS(at=IN, actor="an", action="config.updated", target="trigger_tag", detail=""),
               NS(at=OUT, actor="x", action="config.updated", target="old", detail="")],
        flaky=[FlakyTest("api", "test a", 3, OUT, IN)],
    ))
    for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6."):
        assert heading in md
    assert "#1 | Phiếu \\| lĩnh" in md and "#2 " not in md        # pipe escaped, old run out
    assert "3 / 4" in md
    assert "cổng chặn đạt 1 / 2" in md
    assert "high 2, low 1" in md
    assert "trigger_tag" in md and "| old |" not in md
    assert "test a" in md


def test_empty_controls_are_said_in_words():
    md = ep.render(_input())
    assert "Không có lần quét bảo mật nào trong kỳ" in md
    assert "Không có PR nào được mở trong kỳ" in md
    assert "Chưa có SBOM" in md


def test_a_suppression_without_a_reason_is_called_out():
    md = ep.render(_input(suppressed=[_finding("medium", by="lead", until=END),
                                      _finding("high", reason="false positive", by="sec")]))
    assert "**(không ghi lý do)**" in md and "false positive" in md


def test_sbom_files_are_listed_from_the_workspace(tmp_path):
    d = tmp_path / ".autopilot" / "sbom"
    d.mkdir(parents=True)
    (d / "api.cdx.json").write_text("{}", encoding="utf-8")
    files = ep.sbom_files(str(tmp_path))
    assert [p.name for p in files] == ["api.cdx.json"]
    assert "api.cdx.json" in ep.render(_input(sbom_files=files))
    assert ep.sbom_files("") == []


def test_evidence_page_and_download(tmp_path):
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app
    from ai_autopilot.config import Settings

    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'ev.db'}")
    with TestClient(create_app(cfg)) as client:
        page = client.get("/dashboard/evidence?days=7")
        dl = client.get("/dashboard/evidence/download?days=7")
    assert page.status_code == 200 and "Quét bảo mật" in page.text
    assert dl.status_code == 200 and dl.headers["content-type"].startswith("text/markdown")
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.text.startswith("# Bộ bằng chứng tuân thủ")
