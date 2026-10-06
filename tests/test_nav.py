"""The sidebar is data — every entry must lead somewhere real."""

from __future__ import annotations

from html import escape as html_escape

import pytest
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard import nav


def test_keys_and_links_are_unique():
    items = [i for g in nav.GROUPS for i in g.items]
    assert len({i.key for i in items}) == len(items)
    assert len({i.href for i in items}) == len(items)
    assert all(i.hint and i.label for i in items)


def test_fleet_only_appears_on_a_fleet_machine():
    def keys(role):
        return {i.key for g in nav.groups(role) for i in g["items"]}

    assert "fleet" not in keys("")
    assert "fleet" in keys("central") and "fleet" in keys("worker")


@pytest.fixture
def client(tmp_path):
    cfg = Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'n.db'}",
                   fleet_role="central", fleet_token="t")
    with TestClient(create_app(cfg)) as c:
        yield c


def test_every_menu_link_opens_a_page(client):
    for group in nav.groups("central"):
        for item in group["items"]:
            resp = client.get(item.href)
            assert resp.status_code == 200, (item.href, resp.status_code)
            assert 'class="nav-link active"' in resp.text or item.key in (
                "overview",), item.href


def test_the_menu_is_grouped_and_described(client):
    html = client.get("/dashboard").text
    for group in nav.GROUPS:
        assert html_escape(group.label, quote=False) in html
    assert 'title="Hôm nay autopilot làm được gì' in html
