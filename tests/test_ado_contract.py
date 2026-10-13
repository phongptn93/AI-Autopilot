"""AdoClient against a fake ADO server: auth, throttling, and what may be replayed.

These are contract tests — every request goes through a real ``httpx.AsyncClient``
with a ``MockTransport`` standing in for dev.azure.com, so the URL, headers and body
the client actually sends are what gets asserted, not a mocked method call.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ai_autopilot.ado import auth as auth_mod
from ai_autopilot.ado import client as client_mod
from ai_autopilot.ado.auth import AdoAuthService, TokenInfo
from ai_autopilot.ado.client import AdoClient
from ai_autopilot.config import BOT_COMMENT_PREFIX, Settings

ORG = "https://dev.azure.com/org"


class FakeAdo:
    """Records every request; answers from a queue of responses per (method, path)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: dict[tuple[str, str], list[httpx.Response]] = {}

    def on(self, method: str, path_part: str, *responses: httpx.Response) -> None:
        self._routes.setdefault((method, path_part), []).extend(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for (method, part), queue in self._routes.items():
            if request.method == method and part in request.url.path:
                # The last response repeats once the queue is drained.
                return queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(404, json={"message": "no fake route"})

    def sent(self, method: str, path_part: str = "") -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and path_part in r.url.path]


@pytest.fixture
def no_sleep(monkeypatch):
    """Record backoff delays instead of waiting them out."""
    delays: list[float] = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)
    return delays


def _client(fake: FakeAdo, **over) -> AdoClient:
    cfg = Settings(ado_organization=ORG, ado_project="Proj", ado_pat="secret-pat", **over)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return AdoClient(http=http, auth=AdoAuthService(cfg), config=cfg)


def _wi(item_id: int, *, tags: str = "", rev: int = 1, state: str = "New") -> dict:
    return {"id": item_id, "rev": rev, "fields": {
        "System.Id": item_id, "System.Title": f"Item {item_id}", "System.TeamProject": "Proj",
        "System.WorkItemType": "Task", "System.State": state, "System.Tags": tags}}


# ── Auth ────────────────────────────────────────────────────────────────────

async def test_pat_is_sent_as_basic_auth_on_every_call():
    fake = FakeAdo()
    fake.on("POST", "/wit/wiql", httpx.Response(200, json={"workItems": [{"id": 7}]}))
    fake.on("GET", "/wit/workitems", httpx.Response(200, json={"value": [_wi(7)]}))

    items = await _client(fake).get_pending_work_items()

    assert [i.id for i in items] == [7]
    want = "Basic " + base64.b64encode(b":secret-pat").decode()
    assert [r.headers["Authorization"] for r in fake.requests] == [want, want]


async def test_expired_oauth_token_is_refreshed_and_saved(monkeypatch, tmp_path):
    cfg = Settings(ado_organization=ORG, ado_pat="", oauth_app_id="app",
                   oauth_app_secret="assertion")
    svc = AdoAuthService(cfg)
    svc._token_file = tmp_path / "token.json"
    svc._token_file.write_text(TokenInfo(
        "old-access", "old-refresh", datetime.now(UTC) - timedelta(minutes=1)).to_json())

    seen: list[httpx.Request] = []

    def token_server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": "new-access",
                                         "refresh_token": "new-refresh", "expires_in": 3600})

    real = httpx.AsyncClient
    monkeypatch.setattr(auth_mod.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(token_server)))

    assert await svc.get_auth_header() == {"Authorization": "Bearer new-access"}
    assert b"assertion=old-refresh" in seen[0].content
    saved = TokenInfo.from_json(svc._token_file.read_text())
    assert (saved.access_token, saved.refresh_token) == ("new-access", "new-refresh")


async def test_no_pat_and_no_oauth_app_is_a_clear_error(tmp_path):
    svc = AdoAuthService(Settings(ado_organization=ORG, ado_pat="", oauth_app_id="",
                                  oauth_app_secret=""))
    svc._token_file = tmp_path / "absent.json"
    with pytest.raises(RuntimeError, match="AUTOPILOT_ADO_PAT"):
        await svc.get_auth_header()


# ── Reads: throttling and failures ──────────────────────────────────────────

async def test_429_honours_retry_after_then_succeeds(no_sleep):
    fake = FakeAdo()
    fake.on("POST", "/wit/wiql",
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json={"workItems": []}))

    assert await _client(fake).get_pending_work_items() == []
    assert no_sleep == [3.0]
    assert len(fake.sent("POST", "/wit/wiql")) == 2


