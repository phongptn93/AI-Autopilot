"""The AI scan pass runs on this machine's Claude Code — an API key is optional."""

from __future__ import annotations

from ai_autopilot import doctor
from ai_autopilot.config import Settings
from ai_autopilot.dashboard.routes import security


def _rows(monkeypatch, *, cli_level, key=""):
    monkeypatch.setenv("ANTHROPIC_API_KEY", key) if key else monkeypatch.delenv(
        "ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(doctor, "check_claude_cli",
                        lambda cfg: [doctor.Finding(cli_level, "claude")])
    cfg = Settings()
    cfg.security_scan.ai_mode = "fast"
    return {r["name"]: r for r in security._readiness(cfg)}


def test_claude_code_alone_is_enough(monkeypatch):
    ai = _rows(monkeypatch, cli_level=doctor.OK)["ai"]
    assert ai["ok"] and "Claude Code" in ai["note"] and not ai["install"]


def test_no_claude_and_no_key_explains_how_to_install(monkeypatch):
    ai = _rows(monkeypatch, cli_level=doctor.ERROR)["ai"]
    assert not ai["ok"] and any("claude.ai/install" in c for _, c in ai["install"])


def test_an_api_key_still_works_without_the_cli(monkeypatch):
    assert _rows(monkeypatch, cli_level=doctor.ERROR, key="sk-test")["ai"]["ok"]


def test_missing_scanners_carry_install_commands(monkeypatch):
    rows = _rows(monkeypatch, cli_level=doctor.OK)
    for name in ("gitleaks", "semgrep"):
        if not rows[name]["ok"]:
            assert rows[name]["install"]
