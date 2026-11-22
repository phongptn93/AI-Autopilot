"""Team pulse: one signal per role, worst first, same numbers as the role pages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from ai_autopilot import ci_triage, dora, release_notes
from ai_autopilot import team_pulse as tp

NOW = datetime(2026, 11, 22, 12, tzinfo=UTC)


def test_ba_flags_silent_spec_changes_first():
    s = tp.ba_signal(NS(silent=[1], open=[1, 2]))
    assert s.tone == tp.BAD and "xác nhận lại" in s.action
    assert tp.ba_signal(NS(silent=[], open=[])).tone == tp.GOOD
    assert tp.ba_signal(None).tone == tp.UNKNOWN


def test_pm_reads_the_sprint_verdict():
    behind = NS(status="ok", name="S42", done_pct=30.0, elapsed_pct=70.0, verdict="behind",
                at_risk=[1, 2])
    s = tp.pm_signal(behind)
    assert s.tone == tp.BAD and "chậm hơn lịch" in s.summary and "chưa bắt đầu: 2" in s.summary
    assert tp.pm_signal(NS(status="none", reason="")).tone == tp.WARN
    assert tp.pm_signal(NS(status="unknown", reason="ADO lỗi")).reason == "ADO lỗi"


def _failed(cat, flaky=False):
    f = ci_triage.FailedBuild(1, "2026.1", "api-ci", "main", "abc", "", NOW)
    f.category, f.flaky = cat, flaky
    return f


def test_dev_counts_only_real_code_failures():
    ci = ci_triage.CiReport(days=1, failed=[_failed("build"), _failed("test", flaky=True),
                                            _failed("agent")])
    s = tp.dev_signal(ci, flaky_count=2)
    assert s.facts[0] == ("build đỏ do code:", "1") and s.tone == tp.WARN
    assert "api-ci" in s.action
    assert tp.dev_signal(ci_triage.CiReport(days=1, status="unknown"), None).tone == tp.UNKNOWN


def test_qc_escape_rate_drives_the_tone():
    rep = NS(open_count=4, escape_rate=0.4, reopen_rate=None, oldest_open=[])
    s = tp.qc_signal(rep)
    assert s.tone == tp.BAD and "40%" in s.summary and "mở lại: —" in s.summary


def test_ops_broken_pipeline_and_open_incidents():
    d = dora.DoraReport(days=14, deploys_ok=4, per_week=2.0, change_failure_rate=0.1,
                        broken_since=NOW - timedelta(hours=3))
    s = tp.ops_signal(d, NS(open=[NS(id=55)]))
    assert s.tone == tp.BAD and s.action == "khôi phục pipeline deploy"
    assert tp.ops_signal(None, None).tone == tp.UNKNOWN


def test_worst_role_comes_first():
    signals = [tp.RoleSignal("A", "", "", tone=tp.GOOD), tp.RoleSignal("B", "", "", tone=tp.BAD),
               tp.RoleSignal("C", "", "", tone=tp.UNKNOWN), tp.RoleSignal("D", "", "", tone=tp.WARN)]
    assert [s.role for s in tp.sort_for_attention(signals)] == ["B", "D", "C", "A"]


def test_morning_brief_card_and_text_carry_the_pulse():
    note = release_notes.ReleaseNote(start=NOW - timedelta(days=1), end=NOW, days=1)
    note.pulse = [tp.RoleSignal("QC", "🐞", "/x", tone=tp.BAD, facts=[("lỗi mở:", "4")],
                                action="xử lý")]
    card = release_notes.render_card(note)
    facts = [b for b in card if "🧭 THEO VAI" in str(b)]
    assert facts and "🔴 QC" in str(facts[0]) and "lỗi mở: 4" in str(facts[0])
    _heading, text = release_notes.render_teams(note)
    assert "**🧭 Theo vai**" in text and "🔴 **QC**" in text


@pytest.mark.asyncio
async def test_without_ado_every_role_says_why():
    from ai_autopilot.config import Settings
    from ai_autopilot.services import team_pulse_service
    signals = await team_pulse_service.gather(NS(config=Settings(dry_run=True)))
    assert [s.role for s in signals] == ["BA", "PM", "Dev", "QC", "DevSecOps"]
    assert all(s.tone == tp.UNKNOWN and "Azure DevOps" in s.reason for s in signals)


def test_team_page_renders(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from ai_autopilot.app import create_app
    from ai_autopilot.config import Settings
    from ai_autopilot.services import team_pulse_service

    async def fake(c, now=None):
        return [tp.RoleSignal("QC", "🐞", "/dashboard/defects", tone=tp.BAD,
                              facts=[("lỗi mở:", "4")], action="xử lý lỗi cũ")]

    monkeypatch.setattr(team_pulse_service, "gather", fake)
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    with TestClient(create_app(cfg)) as client:
        html = client.get("/dashboard/team").text
    assert "Cần xử lý" in html and "xử lý lỗi cũ" in html and "/dashboard/defects" in html
