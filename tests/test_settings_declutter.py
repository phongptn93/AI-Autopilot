"""Settings shows no empty boxes for things nobody should start using."""

from __future__ import annotations

from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings


def _page(tmp_path, monkeypatch, states=None, **over):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "c.yaml"))
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'd.db'}", **over)
    with TestClient(create_app(cfg)) as client:
        if states is not None:
            async def _states():
                return states
            client.app.state.container.ado.get_states = _states
            client.app.state.container.ado.get_states_by_type = None
        return client.get("/dashboard/settings").text


def test_the_legacy_rollup_box_is_gone_when_empty(tmp_path, monkeypatch):
    assert 'name="parent_rollup_map"' not in _page(tmp_path, monkeypatch)


def test_a_configured_legacy_rollup_stays_editable(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, parent_rollup_map=["Active = Active"])
    assert 'name="parent_rollup_map"' in page and "Active = Active" in page


def test_saving_the_page_does_not_touch_a_hidden_empty_rollup(tmp_path, monkeypatch):
    from ai_autopilot.dashboard import settings_form

    assert settings_form.parse_form({})["parent_rollup_map"] == []


def test_manual_states_fold_into_a_link_when_real_states_are_listed(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, trigger_states=["Active"])
    assert 'class="more-states"' in page and "＋ Thêm state khác" in page
    assert 'name="trigger_states__manual"' in page          # still submitted
