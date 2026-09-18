"""The live config object is WRITTEN to while the process runs, and everything here is
about that fact.

Three doors write straight onto ``Settings`` so a change takes effect without a restart:
the Settings page, the setup wizard, and a fleet sync. All three hand over JSON/form
shapes — a ``sdlc_roles`` off the wire is a dict of dicts, not a dict of ``SdlcRole`` —
and the object used to store them verbatim. What followed was a machine that looked
configured and was not: pages 500'd, the poller stayed asleep, and the cure everybody
found was the restart the whole design exists to avoid.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from starlette.testclient import TestClient

from ai_autopilot import fleet
from ai_autopilot.app import create_app
from ai_autopilot.config import SdlcRole, Settings, WorkspaceConfig
from ai_autopilot.dashboard import settings_form

TOKEN = "s3cret-fleet"


def _settings(tmp_path, **over) -> Settings:
    return Settings(
        dry_run=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'live.db'}",
        **over,
    )


def _done():
    fut: asyncio.Future = asyncio.Future()
    fut.set_result(None)
    return fut


# ── what a write leaves behind ───────────────────────────────────────────────


def test_a_nested_setting_written_as_plain_data_becomes_its_real_type():
    """The shape that arrives over the wire is JSON. The shape every reader expects is
    a model. Assignment is where the two must meet."""
    cfg = Settings()
    settings_form.apply_to_config(cfg, {
        "sdlc_roles": {"dev": {"stages": ["implement"], "waits_in": "Ready for Development"}},
        "workspaces": [{"name": "DX", "provider": "jira", "jira_project": "DX"}],
    })
    assert isinstance(cfg.sdlc_roles["dev"], SdlcRole)
    assert cfg.sdlc_roles["dev"].stages == ["implement"]
    assert isinstance(cfg.workspaces[0], WorkspaceConfig)
    assert cfg.workspaces[0].provider == "jira"


def test_one_unusable_value_does_not_cost_the_rest_of_the_document():
    """A central serving a single malformed key must not leave a worker half-applied
    with no idea which half."""
    cfg = Settings(max_revisions=3)
    rejected = settings_form.apply_to_config(cfg, {
        "max_revisions": 9,
        "sdlc_roles": {"dev": "this is not a role"},
        "review_tag": "looked-at",
    })
    assert rejected == ["sdlc_roles"]
    assert cfg.max_revisions == 9 and cfg.review_tag == "looked-at"
    assert cfg.sdlc_roles == {}


def test_the_pages_survive_a_fleet_sync(tmp_path):
    """The two pages an operator actually reported: both read ``role.stages``, so both
    died on a dict the moment a worker synced — and came back after a restart, which is
    why it read as "must restart to work"."""
    cfg = _settings(tmp_path)
    cfg.dashboard_auth_password_hash = ""
    with TestClient(create_app(cfg)) as client:
        assert client.get("/dashboard/roles").status_code == 200
        settings_form.apply_to_config(cfg, {          # what FleetAgentService._apply does
            "sdlc_roles": {
                "dev": {"stages": ["implement", "pr"], "waits_in": "Ready for Development",
                        "shows": "In Development", "done": "Ready for Testing", "auto": True},
            },
            "sdlc_stage_wiring": {
                "implement": {"entry_states": ["Ready for Development"],
                              "working_state": "In Development"},
            },
        })
        assert client.get("/dashboard/roles").status_code == 200
        assert client.get("/dashboard/board-views").status_code == 200


# ── the poller waits instead of giving up ────────────────────────────────────


def test_a_machine_with_no_credentials_yet_waits_rather_than_shutting_down(tmp_path):
    """First run has no PAT — that is the premise of the setup wizard. The loop used to
    return, so a wizard that wrote credentials into the live config produced a
    configured machine that polled nothing until someone restarted it."""
    from ai_autopilot.services.poller import AdoPollerService

    cfg = _settings(tmp_path, ado_pat="", poll_interval_seconds=1)
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                               error=lambda *a, **k: None)

    async def run() -> bool:
        waiting = asyncio.create_task(svc._await_credentials())
        await asyncio.sleep(0.05)
        still_waiting = not waiting.done()
        cfg.ado_pat = "PAT-typed-into-the-wizard"       # no restart, no re-registration
        await asyncio.wait_for(waiting, timeout=10)
        return still_waiting

    assert asyncio.run(run()) is True


def test_a_jira_only_machine_counts_as_having_credentials():
    """``has_auth`` asks about Azure DevOps. The poller asks every provider, so the gate
    on polling had to as well — a Jira team's machine polled nothing, forever, and was
    told to set a PAT it does not own."""
    cfg = Settings(ado_pat="")
    assert cfg.has_tracker_auth is False
    cfg.workspaces = [WorkspaceConfig(
        name="DX", provider="jira", jira_project="DX",
        jira_url="https://acme.atlassian.net", jira_token="t", ado_projects=["DX"],
    )]
    assert cfg.has_tracker_auth is True


# ── the fleet page tells the truth about each machine ────────────────────────


def _agent(cfg, *, audit=None):
    from ai_autopilot.services.fleet_agent import FleetAgentService

    svc = FleetAgentService.__new__(FleetAgentService)
    svc._config = cfg
    svc._log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                               error=lambda *a, **k: None)
    svc._task = None
    svc._c = SimpleNamespace(audit_repo=audit, ado=SimpleNamespace(refresh=lambda: None))
    # `__new__` skips __init__, which is the repo's usual shortcut for this service —
    # so the bookkeeping __init__ would have set has to be set here too.
    svc.central_version, svc.behind, svc._warned_version = "", False, ""
    return svc


async def test_a_structured_setting_that_did_not_change_is_not_rewritten(tmp_path, monkeypatch):
    """The live value is a model and the document is JSON, so a plain ``!=`` called
    every structured setting "changed" on every beat: config.yaml rewritten, an audit
    row filed, and the fleet page showing a worker eternally out of sync."""
    path = tmp_path / "worker.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(path))
    cfg = Settings(
        fleet_role="worker",
        sdlc_roles={"dev": SdlcRole(stages=["implement"], waits_in="Ready for Development")},
    )
    svc = _agent(cfg, audit=SimpleNamespace(record=lambda **kw: _done()))
    document, _ = fleet.config_document(cfg)          # byte-for-byte what the central sends
    assert await svc._apply({"config": document}) == []
    assert not path.exists()


def test_testing_the_connection_does_not_enrol_a_machine(tmp_path):
    """The wizard's "test it now" has to send a REAL heartbeat — reachability proves
    nothing about the token — but the machine pressing it is usually not configured yet,
    so every install left a phantom `setup-check` worker on the fleet page."""
    cfg = _settings(tmp_path, fleet_role="central", fleet_token=TOKEN)
    with TestClient(create_app(cfg)) as client:
        probe = fleet.WorkerReport(name="setup-check", probe=True)
        answer = client.post("/api/fleet/heartbeat", json=probe.model_dump(mode="json"),
                             headers={fleet.TOKEN_HEADER: TOKEN})
        assert answer.status_code == 200
        assert answer.json()["config_hash"]            # still proves what it claims
        assert answer.json()["central_version"]
        assert "setup-check" not in client.get("/dashboard/fleet").text


async def test_the_shared_run_now_tag_is_not_listed_as_a_machines_own():
    """``stage_entry_tag`` is served by the central to everybody, so printing it under
    "tag riêng của máy" put an identical chip on every row of the one column whose job
    is to show where machines differ."""
    cfg = Settings(fleet_role="worker", fleet_worker_name="dev-01",
                   trigger_tag="dev-01-autopilot", stage_entry_tag="vm-autopilot-run",
                   assignee_trigger_tag="ai-autopilot")
    svc = _agent(cfg)
    svc._c = SimpleNamespace(execution_repo=SimpleNamespace())   # no DB — report still builds

    report = await svc.build_report()

    assert "vm-autopilot-run" not in report.tags
    assert "dev-01-autopilot" in report.tags
    assert "ai-autopilot" in report.tags
    assert report.probe is False            # a real beat enrols; only the wizard probes


# ── a worker must know when the centre moved on without it ───────────────────


def test_only_an_older_build_counts_as_behind():
    """"Different" was the old test and it flagged the wrong machines: a worker running
    AHEAD of the central mid-rollout got the same red chip as one running code that
    predates the settings it is being handed."""
    assert fleet.is_behind("2.47.1", "2.48.0") is True
    assert fleet.is_behind("2.48.0", "2.47.1") is False      # ahead is not a problem
    assert fleet.is_behind("2.48.0", "2.48.0") is False
    assert fleet.is_behind("", "2.48.0") is False            # cannot tell → say nothing
    assert fleet.is_behind("nightly", "2.48.0") is False


async def test_the_worker_is_told_when_it_trails_the_central(tmp_path, monkeypatch):
    """The central's page could always see the drift. The machine that has to act on it
    could not see it anywhere — and an older build silently DROPS settings its Settings
    class has no field for, so this is a correctness warning, not a cosmetic one."""
    from ai_autopilot import __version__

    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "worker.yaml"))
    cfg = Settings(fleet_role="worker")
    svc = _agent(cfg)
    warnings: list[dict] = []
    svc._log = SimpleNamespace(
        info=lambda *a, **k: None, error=lambda *a, **k: None,
        warning=lambda *a, **k: warnings.append(k),
    )

    ahead = ".".join([str(int(__version__.split(".")[0]) + 1), "0", "0"])
    svc._note_central_version(ahead)
    assert svc.behind is True
    assert warnings and warnings[0]["central_version"] == ahead
    assert "pip install" in warnings[0]["fix"]

    svc._note_central_version(ahead)        # nagged once per version, not per beat
    assert len(warnings) == 1

    svc._note_central_version(__version__)  # central caught up / we upgraded
    assert svc.behind is False


def test_the_worker_page_says_which_command_fixes_it(tmp_path):
    cfg = _settings(tmp_path, fleet_role="worker", fleet_central_url="http://central",
                    fleet_token=TOKEN)
    cfg.dashboard_auth_password_hash = ""
    with TestClient(create_app(cfg)) as client:
        agent = client.app.state.fleet_agent
        agent.behind, agent.central_version = True, "9.9.9"
        page = client.get("/dashboard/fleet").text
        assert "đang chạy bản cũ hơn trung tâm" in page
        assert "9.9.9" in page and "pip install" in page
