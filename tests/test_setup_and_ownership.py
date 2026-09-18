"""Who owns a setting, and the short path to a machine that works.

Three things that only make sense together:

* **Ownership.** A fleet has settings the team shares, settings each machine answers
  for itself, and secrets that never travel. Only two of those groups existed, so the
  middle one was being served by the central — which is how one `sdlc_profile` turns a
  whole fleet into one role.
* **The worker's Settings page.** What the central serves is put away and locked there,
  because editing it would be undone by the next heartbeat.
* **Setup.** The eight fields a machine cannot start without, in the order the answers
  depend on each other.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from ai_autopilot import fleet
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard import settings_form as sf


@pytest.fixture
def own_config(tmp_path, monkeypatch):
    """Point config.yaml at this test's tmp_path.

    Anything that saves writes through `config_file_path()`; without this the run's
    shared sentinel config is created and every later `Settings()` inherits it (see
    tests/conftest.py, which fails the writer rather than the 34 readers after it).
    """
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))


def _settings(tmp_path, **over) -> Settings:
    return Settings(
        dry_run=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'own.db'}",
        dashboard_auth_password_hash="", dashboard_auth_token="",
        **over,
    )


# ── ownership ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", sorted(sf.MACHINE_LOCAL))
def test_a_machine_local_setting_never_leaves_the_central(key):
    """Each of these has a perfectly good value that is simply DIFFERENT per machine,
    so a central serving one is serving the wrong one to everybody else."""
    document, _ = fleet.config_document(Settings())
    assert key not in document
    # And the worker drops it again on arrival, so a misconfigured central cannot
    # write it either — the same both-ends rule the secrets already get.
    assert fleet.strip_local({key: "whatever"}) == {}


def test_the_two_reasons_stay_separate():
    """"This is a secret" and "your machine decides this" are different sentences, and
    a page that says the first about max_concurrent is lying."""
    assert not (sf.MACHINE_LOCAL & sf.NEVER_SHARED)
    assert sf.EXPORT_EXCLUDE == sf.NEVER_SHARED | sf.MACHINE_LOCAL
    assert sf.MACHINE_LOCAL <= set(Settings.model_fields)


def test_scheduled_loops_are_per_machine():
    """LoopScheduler has no cross-machine coordination, so a shared list is N machines
    running the same cron: N Claude runs, N pull requests, N times the spend."""
    assert "scheduled_loops" in sf.MACHINE_LOCAL


def test_a_plural_trigger_tag_can_no_longer_be_pushed_to_everyone():
    """`trigger_tag` was already local precisely so two machines do not fight over one
    work item — and `effective_trigger_tags` merges the plural straight into it."""
    assert {"trigger_tag", "trigger_tags"} <= sf.EXPORT_EXCLUDE


def test_owner_depends_on_the_role_of_the_machine_you_are_on():
    standalone = Settings()
    worker = Settings(fleet_role="worker", fleet_local_keys=["board_max_per_column"])
    # A machine with no central owns everything it holds.
    assert sf.owner_of("board_drop_map", standalone) == sf.OWNER_MACHINE
    # On a worker: served, claimed, and mine-by-nature are three different answers.
    assert sf.owner_of("board_drop_map", worker) == sf.OWNER_CENTRAL
    assert sf.owner_of("board_max_per_column", worker) == sf.OWNER_CLAIMED
    assert sf.owner_of("sdlc_profile", worker) == sf.OWNER_MACHINE
    assert sf.writable_here("board_drop_map", worker) is False
    assert sf.writable_here("board_max_per_column", worker) is True


# ── the worker's Settings page ───────────────────────────────────────────────

def test_a_worker_cannot_save_what_the_central_serves(tmp_path, own_config):
    """Refused server-side, not just disabled in the markup: the next heartbeat puts
    the central's value back, so a green "đã lưu" would be a lie — and the same POST
    can arrive from curl."""
    cfg = _settings(tmp_path, fleet_role="worker", fleet_token="t",
                    fleet_central_url="http://c", board_max_per_column=20)
    with TestClient(create_app(cfg)) as client:
        client.post("/dashboard/settings", data={
            "board_max_per_column": "99",      # central's to decide
            "max_concurrent": "3",             # this machine's own capacity
        })
        assert client.app.state.container.config.board_max_per_column == 20
        assert client.app.state.container.config.max_concurrent == 3


def test_claiming_a_setting_makes_it_editable_here(tmp_path, own_config):
    cfg = _settings(tmp_path, fleet_role="worker", fleet_token="t",
                    fleet_central_url="http://c", board_max_per_column=20)
    with TestClient(create_app(cfg)) as client:
        live = client.app.state.container.config
        client.post("/dashboard/settings/claim", data={"key": "board_max_per_column"})
        assert "board_max_per_column" in live.fleet_local_keys
        client.post("/dashboard/settings", data={"board_max_per_column": "99"})
        assert live.board_max_per_column == 99
        # …and handing it back returns it to the central's care.
        client.post("/dashboard/settings/claim",
                    data={"key": "board_max_per_column", "release": "1"})
        assert "board_max_per_column" not in live.fleet_local_keys


def test_only_a_worker_claims_anything(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.post("/dashboard/settings/claim",
                           data={"key": "board_drop_map"}).status_code == 404


def test_the_worker_page_locks_what_it_shows(tmp_path):
    cfg = _settings(tmp_path, fleet_role="worker", fleet_token="t",
                    fleet_central_url="http://c")
    with TestClient(create_app(cfg)) as client:
        page = client.get("/dashboard/settings").text
    # The per-field pill is gone on purpose: these are hidden until the section toggle
    # — which says "do trung tâm quản lý" once — is pressed, and each is visibly locked.
    # Stamping the same badge onto 152 of 180 fields repeated what the group already
    # said and squeezed the label itself off the row. What stays is the actionable bit.
    assert "own-badge own-central" not in page
    assert "Giành quyền" in page
    assert "<fieldset disabled" in page          # revealed, but not editable
    start = page.index('data-k="board_drop_map"')
    assert 'data-owner="central"' in page[start - 300:start + 120]


def test_a_standalone_page_is_unchanged(tmp_path):
    """Nobody without a fleet should meet fleet vocabulary on their Settings page.

    Asserted on what is RENDERED, not on the words: the toggle script carries both
    labels either way, so searching the page text for "trung tâm quản lý" matches a
    string constant in the JavaScript and proves nothing.
    """
    with TestClient(create_app(_settings(tmp_path))) as client:
        page = client.get("/dashboard/settings").text
    assert "own-badge own-central" not in page   # no field is marked central-managed
    assert "Giành quyền" not in page             # and nothing to claim
    assert "<fieldset disabled" not in page      # nothing locked
    assert 'data-owner="central"' not in page


# ── setup ────────────────────────────────────────────────────────────────────

def test_each_role_gets_the_steps_it_actually_needs(tmp_path):
    """A worker's central serves almost everything, so its path is the short one."""
    for role, expect in (("", "workspace"), ("central", "fleet"), ("worker", "connect")):
        cfg = _settings(tmp_path, fleet_role=role)
        with TestClient(create_app(cfg)) as client:
            page = client.get(f"/dashboard/setup?step={expect}")
        assert page.status_code == 200, (role, expect)


