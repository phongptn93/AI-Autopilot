"""The "Chính sách" scope on the Settings page.

It re-asks a curated set of settings as the decisions they encode ("Có cho phép … không?")
without introducing a second input for any of them. Two ways for that to rot, and each
test below guards one: a question pointing at a key that is no longer a field (the page
would count a box it cannot show), and a re-labelled input that no longer saves (the
scope exists to make the decision easier to take, not to take it somewhere else).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard import settings_form


@pytest.fixture
def own_config(tmp_path, monkeypatch):
    # Saving writes through config_file_path(); keep it inside this test.
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))


def _settings(tmp_path) -> Settings:
    return Settings(
        dry_run=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'policy.db'}",
        dashboard_auth_password_hash="", dashboard_auth_token="",
    )


def test_every_policy_question_points_at_a_real_field():
    fields = {f.key for f in settings_form.FIELDS}
    keys = [q.key for q in settings_form.POLICY_QUESTIONS]
    assert 15 <= len(keys) <= 25
    assert len(keys) == len(set(keys)), "a setting asked twice"
    for q in settings_form.POLICY_QUESTIONS:
        assert q.key in fields, q.key
        assert q.key in Settings.model_fields, q.key
        assert q.question.strip().endswith("?"), q.key
        assert q.why.strip(), q.key


def test_the_page_offers_the_policy_scope_with_its_questions(tmp_path, own_config):
    with TestClient(create_app(_settings(tmp_path))) as client:
        html = client.get("/dashboard/settings").text
    n = len(settings_form.POLICY_QUESTIONS)
    assert f'data-scope="policy">Chính sách ({n})</button>' in html
    for q in settings_form.POLICY_QUESTIONS:
        assert f'<span class="pol-q">{q.question}</span>' in html, q.key
    # The question is a second label on the SAME input: the field keeps its key, so the
    # scope filter and the save path see nothing new.
    first = settings_form.POLICY_QUESTIONS[0]
    assert f'data-k="{first.key}"' in html and f'name="{first.key}"' in html
    assert '"policy": [' in html.replace(" ", "") or "policy: [" in html


def test_saving_a_policy_field_uses_the_normal_post(tmp_path, own_config):
    with TestClient(create_app(_settings(tmp_path))) as client:
        live = client.app.state.container.config
        assert live.test_gate_enabled is False
        client.post("/dashboard/settings", data={
            "dry_run": "on",
            "test_gate_enabled": "on",
            "autonomy_level": "report",
            "max_concurrent": "2",
        })
        assert live.test_gate_enabled is True
        assert live.autonomy_level == "report"
        assert live.max_concurrent == 2
