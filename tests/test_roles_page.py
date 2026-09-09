"""The Roles page: one row per role, and that row IS the chain.

What a role runs, where it waits, what it shows while running and where it hands off
used to live in five places keyed by four different things, so nothing on screen
showed that one role's way OUT is the next role's way IN. That is how a QC hand-off
became a trigger and reworked finished work — so the page is tested for saying a
whole role in one row, and for refusing the contradictions the old shape allowed.
"""

from __future__ import annotations

import yaml
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import SdlcRole, SdlcStageWiring, Settings
from ai_autopilot.execution import sdlc_plan


def _client(tmp_path, **overrides):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", **overrides)
    return TestClient(create_app(settings))


def test_the_page_shows_each_role_with_its_stages_door_and_dial(tmp_path):
    with _client(
        tmp_path,
        trigger_states=["New", "Ready for Development", "Ready for Testing"],
        sdlc_roles={
            "dev": SdlcRole(
                stages=["implement", "review", "pr"], waits_in="Ready for Development",
                shows="In Development", done="Ready for Testing", auto=True,
            ),
            "qc": SdlcRole(
                stages=["test"], waits_in="Ready for Testing", shows="In Testing", auto=False,
            ),
        },
    ) as client:
        page = client.get("/dashboard/roles").text
        assert "In Testing" in page and "In Development" in page
        assert "role_qc_auto" in page and "role_dev_auto" in page
        # Every field of a role is on its own row — including the way out.
        assert "role_dev_done" in page and "role_dev_stages" in page
        # The chain is asserted, not implied: dev hands to a door qc actually waits in.
        assert "Hands to" in page
        # The derived pickup set is shown, so the dial's effect is visible where it is set.
        assert "Poller starts from" in page


def test_a_hand_off_nobody_waits_in_is_called_a_dead_end(tmp_path):
    """The old chain drew an arrow between wired stages without checking anything
    linked them. A hand-off into a state no role claims must say so."""
    with _client(
        tmp_path,
        trigger_states=["New"],
        sdlc_roles={
            "dev": SdlcRole(stages=["implement"], waits_in="Ready for Development",
                            done="Nowhere In Particular", auto=True),
        },
    ) as client:
        page = client.get("/dashboard/roles").text
        assert "nobody waits here" in page or "No role waits in" in page


def test_two_roles_behind_one_door_is_shown_and_refused(tmp_path):
    """A state that names two roles cannot say which is due. The old shape broke the
    tie with `runs_profile`; the new one calls it a contradiction."""
    cfg = Settings(
        sdlc_roles={
            "ba": SdlcRole(stages=["analyze"], waits_in="Ready for Analysis"),
            "full": SdlcRole(stages=["analyze", "implement"], waits_in="Ready for Analysis"),
        },
    )
    assert sdlc_plan.profile_for_state("Ready for Analysis", cfg) == ""

    with _client(tmp_path, sdlc_roles=cfg.sdlc_roles) as client:
        page = client.get("/dashboard/roles").text
        assert "two roles wait in this state" in page


