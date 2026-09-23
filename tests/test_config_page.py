"""The read-only Configuration page — generated, so it cannot fall behind Settings."""

from __future__ import annotations

import re

from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard import settings_form


def _page(tmp_path) -> str:
    settings = Settings(
        dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'cfg.db'}"
    )
    with TestClient(create_app(settings)) as client:
        return client.get("/dashboard/config").text


def test_the_page_covers_every_setting_settings_can_edit(tmp_path):
    """It was a hand-written copy and had drifted to 61 of 180 fields.

    Whole sections — alerts, PR review & feedback, the Teams bot, fleet, quality gates —
    were missing, each because somebody added a setting to Settings and this page was a
    separate list nobody remembered. Generating it from the same registry is what makes
    the gap impossible; this test is what keeps it that way.
    """
    body = _page(tmp_path)
    rendered = set(re.findall(r'data-find="[^"]*?([a-z_][a-z0-9_]{3,})', body))
    missing = [
        f.key for f in settings_form.FIELDS
        if f.key not in body                       # the key appears in the row's search text
    ]
    assert not missing, f"{len(missing)} settings are not on the Configuration page: {missing[:8]}"
    assert rendered                                 # the rows really rendered


def test_a_secret_is_reported_as_set_but_never_printed(tmp_path):
    settings = Settings(
        dry_run=True,
        ado_pat="super-secret-token-value",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'sec.db'}",
    )
    with TestClient(create_app(settings)) as client:
        body = client.get("/dashboard/config").text
    assert "super-secret-token-value" not in body   # never, under any view
    assert "đã đặt" in body


def test_a_setting_left_at_its_default_is_not_reported_as_changed(tmp_path):
    """The page opens on "đã đổi"; if defaults counted, that view would be all 180 rows
    and would answer nothing."""
    body = _page(tmp_path)
    changed = body.count('data-changed="1"')
    total = body.count("cfg-row ")
    assert total >= 150                            # the whole registry is on the page
    assert changed < total // 3                    # and a fresh install has changed few
