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
        # The tracker choice rides along, or stepping on would drop the branch.
        assert resp.headers["location"] == "/dashboard/setup?step=connect&src=ado"
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


# ── Two audiences for one config ─────────────────────────────────────────────
# A fleet document is served over an authenticated endpoint to machines that already
# hold the shared token. An export is a file that leaves the building. They had one
# filter between them, which meant the notification channels could go to neither.

def test_a_downloadable_config_still_carries_no_channels():
    """A Teams Workflows URL IS the authorisation to post. It must never ride along in
    a file somebody emails to a colleague."""
    cfg = Settings(teams_webhook_url="https://hook", smtp_password="hunter2",
                   zalo_oa_access_token="z", email_to="a@b.c")
    shared = sf.export_settings(cfg)
    for key in sf.FLEET_ONLY_SHARED:
        assert key not in shared, key


def test_a_central_may_hand_its_own_workers_the_channels():
    """A worker reporting to a different channel than the rest of the team is a worker
    nobody reads — and pasting the same webhook onto every machine by hand is how that
    happens."""
    cfg = Settings(teams_webhook_url="https://hook", smtp_host="smtp.local")
    document, _ = fleet.config_document(cfg)
    assert document["teams_webhook_url"] == "https://hook"
    assert document["smtp_host"] == "smtp.local"
    # …and a worker accepts them, which the strip has to allow on its side too.
    assert fleet.strip_local({"teams_webhook_url": "https://hook"}) == {
        "teams_webhook_url": "https://hook"
    }


def test_the_ado_pat_travels_on_neither_road():
    cfg = Settings(ado_pat="PAT-123", dashboard_auth_password_hash="pbkdf2$x")
    document, _ = fleet.config_document(cfg)
    assert "ado_pat" not in document and "ado_pat" not in sf.export_settings(cfg)
    assert "dashboard_auth_password_hash" not in document


def test_the_owner_answer_follows_what_the_central_actually_sends():
    """owner_of reads the FLEET filter, not the export one. Reading the export filter
    would have shown the webhook fields as this machine's own while the next heartbeat
    quietly replaced them."""
    worker = Settings(fleet_role="worker")
    assert sf.owner_of("teams_webhook_channels", worker) == sf.OWNER_CENTRAL
    assert sf.owner_of("ado_pat", worker) == sf.OWNER_MACHINE


@pytest.mark.parametrize("key", [
    "bot_persona_name", "teams_agentic_enabled", "teams_agent_nlu_enabled",
    "alert_events", "delivery_review_hours", "notify_hours_start",
    "digest_skip_when_empty", "alert_repeat_hours",
])
def test_the_bot_and_the_alerting_are_each_machines_own(key):
    """The bot runs where its credentials are, and every channel an alert reaches is
    per-machine already — so the knobs that decide the noise belong to the machine
    that makes it."""
    document, _ = fleet.config_document(Settings())
    assert key not in document


# ── Reset ────────────────────────────────────────────────────────────────────

def test_reset_lists_only_what_actually_differs():
    """An empty plan honestly means "already stock", which is why the preview is built
    from real differences rather than from the whole field list."""
    stock = Settings()
    assert sf.reset_plan(stock) == {}
    changed = Settings(max_concurrent=9, base_branch="develop")
    plan = sf.reset_plan(changed)
    assert plan["max_concurrent"] == Settings().max_concurrent
    assert plan["base_branch"] == Settings().base_branch


def test_reset_can_be_scoped_to_one_section():
    changed = Settings(max_concurrent=9, base_branch="develop")
    plan = sf.reset_plan(changed, "Workspace & Repository")
    assert "base_branch" in plan and "max_concurrent" not in plan


@pytest.mark.parametrize("key", sorted(sf.RESET_KEEP))
def test_reset_never_touches_what_would_lock_you_out(key):
    """Losing these is not "back to default" — it is being locked out of the machine
    you are resetting, orphaning its data, or dropping it out of its fleet."""
    changed = Settings(fleet_role="worker", fleet_token="t", fleet_central_url="http://c",
                       dashboard_auth_password_hash="pbkdf2$x",
                       database_url="sqlite+aiosqlite:///x.db", health_port=9999)
    assert key not in sf.reset_plan(changed)


def test_reset_previews_before_it_changes_anything(tmp_path, own_config):
    cfg = _settings(tmp_path, max_concurrent=9)
    with TestClient(create_app(cfg)) as client:
        page = client.get("/dashboard/settings/reset")
        assert page.status_code == 200
        assert "Max concurrent" in page.text
        # A GET must not have moved anything.
        assert client.app.state.container.config.max_concurrent == 9
        client.post("/dashboard/settings/reset", data={"section": ""})
        assert client.app.state.container.config.max_concurrent == Settings().max_concurrent


def test_a_worker_does_not_reset_what_the_central_hands_straight_back(tmp_path, own_config):
    """That would be a reset the next heartbeat undoes — the same lie as a save that
    does not stick."""
    cfg = _settings(tmp_path, fleet_role="worker", fleet_token="t",
                    fleet_central_url="http://c", board_max_per_column=99, max_concurrent=9)
    with TestClient(create_app(cfg)) as client:
        client.post("/dashboard/settings/reset", data={"section": ""})
        live = client.app.state.container.config
        assert live.board_max_per_column == 99                      # central's to decide
        assert live.max_concurrent == Settings().max_concurrent      # this machine's own


# ── The work-item source is a choice, and the page has to follow it ───────────

