"""The dashboard is one router per area — each can be mounted and exercised alone."""

from __future__ import annotations

import importlib
import pkgutil
from types import SimpleNamespace

from fastapi import FastAPI
from starlette.testclient import TestClient

from ai_autopilot import dashboard
from ai_autopilot.config import Settings
from ai_autopilot.dashboard import routes
from ai_autopilot.dashboard.routes import auth


def test_every_area_module_is_mounted():
    """A new routes/x.py that nobody adds to _AREAS would serve nothing, silently."""
    found = {
        m.name for m in pkgutil.iter_modules(routes.__path__) if not m.name.startswith("_")
    }
    mounted = {a.__name__.rsplit(".", 1)[-1] for a in dashboard._AREAS}
    assert found == mounted


def test_each_area_builds_a_router_of_its_own():
    for name in (a.__name__ for a in dashboard._AREAS):
        router = importlib.import_module(name).create_router()
        assert router.routes, name


def test_an_area_can_be_served_without_the_rest_of_the_app():
    """The point of the split: the login page needs no container, no poller, no DB."""
    app = FastAPI()
    # Login only exists while a password is set; with none it answers 404 by design.
    app.state.container = SimpleNamespace(config=Settings(dashboard_auth_token="pw"))
    app.include_router(auth.create_router(), prefix="/dashboard")
    with TestClient(app) as client:
        resp = client.get("/dashboard/login")
    assert resp.status_code == 200
    assert 'name="password"' in resp.text
