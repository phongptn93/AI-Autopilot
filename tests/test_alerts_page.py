"""The alert settings must be findable AND savable from the Settings page.

The complaint that started this was "I can't find where to configure alerts" — so the
test that matters is not that the values exist in Settings, but that the page renders
them together and a POST from that page persists them.
"""
from __future__ import annotations

import pytest
import yaml
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings

SECTION = "\U0001F514 Cảnh báo"


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    with TestClient(create_app(settings)) as c:
        yield c


def test_every_alert_control_lives_in_one_section(client: TestClient):
    """They used to be spread over five sections, none of them called "alerts"."""
    html = client.get("/dashboard/settings").text
    assert SECTION in html

    for name in (
        "alert_events", "alert_min_severity", "alert_dedup_enabled",
        "alert_repeat_hours", "alert_snooze_default_days",
        "digest_skip_when_empty", "digest_respect_quiet_hours",
        "delivery_merge_hours", "delivery_review_hours", "delivery_stale_days",
        "pr_reviewer_reminder_hours", "teams_agent_digest_interval_hours",
        "teams_agent_digest_at", "notify_hours_start", "notify_days",
    ):
        assert f'name="{name}"' in html, name


def test_the_section_appears_in_the_page_navigation(client: TestClient):
    """A section you can only reach by scrolling 160 fields is still hard to find."""
    html = client.get("/dashboard/settings").text
    assert html.count(SECTION) >= 2      # quick-nav tab + the section heading


def test_alert_settings_save_from_the_page(client: TestClient, tmp_path):
    resp = client.post(
        "/dashboard/settings",
        data={
            "workspace_directory": "/ws",
            "alert_events": "failed,error",
            "alert_min_severity": "warning",
            "alert_repeat_hours": "48",
            "delivery_stale_days": "5",
            "digest_skip_when_empty": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303)

    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["alert_events"] == "failed,error"
    assert saved["alert_min_severity"] == "warning"
    assert saved["alert_repeat_hours"] == 48
    assert saved["delivery_stale_days"] == 5

    # And it takes effect, rather than merely being stored.
    cfg = Settings(**{k: v for k, v in saved.items() if k in Settings.model_fields})
    assert not cfg.wants_alert("completed", 10)     # success is INFO → below the floor
    assert cfg.wants_alert("failed", 20)
