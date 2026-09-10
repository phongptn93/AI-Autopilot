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


def test_a_non_auto_door_that_is_a_trigger_state_is_warned_about_on_its_own_row(tmp_path):
    """The trap, where it is set: a role's autonomy wins over Trigger states, so leaving
    the box off REMOVES that state from the poll query while Settings still lists it.
    The operator who unticks it does not expect the autopilot to stop taking that work,
    so the row has to say it."""
    with _client(
        tmp_path,
        trigger_states=["New", "Active"],
        sdlc_roles={"dev": SdlcRole(stages=["implement"], waits_in="Active", auto=False)},
    ) as client:
        page = client.get("/dashboard/roles").text
        assert "removes it from the poll query" in page
        assert "Trigger state" in page


def test_an_auto_door_on_a_trigger_state_is_stated_without_alarm(tmp_path):
    """`auto` ADDS the door, so the pages agree — say so, but do not cry wolf."""
    with _client(
        tmp_path,
        trigger_states=["New", "Active"],
        sdlc_roles={"dev": SdlcRole(stages=["implement"], waits_in="Active", auto=True)},
    ) as client:
        page = client.get("/dashboard/roles").text
        assert "this role owns it either way" in page
        assert "removes it from the poll query" not in page


def test_a_door_of_its_own_gets_no_trigger_warning(tmp_path):
    """Wiring a role to a state nobody triggers on removes nothing."""
    with _client(
        tmp_path,
        trigger_states=["New"],
        sdlc_roles={"dev": SdlcRole(stages=["implement"], waits_in="Ready for Dev")},
    ) as client:
        page = client.get("/dashboard/roles").text
        # Neither half of the per-row notice fires. Asserted on the exact wording, not
        # on "Trigger state": the page explains that phrase in its static help text.
        assert "removes it from the poll query" not in page
        assert "this role owns it either way" not in page


def test_the_when_done_placeholder_names_the_state_a_blank_field_really_sets(tmp_path):
    """Every other placeholder on this page states its real fallback; this one claimed
    a blank field means "stop and wait for a person". It does not — handoff_state falls
    back to resolved_state, so the item is moved to Resolved. The hint below the field
    already knew that; the placeholder people read while typing did not."""
    with _client(
        tmp_path, resolved_state="Resolved",
        sdlc_roles={"dev": SdlcRole(stages=["implement"], waits_in="Ready for Dev")},
    ) as client:
        page = client.get("/dashboard/roles").text
        assert "falls back to Resolved" in page
        assert "blank = stop and wait for a person" not in page


def test_it_says_stop_only_when_there_really_is_no_fallback(tmp_path):
    with _client(
        tmp_path, resolved_state="",
        sdlc_roles={"dev": SdlcRole(stages=["implement"], waits_in="Ready for Dev")},
    ) as client:
        assert "blank = stop and wait for a person" in client.get("/dashboard/roles").text


def test_the_page_says_where_the_shared_run_now_tag_is_changed(tmp_path):
    """The page named the fallback in a placeholder and owned no way to change it —
    stage_entry_tag had no field on any screen, so the only way to edit it was by hand
    in YAML. It has a field now; this page points at it."""
    with _client(
        tmp_path, stage_entry_tag="autopilot-run",
        sdlc_roles={"dev": SdlcRole(stages=["implement"], waits_in="Ready for Dev")},
    ) as client:
        page = client.get("/dashboard/roles").text
        assert "falls back to autopilot-run" in page
        assert "/dashboard/settings" in page and "shared" in page


def test_it_does_not_promise_a_shared_fallback_that_is_not_set(tmp_path):
    with _client(
        tmp_path, stage_entry_tag="",
        sdlc_roles={"dev": SdlcRole(stages=["implement"], waits_in="Ready for Dev")},
    ) as client:
        page = client.get("/dashboard/roles").text
        assert "No shared fallback is set" in page
        assert "falls back to nothing" in page


def test_a_role_run_now_tag_equal_to_the_trigger_tag_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    """The same destruction as the shared tag, reached through a different page: the
    sweep consumes the tag it matched on, so this would strip the trigger tag off every
    item the autopilot owns. Settings refuses it; this door has to as well."""
    with _client(tmp_path, trigger_tag="vm-claude-autopilot") as client:
        resp = client.post(
            "/dashboard/roles",
            data={"role_dev_waits": "Ready for Dev", "role_dev_stages": "implement",
                  "role_dev_tag": "vm-claude-autopilot"},
            follow_redirects=False,
        )
        assert resp.cookies["autopilot_flash"] == "err_role_tag_clash"
        assert not client.app.state.container.config.sdlc_roles   # nothing applied


def test_a_role_run_now_tag_of_its_own_saves(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    with _client(tmp_path, trigger_tag="vm-claude-autopilot") as client:
        resp = client.post(
            "/dashboard/roles",
            data={"role_dev_waits": "Ready for Dev", "role_dev_stages": "implement",
                  "role_dev_tag": "vm-claude-autopilot-run-dev"},
            follow_redirects=False,
        )
        assert resp.cookies["autopilot_flash"] == "roles_saved"
        saved = client.app.state.container.config.sdlc_roles["dev"]
        assert saved.entry_tag == "vm-claude-autopilot-run-dev"


def test_a_role_can_wait_in_more_than_one_state(tmp_path):
    """One door was too few for a real board: a developer takes new work from
    "Ready for Development" AND takes rejected work back from "Rework Required" — same
    person, same stages, two queues. The second queue had no owner at all."""
    from ai_autopilot.execution.sdlc_plan import profile_for_state, role_doors

    cfg = Settings(sdlc_roles={"dev": SdlcRole(
        stages=["implement"], waits_in="Ready for Development, Rework Required",
        auto=True,
    )})
    assert role_doors(cfg.sdlc_roles["dev"]) == ["Ready for Development", "Rework Required"]
    assert profile_for_state("Rework Required", cfg) == "dev"
    assert profile_for_state("Ready for Development", cfg) == "dev"
    # Both doors amend the poll query, so both queues are actually swept.
    polled = {s.lower() for s in cfg.effective_trigger_states}
    assert {"ready for development", "rework required"} <= polled


def test_both_doors_are_shown_on_the_row(tmp_path):
    with _client(
        tmp_path,
        sdlc_roles={"dev": SdlcRole(stages=["implement"],
                                    waits_in="Ready for Development, Rework Required",
                                    auto=True)},
    ) as client:
        page = client.get("/dashboard/roles").text
        assert "Ready for Development, Rework Required" in page


def test_two_roles_clashing_on_the_second_door_is_still_caught(tmp_path):
    """A role with two queues can collide on either — checking only the first would
    have let the state that cannot say who is due through."""
    from ai_autopilot.execution import sdlc_plan

    cfg = Settings(sdlc_roles={
        "dev": SdlcRole(stages=["implement"], waits_in="Ready for Development, Rework Required"),
        "qc": SdlcRole(stages=["test"], waits_in="Rework Required"),
    })
    assert sdlc_plan.profile_for_state("Rework Required", cfg) == ""   # refused, not guessed