def test_a_worker_is_never_asked_for_the_team_policy(tmp_path):
    """Its central decides autonomy and model. An unknown step falls back to step zero
    rather than 404-ing, so a stale bookmark lands somewhere useful."""
    cfg = _settings(tmp_path, fleet_role="worker")
    with TestClient(create_app(cfg)) as client:
        page = client.get("/dashboard/setup?step=policy")
    assert page.status_code == 200
    assert 'name="step" value="role"' in page.text      # sent back to the start
    assert 'name="autonomy_level"' not in page.text     # never offered the team policy


def test_choosing_the_role_decides_the_rest_of_the_wizard(tmp_path, own_config):
    cfg = _settings(tmp_path)
    with TestClient(create_app(cfg)) as client:
        resp = client.post("/dashboard/setup",
                           data={"step": "role", "fleet_role": "worker"},
                           follow_redirects=False)
        # The next step is computed AFTER the role is applied — it is the role that
        # decides which steps exist at all.
        assert resp.headers["location"] == "/dashboard/setup?step=connect"
        assert client.app.state.container.config.fleet_role == "worker"


def test_a_setup_step_writes_through_the_same_path_as_settings(tmp_path, own_config):
    cfg = _settings(tmp_path)
    with TestClient(create_app(cfg)) as client:
        client.post("/dashboard/setup", data={
            "step": "workspace", "base_branch": "develop", "trigger_tag": "box-autopilot",
        })
        live = client.app.state.container.config
        assert live.base_branch == "develop"
        assert live.trigger_tag == "box-autopilot"


def test_a_blank_secret_in_setup_keeps_the_stored_one(tmp_path, own_config):
    cfg = _settings(tmp_path, ado_pat="KEEP-ME")
    with TestClient(create_app(cfg)) as client:
        client.post("/dashboard/setup", data={
            "step": "ado", "ado_organization": "https://dev.azure.com/x", "ado_pat": "",
        })
        assert client.app.state.container.config.ado_pat == "KEEP-ME"


def test_the_last_step_reports_with_the_real_doctor(tmp_path):
    """A wizard that grades itself against its own shorter checklist is how "setup
    complete" and "actually working" drift apart."""
    cfg = _settings(tmp_path, fleet_role="worker", fleet_token="", fleet_central_url="")
    with TestClient(create_app(cfg)) as client:
        page = client.get("/dashboard/setup?step=done").text
    # doctor's own fleet findings, verbatim — not a second opinion written here.
    assert "fleet_central_url" in page
    assert "ERROR" in page


def test_the_central_check_calls_a_real_heartbeat(tmp_path):
    """Reachability proves nothing: the question is whether the token matches and
    whether that host is a central at all."""
    cfg = _settings(tmp_path, fleet_role="worker", fleet_central_url="", fleet_token="")
    with TestClient(create_app(cfg)) as client:
        body = client.post("/dashboard/setup/check/fleet").json()
    assert body["ok"] is False and "URL trung tâm" in body["detail"]
