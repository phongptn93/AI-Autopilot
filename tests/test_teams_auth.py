"""/api/messages only answers Bot Service, for our own tenant.

The adapter falls back to an anonymous identity when no claims are set, so before this
gate a forged activity with ``deliveryMode: expectReplies`` got its reply — ADO data
included — straight back in the HTTP response.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

pytest.importorskip("microsoft_agents.hosting.fastapi")

from microsoft_agents.hosting.core.authorization.jwt import JwtTokenValidator  # noqa: E402

from ai_autopilot.app import create_app  # noqa: E402
from ai_autopilot.config import Settings  # noqa: E402
from ai_autopilot.teams_agent import activity_tenant  # noqa: E402

TENANT = "11111111-2222-3333-4444-555555555555"


def _activity(tenant: str | None = TENANT) -> dict:
    act = {"type": "message", "text": "/item 1", "deliveryMode": "expectReplies",
           "conversation": {"id": "c1"}, "serviceUrl": "https://smba.trafficmanager.net/x/"}
    if tenant is not None:
        act["conversation"]["tenantId"] = tenant
    return act


@pytest.fixture
def client(tmp_path, monkeypatch):
    dispatched: list = []

    async def fake_start(request, agent, adapter):
        dispatched.append(await request.json())

    import microsoft_agents.hosting.fastapi as mf
    monkeypatch.setattr(mf, "start_agent_process", fake_start)
    settings = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
                        teams_agent_app_id="app-id", teams_agent_app_secret="s",
                        teams_agent_tenant_id=TENANT)
    with TestClient(create_app(settings)) as c:
        c.app.state.teams_agent = object()   # pretend the bot is configured
        c.app.state.teams_adapter = object()
        c.dispatched = dispatched
        yield c


def _valid_token(monkeypatch):
    async def ok(self, token):
        assert token == "good"
        return object()
    monkeypatch.setattr(JwtTokenValidator, "validate_token", ok)


def test_no_token_is_refused(client):
    assert client.post("/api/messages", json=_activity()).status_code == 401
    assert client.dispatched == []


def test_a_forged_token_is_refused(client):
    resp = client.post("/api/messages", json=_activity(),
                       headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401 and client.dispatched == []


def test_a_valid_token_from_another_tenant_is_refused(client, monkeypatch):
    _valid_token(monkeypatch)
    for act in (_activity("99999999-0000-0000-0000-000000000000"), _activity(None)):
        resp = client.post("/api/messages", json=act, headers={"Authorization": "Bearer good"})
        assert resp.status_code == 403
    assert client.dispatched == []


def test_a_valid_token_from_our_tenant_reaches_the_bot(client, monkeypatch):
    _valid_token(monkeypatch)
    resp = client.post("/api/messages", json=_activity(TENANT.upper()),
                       headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200 and len(client.dispatched) == 1


def test_activity_tenant_reads_either_place():
    assert activity_tenant({"conversation": {"tenantId": "a"}}) == "a"
    assert activity_tenant({"channelData": {"tenant": {"id": "b"}}}) == "b"
    assert activity_tenant({}) == "" and activity_tenant("junk") == ""
