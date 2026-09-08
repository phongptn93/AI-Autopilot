"""The Relay page: one table that IS the chain.

Where a step waits, what it shows while running, and whether it self-starts used to
live on three settings pages keyed by three different things, so nothing on screen
showed they described the same step. That is how a QC hand-off became a trigger and
reworked finished work — so the page is tested for saying all three in one row.
"""

from __future__ import annotations

import yaml
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import SdlcStageWiring, Settings


def _client(tmp_path, **overrides):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", **overrides)
    return TestClient(create_app(settings))


def test_the_page_shows_each_stage_with_its_state_and_dial(tmp_path):
    with _client(
        tmp_path,
        trigger_states=["New", "Ready for Development", "Ready for Testing"],
        sdlc_stage_wiring={
            "implement": SdlcStageWiring(queue_state="Ready for Development", auto=True),
            "test": SdlcStageWiring(
                queue_state="Ready for Testing", working_state="In Testing", auto=False
            ),
        },
    ) as client:
        page = client.get("/dashboard/relay").text
        assert "In Testing" in page
        assert "stage_test_auto" in page and "stage_implement_auto" in page
        # Only a profile's FIRST stage owns a door, and the page says which.
        assert "opens qc" in page and "opens dev" in page
        # The derived pickup set is shown, so the dial's effect is visible where it is set.
        assert "Poller starts from" in page


def test_saving_writes_the_wiring_and_applies_it_live(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    with _client(tmp_path, trigger_states=["New", "Ready for Testing"]) as client:
        r = client.post("/dashboard/relay", data={
            "stage_test_queue": "Ready for Testing",
            "stage_test_working": "In Testing",
            "stage_test_tag": "",
            # no stage_test_auto → it waits for a person
            "stage_implement_queue": "Ready for Development",
            "stage_implement_auto": "on",
            "stage_analyze_queue": "",          # blank = not wired at all
            "stage_analyze_auto": "on",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)

        cfg = client.app.state.container.config
        wiring = cfg.sdlc_stage_wiring
        assert set(wiring) == {"test", "implement"}          # the blank row is not wired
        assert wiring["test"].working_state == "In Testing"
        assert wiring["implement"].auto is True

        # The dial does what it says: auto adds its state, not-auto removes it.
        assert "Ready for Development" in cfg.effective_trigger_states
        assert "Ready for Testing" not in cfg.effective_trigger_states
        assert "New" in cfg.effective_trigger_states

        saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert saved["sdlc_stage_wiring"]["test"]["queue_state"] == "Ready for Testing"


def test_clearing_falls_back_to_trigger_states(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    with _client(tmp_path, trigger_states=["New", "Ready for Testing"],
                 sdlc_stage_wiring={
                     "test": SdlcStageWiring(queue_state="Ready for Testing", auto=False),
                 }) as client:
        cfg = client.app.state.container.config
        assert "Ready for Testing" not in cfg.effective_trigger_states
        client.post("/dashboard/relay", data={"reset": "1"}, follow_redirects=False)
        assert cfg.sdlc_stage_wiring == {}
        assert cfg.effective_trigger_states == ["New", "Ready for Testing"]