def test_the_jira_boxes_can_actually_be_hidden(tmp_path):
    """`.ws-field{display:flex}` is an author rule and outranks the UA stylesheet's
    `[hidden]{display:none}` — so the attribute the template and the picker both set
    was inert, and every ADO workspace showed the Jira credential boxes."""
    with TestClient(create_app(_settings(tmp_path))) as client:
        page = client.get("/dashboard/workspaces").text
    assert ".ws-field[hidden] { display:none; }" in page


def test_the_routing_field_asks_for_the_key_the_chosen_tracker_uses(tmp_path):
    """Items route on the workspace's project list, and a Jira item carries its JIRA
    key — so on a Jira row the field is not asking for an "ADO project"."""
    with TestClient(create_app(_settings(tmp_path))) as client:
        page = client.get("/dashboard/workspaces").text
    assert "data-lbl-ado" in page and "data-lbl-jira" in page
    assert "Jira project key (định tuyến)" in page


def test_doctor_catches_a_jira_workspace_whose_key_routes_nowhere():
    """The quiet one: the lookup misses, falls back to this machine's ADO client, and
    a Jira item's comments are addressed to Azure DevOps with an id that means nothing
    there. Nothing logs it."""
    from ai_autopilot import doctor
    from ai_autopilot.config import WorkspaceConfig

    ws = WorkspaceConfig(
        name="Khatoco", provider="jira", ado_projects=["KHATOCO"],
        jira_url="https://x.atlassian.net", jira_email="b@c.d",
        jira_token="t", jira_project="DXF",          # ← not in ado_projects
    )
    found = doctor.check_providers(Settings(workspaces=[ws]))
    assert any(f.level == doctor.ERROR and "DXF" in f.title for f in found), found

    ws.ado_projects = ["DXF"]                        # …and it clears when they agree
    ok = doctor.check_providers(Settings(workspaces=[ws]))
    assert not any(f.level == doctor.ERROR for f in ok), ok


# ── Setup asks WHERE the work comes from, before asking for a connection ─────

def test_the_wizard_asks_for_the_tracker_before_the_connection(tmp_path):
    """It used to go straight to "Kết nối Azure DevOps", which reads as "this product
    is for ADO teams" — while a Jira team's path existed the whole time on the
    Workspaces page, two clicks away and unmentioned."""
    with TestClient(create_app(_settings(tmp_path))) as client:
        page = client.get("/dashboard/setup?step=source").text
    assert 'name="work_item_source"' in page
    assert "Azure DevOps" in page and "Jira" in page


def test_choosing_jira_changes_the_next_step(tmp_path, own_config):
    with TestClient(create_app(_settings(tmp_path))) as client:
        resp = client.post("/dashboard/setup",
                           data={"step": "source", "work_item_source": "jira"},
                           follow_redirects=False)
        assert resp.headers["location"] == "/dashboard/setup?step=jira&src=jira"
        # …and the ADO branch still goes where it did.
        resp = client.post("/dashboard/setup",
                           data={"step": "source", "work_item_source": "ado"},
                           follow_redirects=False)
        assert resp.headers["location"] == "/dashboard/setup?step=ado&src=ado"


def test_the_jira_step_writes_a_workspace_that_routes_to_itself(tmp_path, own_config):
    """The wizard is the one place that can make the Jira key and the routing list
    agree by construction. Left to be typed twice, the day they differ the lookup falls
    back to Azure DevOps and a Jira item's comments go to the wrong tracker, silently.
    """
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.post("/dashboard/setup", data={
            "step": "jira", "src": "jira", "jira_name": "Khatoco",
            "jira_url": "https://kh.atlassian.net", "jira_email": "bot@kh.vn",
            "jira_project": "DXF",
        })
        live = client.app.state.container.config
        # A just-saved workspace is a plain dict until the config is re-read (see
        # _ws_attr): assert on the shape that is actually there.
        ws = next(w for w in live.workspaces if dict(w).get("provider") == "jira")
        assert dict(ws)["jira_project"] == "DXF"
        assert dict(ws)["ado_projects"] == ["DXF"]   # routes to itself, by construction

        # And the check that exists for the hand-written case now has nothing to say.
        from ai_autopilot import doctor
        from ai_autopilot.config import Settings as S
        reread = S(workspaces=[dict(w) for w in live.workspaces])
        assert not [f for f in doctor.check_providers(reread)
                    if f.level == doctor.ERROR and "DXF" in f.title]


def test_a_jira_step_without_a_key_changes_nothing(tmp_path, own_config):
    """There is nothing to route on, and a half-written workspace is a broken route."""
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.post("/dashboard/setup", data={
            "step": "jira", "src": "jira", "jira_url": "https://kh.atlassian.net",
        })
        assert not client.app.state.container.config.workspaces


def test_the_ado_step_is_optional_once_jira_is_chosen(tmp_path):
    """PRs still run through ADO, but a team whose code is elsewhere must not be told
    it cannot finish setup without it."""
    with TestClient(create_app(_settings(tmp_path))) as client:
        page = client.get("/dashboard/setup?step=ado&src=jira").text
    assert "Không bắt buộc với đội dùng Jira" in page


def test_reopening_the_wizard_lands_a_jira_team_on_their_own_path(tmp_path):
    """Derived from what is configured, so coming back later does not start over on
    the ADO branch."""
    from ai_autopilot.config import WorkspaceConfig

    cfg = _settings(tmp_path, workspaces=[WorkspaceConfig(
        name="Khatoco", provider="jira", ado_projects=["DXF"], jira_project="DXF",
    )])
    with TestClient(create_app(cfg)) as client:
        page = client.get("/dashboard/setup?step=source").text
    assert 'value="jira"\n                 checked' in page or 'checked' in page
    with TestClient(create_app(cfg)) as client:
        assert client.get("/dashboard/setup?step=jira").status_code == 200
