"""SDLC starter presets, and the test gate after a steered (interactive) session."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ai_autopilot import sdlc_presets
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.execution import sdlc_plan


def test_every_preset_only_names_real_settings():
    fields = set(Settings.model_fields)
    for preset in sdlc_presets.PRESETS.values():
        assert set(preset.settings) <= fields, preset.key


def test_every_relay_step_runs_catalog_stages():
    for preset in sdlc_presets.PRESETS.values():
        for step in preset.chain:
            assert set(step.stages) <= set(sdlc_plan.CATALOG), step.name


def test_the_scrum_chain_hands_each_role_to_the_next():
    roles = sdlc_presets.roles_from_chain(sdlc_presets.PRESETS["scrum"])
    doors = {d.strip(): n for n, r in roles.items() for d in r["waits_in"].split(",") if d.strip()}
    for name, role in roles.items():
        if role["done"]:
            assert role["done"] in doors, f"{name} hands off into a state nobody waits in"
    assert roles["review"]["done"] == "" and roles["review"]["auto"] is False


def test_the_operators_state_names_win_even_blank():
    roles = sdlc_presets.roles_from_chain(
        sdlc_presets.PRESETS["scrum"], {"qc": {"waits_in": "Testing", "done": ""}},
    )
    assert roles["qc"] == {"stages": ["test"], "waits_in": "Testing", "done": "", "auto": True}


def test_a_chain_landing_in_a_trigger_state_is_refused():
    roles = sdlc_presets.roles_from_chain(sdlc_presets.PRESETS["scrum"])
    problems = sdlc_presets.chain_problems(roles, ["Ready for Test"])
    assert problems and "Ready for Test" in problems[0]


def test_two_roles_behind_one_door_are_refused():
    roles = sdlc_presets.roles_from_chain(
        sdlc_presets.PRESETS["scrum"], {"qc": {"waits_in": "Ready for Development", "done": ""}},
    )
    assert any("cửa vào" in p for p in sdlc_presets.chain_problems(roles, []))


def test_the_diff_lists_only_what_changes():
    cfg = Settings(autonomy_level="assisted", execution_mode="interactive")
    keys = {d["key"] for d in sdlc_presets.diff(sdlc_presets.PRESETS["autonomous"], cfg)}
    assert "execution_mode" in keys and "autonomy_level" not in keys


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "c.yaml"))
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'p.db'}",
                   trigger_states=["Proposed"])
    with TestClient(create_app(cfg)) as c:
        yield c


def test_the_roles_page_offers_the_presets(client):
    html = client.get("/dashboard/roles").text
    for p in sdlc_presets.PRESETS.values():
        assert p.title in html


def test_applying_the_relay_wires_roles_and_settings(client):
    resp = client.post("/dashboard/roles/preset", data={
        "preset": "scrum", "qc_waits": "Testing", "qc_done": "Ready for Review",
    }, follow_redirects=False)
    assert resp.status_code == 303
    cfg = client.app.state.container.config
    assert cfg.sdlc_roles["qc"].waits_in == "Testing"
    assert cfg.sdlc_roles["dev"].done == "Ready for Test"
    assert cfg.sdlc_max_iterations == 4


def test_an_invalid_chain_writes_nothing(client):
    client.post("/dashboard/roles/preset", data={
        "preset": "scrum", "dev_done": "Proposed",      # Proposed is a trigger state
    }, follow_redirects=False)
    assert client.app.state.container.config.sdlc_roles == {}


# ── the gate after an interactive session ───────────────────────────────────


def _poller(cfg):
    from ai_autopilot.services.poller import AdoPollerService

    p = AdoPollerService.__new__(AdoPollerService)
    p._config = cfg
    p._log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    return p


def _result(**kw):
    from ai_autopilot.models import ExecutionResult

    return ExecutionResult(work_item_id=1, skill_used="agent", success=True, **kw)


async def test_a_red_suite_holds_the_item_instead_of_handing_it_on(monkeypatch):
    from ai_autopilot.execution import test_gate
    from ai_autopilot.services import poller as poller_mod

    async def red(self, work_dir):
        return test_gate.TestResult(passed=False, ran=True, summary="2 failed",
                                    failures=["Inv.Tests.Totals <x>"])

    monkeypatch.setattr(poller_mod.TestGate, "run", red)
    result = _result()
    await _poller(Settings(sdlc_loop_enabled=True))._gate_interactive(
        SimpleNamespace(id=1), result, ".")
    assert result.needs_human and "2 failed" in result.error and "&lt;x&gt;" in result.error


async def test_the_gate_is_quiet_when_the_relay_is_off(monkeypatch):
    from ai_autopilot.services import poller as poller_mod

    async def boom(self, work_dir):
        raise AssertionError("gate must not run")

    monkeypatch.setattr(poller_mod.TestGate, "run", boom)
    result = _result()
    await _poller(Settings(sdlc_loop_enabled=False))._gate_interactive(
        SimpleNamespace(id=1), result, ".")
    await _poller(Settings(sdlc_loop_enabled=True, sdlc_interactive_gate=False))._gate_interactive(
        SimpleNamespace(id=1), result, ".")
    assert not result.needs_human
