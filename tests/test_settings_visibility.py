"""The Settings page shows what this machine actually uses — and nothing it doesn't.

Two rules, and they only look contradictory: a setting that does nothing here is not
worth reading, but a value somebody configured must never disappear from the page whose
job is to show the configuration. So: inapplicable + empty is hidden, inapplicable +
set is dimmed with the reason, and each section offers one click to show what it hid.

Also covers the one secret that can be read back (the fleet token): a shared secret
exists to be copied onto every other machine, so "type it again" is the wrong answer to
"what is it".
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard import settings_form


def _field(key: str) -> settings_form.Field:
    return next(f for f in settings_form.FIELDS if f.key == key)


@pytest.fixture
def client(tmp_path) -> TestClient:
    settings = Settings(
        dry_run=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        fleet_role="central",
        fleet_token="s3cret-fleet-token",
    )
    with TestClient(create_app(settings)) as c:
        yield c


# ── the rule itself ──────────────────────────────────────────────────────────

def test_a_bool_switch_is_read_as_ticked_or_not():
    """A checkbox has no value — mirrors what the browser sends for the same field."""
    assert settings_form.control_value({"test_gate_enabled": True}, "test_gate_enabled") == "1"
    assert settings_form.control_value({"test_gate_enabled": False}, "test_gate_enabled") == ""
    assert settings_form.control_value({"fleet_role": "worker"}, "fleet_role") == "worker"
    assert settings_form.control_value({}, "missing") == ""


@pytest.mark.parametrize(
    ("key", "current", "expected"),
    [
        # A worker-only field on a central, and the same field once the role changes.
        ("fleet_worker_name", {"fleet_role": "central"}, False),
        ("fleet_worker_name", {"fleet_role": "worker"}, True),
        # The test-gate settings are inert while the gate is off.
        ("test_timeout_seconds", {"test_gate_enabled": False}, False),
        ("test_timeout_seconds", {"test_gate_enabled": True}, True),
        # The interactive console has no counterpart in a headless run.
        ("interactive_close_on", {"execution_mode": "headless"}, False),
        ("interactive_close_on", {"execution_mode": "interactive"}, True),
        # A field with no parent always applies.
        ("ado_organization", {}, True),
    ],
)
def test_applies_follows_the_controlling_switch(key, current, expected):
    assert settings_form.applies(_field(key), current) is expected


def test_every_declared_parent_is_a_real_setting():
    """A typo here would hide a field behind a switch that does not exist."""
    keys = {f.key for f in settings_form.FIELDS}
    for child, (parent, _values) in settings_form._DEPENDS_ON.items():
        assert child in keys, child
        assert parent in keys, parent


def test_has_value_separates_hide_from_dim():
    """What is empty can be hidden; what is set must stay on the page."""
    current = {"fleet_worker_name": "", "fleet_local_keys": ["sdlc_profile"],
               "batch_stacked_prs": False}
    secrets_set = {"fleet_token": True, "ado_pat": False}
    assert settings_form.has_value(_field("fleet_worker_name"), current, secrets_set) is False
    assert settings_form.has_value(_field("fleet_local_keys"), current, secrets_set) is True
    assert settings_form.has_value(_field("batch_stacked_prs"), current, secrets_set) is False
    # Secrets are never echoed into `current`, so their answer comes from secrets_set.
    assert settings_form.has_value(_field("fleet_token"), current, secrets_set) is True
    assert settings_form.has_value(_field("ado_pat"), current, secrets_set) is False


# ── what the page renders ────────────────────────────────────────────────────

def _classes(html: str, key: str) -> set[str]:
    """The rendered class list of one field row (matched, not eyeballed: "off" is a
    substring of "offline" and of autocomplete="off")."""
    match = re.search(rf'<div class="([^"]*)"\s+data-k="{re.escape(key)}"', html)
    assert match, f"no field row rendered for {key}"
    return set(match.group(1).split())


def test_page_hides_the_inapplicable_and_keeps_the_configured(client: TestClient):
    html = client.get("/dashboard/settings").text

    # central: the worker-only name is empty here, so it is hidden outright…
    assert "off" in _classes(html, "fleet_worker_name")
    # …while the field the role DOES use is offered normally.
    assert not {"off", "na"} & _classes(html, "fleet_offline_after_minutes")
    # The badge names the control on screen, not the config key underneath it.
    assert "chỉ khi “Vai của máy này”" in html
    # A bool parent reads as a switch, not as `= 1`.
    assert "đang bật" in html
    # And every section carries the one click that shows what it held back.
    assert "data-na-toggle" in html


def test_a_configured_but_inapplicable_field_is_dimmed_not_hidden(tmp_path):
    """fleet_local_keys is worker-only. Set it, switch to central: it must stay."""
    settings = Settings(
        dry_run=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        fleet_role="central",
        fleet_local_keys=["sdlc_profile"],
    )
    with TestClient(create_app(settings)) as client:
        html = client.get("/dashboard/settings").text
    assert "na" in _classes(html, "fleet_local_keys")
    assert "off" not in _classes(html, "fleet_local_keys")
    start = html.index('data-k="fleet_local_keys"')
    assert "sdlc_profile" in html[start:start + 600]   # the value is still on the page


# ── revealing the one secret meant to be copied ──────────────────────────────

def test_fleet_token_can_be_read_back(client: TestClient):
    resp = client.get("/dashboard/settings/reveal/fleet_token")
    assert resp.status_code == 200
    assert resp.json() == {"key": "fleet_token", "value": "s3cret-fleet-token"}


@pytest.mark.parametrize("key", ["ado_pat", "smtp_password", "config_export_password",
                                 "dashboard_auth_password", "not_a_setting"])
def test_no_other_secret_can_be_read_back(client: TestClient, key: str):
    """Write-only stays write-only: nobody needs the PAT back, so there is no way out."""
    assert client.get(f"/dashboard/settings/reveal/{key}").status_code == 404


def test_only_the_fleet_token_is_revealable():
    assert [f.key for f in settings_form.FIELDS if f.reveal] == ["fleet_token"]


def test_the_page_says_the_fleet_token_is_set(client: TestClient):
    """It read "not set" on a central holding a perfectly good token — the exact
    sentence that sends someone rotating a working secret across the fleet."""
    html = client.get("/dashboard/settings").text
    start = html.index('data-k="fleet_token"')
    assert "set — leave blank to keep" in html[start:start + 900]


def test_a_default_value_is_not_treated_as_a_decision():
    """"Non-empty" hid almost nothing: every int and bool ships with a default."""
    defaults = {"fleet_offline_after_minutes": 30, "fleet_worker_name": ""}
    current = {"fleet_offline_after_minutes": 30, "fleet_worker_name": ""}
    assert settings_form.has_value(
        _field("fleet_offline_after_minutes"), current, {}, defaults) is False
    current["fleet_offline_after_minutes"] = 45          # somebody chose this
    assert settings_form.has_value(
        _field("fleet_offline_after_minutes"), current, {}, defaults) is True
