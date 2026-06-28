"""End-to-end smoke tests for the FastAPI app (routes, dashboard, webhook).

Runs with no ADO auth, so the poller idles immediately and nothing touches the
network except the ADO health check (which fails gracefully → 503).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings


@pytest.fixture
def client(tmp_path) -> TestClient:
    settings = Settings(
        dry_run=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
    )
    with TestClient(create_app(settings)) as c:
        yield c


def test_metrics_endpoint(client: TestClient):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "autopilot_tasks_total" in resp.text


@pytest.mark.parametrize(
    "path",
    ["/dashboard", "/dashboard/history", "/dashboard/config", "/dashboard/capabilities"],
)
def test_dashboard_pages_render(client: TestClient, path: str):
    resp = client.get(path)
    assert resp.status_code == 200
    assert "AI Autopilot" in resp.text


def test_health_reports_checks(client: TestClient):
    resp = client.get("/health")
    body = resp.json()
    names = {c["name"] for c in body["checks"]}
    assert names == {"ado", "claude", "disk"}


def test_webhook_enqueues(client: TestClient):
    resp = client.post("/api/webhook/ado", json={"resource": {"workItemId": 123}})
    assert resp.status_code == 200
    assert resp.json() == {"queued": 123}


def test_webhook_rejects_missing_id(client: TestClient):
    resp = client.post("/api/webhook/ado", json={"resource": {}})
    assert resp.json() == {"error": "No workItemId in payload"}