def test_saving_writes_the_roles_and_applies_them_live(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    with _client(tmp_path, trigger_states=["New", "Ready for Testing"]) as client:
        r = client.post("/dashboard/roles", data={
            "role_qc_stages": ["test"],
            "role_qc_waits": "Ready for Testing",
            "role_qc_shows": "In Testing",
            "role_qc_tag": "",
            "role_qc_done": "",
            "role_qc_done_tag": "",
            # no role_qc_auto → it waits for a person
            "role_dev_stages": ["implement", "review", "pr"],
            "role_dev_waits": "Ready for Development",
            "role_dev_done": "Ready for Testing",
            "role_dev_auto": "on",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)

        cfg = client.app.state.container.config
        roles = cfg.sdlc_roles
        assert set(roles) == {"qc", "dev"}
        assert roles["qc"].shows == "In Testing"
        assert roles["dev"].auto is True
        assert roles["dev"].stages == ["implement", "review", "pr"]

        # The dial does what it says: auto adds its door, not-auto removes it.
        assert "Ready for Development" in cfg.effective_trigger_states
        assert "Ready for Testing" not in cfg.effective_trigger_states
        assert "New" in cfg.effective_trigger_states

        # And the chain is now readable from one key.
        assert sdlc_plan.handoff_state("dev", cfg) == "Ready for Testing"
        assert sdlc_plan.profile_for_state("Ready for Testing", cfg) == "qc"

        saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert saved["sdlc_roles"]["qc"]["waits_in"] == "Ready for Testing"


def test_clearing_falls_back_to_trigger_states(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    with _client(tmp_path, trigger_states=["New", "Ready for Testing"],
                 sdlc_roles={
                     "qc": SdlcRole(stages=["test"], waits_in="Ready for Testing", auto=False),
                 }) as client:
        cfg = client.app.state.container.config
        assert "Ready for Testing" not in cfg.effective_trigger_states
        client.post("/dashboard/roles", data={"reset": "1"}, follow_redirects=False)
        assert cfg.sdlc_roles == {}
        assert cfg.effective_trigger_states == ["New", "Ready for Testing"]


def test_the_old_relay_url_still_lands(tmp_path):
    with _client(tmp_path) as client:
        r = client.get("/dashboard/relay", follow_redirects=False)
        assert r.status_code == 301
        assert r.headers["location"] == "/dashboard/roles"


# ── Back-compat: an install that predates the Roles page must not change behaviour ──

def test_roles_are_derived_from_the_deprecated_keys_when_unset():
    cfg = Settings(
        trigger_states=["New", "Ready for Development", "Ready for Testing"],
        sdlc_stage_wiring={
            "implement": SdlcStageWiring(
                queue_state="Ready for Development", working_state="In Development", auto=True
            ),
            "test": SdlcStageWiring(queue_state="Ready for Testing", auto=False),
        },
        sdlc_profile_states={"dev": "Ready for Testing"},
        sdlc_profile_tags={"dev": "handoff-qc"},
    )
    roles = sdlc_plan.effective_roles(cfg)
    assert roles["dev"].waits_in == "Ready for Development"
    assert roles["dev"].shows == "In Development"
    assert roles["dev"].auto is True
    assert roles["dev"].done == "Ready for Testing"
    assert roles["dev"].done_tag == "handoff-qc"
    assert roles["qc"].waits_in == "Ready for Testing" and roles["qc"].auto is False
    # And the derived shape drives the same pickup set the old code produced.
    assert "Ready for Development" in cfg.effective_trigger_states
    assert "Ready for Testing" not in cfg.effective_trigger_states


def test_a_stage_door_owned_by_one_profile_stays_owned_when_derived():
    """`runs_profile` said "this door starts full, not ba". Deriving must not hand the
    same door to both, or an install would silently start running a different role."""
    cfg = Settings(
        sdlc_stage_wiring={
            "analyze": SdlcStageWiring(queue_state="Ready for Analysis", runs_profile="full"),
        },
    )
    roles = sdlc_plan.effective_roles(cfg)
    assert roles["full"].waits_in == "Ready for Analysis"
    assert roles["ba"].waits_in == ""          # ba never had this door
    assert sdlc_plan.profile_for_state("Ready for Analysis", cfg) == "full"


def test_sdlc_roles_wins_over_the_deprecated_keys():
    cfg = Settings(
        sdlc_roles={"dev": SdlcRole(stages=["implement"], waits_in="Doing")},
        sdlc_stage_wiring={"test": SdlcStageWiring(queue_state="Ready for Testing", auto=True)},
    )
    roles = sdlc_plan.effective_roles(cfg)
    assert set(roles) == {"dev"}
    assert "Ready for Testing" not in cfg.effective_trigger_states
