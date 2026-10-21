"""The work-item type → state map must not make a page wait on 1 + N round trips.

Settings, Board and Flow render through ``_type_state_map``. It used to walk the N
per-type state requests one after another on every cache miss, retry a failing ADO on
every render, and let pages opened together each start their own walk.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from ai_autopilot.ado import client as client_mod
from ai_autopilot.ado.client import AdoClient
from ai_autopilot.config import Settings

TYPES = [f"T{i}" for i in range(18)]          # a stock template: 1 + 18 requests


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _SlowAdo:
    """Answers like ADO, ``delay`` seconds per request, and records the overlap."""

    def __init__(self, delay=0.05, fail=False):
        self.delay, self.fail = delay, fail
        self.calls = 0
        self.in_flight = 0
        self.peak = 0

    async def get(self, url, headers=None):
        self.calls += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if self.fail:
                raise httpx.ConnectTimeout("slow ADO")
            if url.split("?")[0].endswith("/workitemtypes"):
                return _Resp({"value": [{"name": t} for t in TYPES]})
            name = url.split("/workitemtypes/")[1].split("/")[0]
            return _Resp({"value": [{"name": f"{name}-New", "order": 1,
                                     "stateCategory": "Proposed"}]})
        finally:
            self.in_flight -= 1


class _NoAuth:
    async def get_auth_header(self):
        return {}


def _client(http) -> AdoClient:
    cfg = Settings(ado_organization="https://dev.azure.com/org", ado_project="P")
    return AdoClient(http=http, auth=_NoAuth(), config=cfg)


async def test_state_requests_go_out_concurrently():
    http = _SlowAdo(delay=0.05)
    c = _client(http)
    started = time.monotonic()
    states = await c._type_state_map()
    elapsed = time.monotonic() - started
    assert set(states) == set(TYPES) and http.calls == 1 + len(TYPES)
    assert http.peak == client_mod._TYPE_STATE_CONCURRENCY            # bounded, not 18
    # Serially this is 19 × 50 ms ≈ 0.95 s; concurrently ≈ 1 + ceil(18 / 8) = 4 trips.
    assert elapsed < 0.5, elapsed


async def test_pages_opened_together_share_one_walk():
    http = _SlowAdo(delay=0.02)
    c = _client(http)
    results = await asyncio.gather(c.get_states(), c.get_states_by_type(), c.get_states())
    assert http.calls == 1 + len(TYPES)                               # not three walks
    assert results[0] == results[2] and set(results[1]) == set(TYPES)


async def test_an_expired_entry_is_served_at_once_and_refreshed_in_the_background(
    monkeypatch,
):
    http = _SlowAdo(delay=0.05)
    c = _client(http)
    first = await c._type_state_map()
    calls = http.calls
    stamp, value = c._type_states["p"]
    c._type_states["p"] = (stamp - client_mod._TYPE_STATE_TTL_SECONDS - 1, value)
    started = time.monotonic()
    again = await c._type_state_map()
    assert again == first and time.monotonic() - started < 0.02      # no wait on ADO
    task = c._type_state_refresh["p"]
    await c._type_state_map()                       # a second reader does not start another
    assert c._type_state_refresh.get("p") is task
    await task
    assert http.calls == calls * 2                                    # exactly one refresh
    assert time.monotonic() - c._type_states["p"][0] < 1              # and it is fresh now


async def test_a_failing_ado_costs_one_render_not_every_render():
    http = _SlowAdo(delay=0.01, fail=True)
    c = _client(http)
    assert await c.get_states() == []
    calls = http.calls
    assert await c.get_states() == [] and await c.get_states_by_type() == {}
    assert http.calls == calls                                        # backed off
    c._type_states_failed["p"] -= client_mod._TYPE_STATE_FAIL_RETRY_SECONDS + 1
    await c.get_states()
    assert http.calls > calls                                         # and asks again later


async def test_a_failed_refresh_keeps_serving_the_last_good_map():
    http = _SlowAdo(delay=0.01)
    c = _client(http)
    good = await c._type_state_map()
    stamp, value = c._type_states["p"]
    c._type_states["p"] = (stamp - client_mod._TYPE_STATE_TTL_SECONDS - 1, value)
    http.fail = True
    assert await c._type_state_map() == good
    await c._type_state_refresh["p"]
    assert await c._type_state_map() == good          # stale beats empty


async def test_refresh_clears_the_failure_memory():
    c = _client(_SlowAdo(delay=0.01, fail=True))
    await c.get_states()
    assert c._type_states_failed
    c.refresh()
    assert not c._type_states_failed