async def test_an_absurd_retry_after_is_capped(no_sleep):
    fake = FakeAdo()
    fake.on("POST", "/wit/wiql",
            httpx.Response(429, headers={"Retry-After": "3600"}),
            httpx.Response(200, json={"workItems": []}))

    await _client(fake).get_pending_work_items()
    assert no_sleep == [client_mod._MAX_RETRY_AFTER_SECONDS]


async def test_persistent_5xx_gives_up_after_the_retry_budget(no_sleep):
    fake = FakeAdo()
    fake.on("POST", "/wit/wiql", httpx.Response(503))

    assert await _client(fake).get_pending_work_items() == []
    assert len(fake.sent("POST", "/wit/wiql")) == 3  # first try + 2 retries
    assert no_sleep == [1, 2]                       # exponential, no Retry-After


async def test_an_html_sign_in_page_is_not_read_as_an_empty_board():
    """An expired PAT gets a 203 + HTML sign-in page, not a 401."""
    fake = FakeAdo()
    fake.on("POST", "/wit/wiql", httpx.Response(203, text="<html>Sign in</html>"))

    assert await _client(fake).get_pending_work_items() == []
    assert fake.sent("GET") == []  # did not go on to fetch "the" items


# ── Mutations are never replayed ────────────────────────────────────────────

async def test_add_comment_is_not_retried_on_503(no_sleep):
    fake = FakeAdo()
    fake.on("POST", "/comments", httpx.Response(503))

    assert await _client(fake).add_comment(5, "hello") is False
    assert len(fake.sent("POST", "/comments")) == 1
    assert no_sleep == []


async def test_add_comment_carries_the_bot_prefix():
    fake = FakeAdo()
    fake.on("POST", "/comments", httpx.Response(200, json={}))

    assert await _client(fake).add_comment(5, "hello") is True
    body = json.loads(fake.sent("POST", "/comments")[0].content)
    assert body == {"text": BOT_COMMENT_PREFIX + "hello"}


async def test_update_state_is_one_json_patch_and_not_retried(no_sleep):
    fake = FakeAdo()
    fake.on("PATCH", "/wit/workitems/5", httpx.Response(503))

    assert await _client(fake).update_state(5, "Active") is False
    [req] = fake.sent("PATCH")
    assert req.headers["Content-Type"] == "application/json-patch+json"
    assert json.loads(req.content) == [
        {"op": "replace", "path": "/fields/System.State", "value": "Active"}]


# ── Tags: optimistic concurrency ────────────────────────────────────────────

async def test_add_tag_guards_on_rev_and_retries_a_lost_race():
    fake = FakeAdo()
    fake.on("GET", "/wit/workitems/5",
            httpx.Response(200, json=_wi(5, tags="a", rev=3)),
            httpx.Response(200, json=_wi(5, tags="a; b", rev=4)))
    fake.on("PATCH", "/wit/workitems/5", httpx.Response(412), httpx.Response(200, json={}))

    assert await _client(fake).add_tag(5, "autopilot") is True

    first, second = (json.loads(r.content) for r in fake.sent("PATCH"))
    assert first[0] == {"op": "test", "path": "/rev", "value": 3}
    # The retry is rebuilt from the re-read: the concurrent "b" survives.
    assert second == [{"op": "test", "path": "/rev", "value": 4},
                      {"op": "replace", "path": "/fields/System.Tags",
                       "value": "a; b; autopilot"}]


async def test_add_tag_already_present_sends_no_patch():
    fake = FakeAdo()
    fake.on("GET", "/wit/workitems/5", httpx.Response(200, json=_wi(5, tags="AutoPilot")))

    assert await _client(fake).add_tag(5, "autopilot") is True
    assert fake.sent("PATCH") == []


async def test_add_tag_gives_up_after_three_conflicts():
    fake = FakeAdo()
    fake.on("GET", "/wit/workitems/5", httpx.Response(200, json=_wi(5)))
    fake.on("PATCH", "/wit/workitems/5", httpx.Response(409))

    assert await _client(fake).add_tag(5, "x") is False
    assert len(fake.sent("PATCH")) == 3


async def test_remove_tag_is_case_insensitive():
    fake = FakeAdo()
    fake.on("GET", "/wit/workitems/5", httpx.Response(200, json=_wi(5, tags="Keep; AUTOPILOT")))
    fake.on("PATCH", "/wit/workitems/5", httpx.Response(200, json={}))

    assert await _client(fake).remove_tag(5, "autopilot") is True
    patch = json.loads(fake.sent("PATCH")[0].content)
    assert patch[1]["value"] == "Keep"
