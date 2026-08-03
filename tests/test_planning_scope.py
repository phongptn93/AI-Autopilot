"""Planning workbench scoping: whose work the Load button fetches.

An empty Assignee box is a real choice — the whole team — so it must reach the ADO
query untouched instead of snapping back to this machine's own assignee.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings


class _RecordingAdo:
    """Captures the assignee the dashboard asked for; returns nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_work_items_by_assignee(self, assignee, states=None, types=None, top=200):
        self.calls.append(assignee)
        return []


@pytest.fixture
def planning(tmp_path):
    settings = Settings(
        dry_run=True,
        auto_transition_assignee="me@x.com",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
    )
    with TestClient(create_app(settings)) as client:
        ado = _RecordingAdo()
        client.app.state.container.ado = ado
        yield client, ado


def test_blank_assignee_loads_the_whole_team(planning):
    client, ado = planning
    client.get("/dashboard/planning?assignee=")
    assert ado.calls == [""]                      # not "me@x.com"


def test_blank_choice_survives_the_saved_filter(planning):
    client, ado = planning
    client.get("/dashboard/planning?assignee=")   # sets the filter cookie
    ado.calls.clear()
    client.get("/dashboard/planning")             # no params → restore from cookie
    assert ado.calls == [""]                      # still everyone, not the default


def test_first_visit_defaults_to_this_machines_assignee(planning):
    client, ado = planning
    client.get("/dashboard/planning")             # no params, no cookie yet
    assert ado.calls == ["me@x.com"]


def test_several_people_are_passed_through(planning):
    client, ado = planning
    client.get("/dashboard/planning?assignee=a%40x.com%2C+b%40x.com")
    assert ado.calls == ["a@x.com, b@x.com"]
