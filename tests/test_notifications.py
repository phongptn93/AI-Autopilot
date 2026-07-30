"""Tests for the Teams notification channel — multi-webhook fan-out."""

from __future__ import annotations

import httpx

from ai_autopilot.config import Settings
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.notifications.base import NotificationMessage, NotificationType
from ai_autopilot.notifications.teams import TeamsNotifier, _host

_A = "https://a.example/workflows/1?sig=aaa"
_B = "https://b.example/workflows/2?sig=bbb"


class _FakeHttp:
    """Records posts; ``fail_for`` hosts raise, ``status_for`` returns an error status."""

    def __init__(self, *, fail_for: set[str] | None = None, status_for: dict | None = None):
        self.posts: list[tuple[str, dict]] = []
        self._fail = fail_for or set()
        self._status = status_for or {}

    async def post(self, url, json=None):
        self.posts.append((url, json))
        if url in self._fail:
            raise httpx.ConnectError("boom")
        return httpx.Response(self._status.get(url, 200), text="ok")


def _message() -> NotificationMessage:
    return NotificationMessage(
        work_item=WorkItemInfo(id=42, title="Thing", work_item_type="Task"),
        type=NotificationType.COMPLETED,
        result=ExecutionResult.ok(42, "prompt", "out"),
    )


def _notifier(http, **overrides) -> TeamsNotifier:
    return TeamsNotifier(Settings(**overrides), http)


async def test_posts_to_every_configured_webhook():
    """The point of the feature: one autopilot reporting into several channels."""
    http = _FakeHttp()
    await _notifier(http, teams_webhook_url=_A, teams_webhook_urls=[_B]).send(_message())
    assert [u for u, _ in http.posts] == [_A, _B]
    # Same card everywhere — built once, not re-rendered per channel.
    assert http.posts[0][1] == http.posts[1][1]


async def test_list_alone_is_enough():
    """teams_webhook_url may stay empty and only the list be filled."""
    http = _FakeHttp()
    n = _notifier(http, teams_webhook_url="", teams_webhook_urls=[_A, _B])
    assert n.is_enabled is True
    await n.send(_message())
    assert [u for u, _ in http.posts] == [_A, _B]


async def test_duplicate_url_is_not_notified_twice():
    http = _FakeHttp()
    await _notifier(http, teams_webhook_url=_A, teams_webhook_urls=[_A, "  ", ""]).send(_message())
    assert [u for u, _ in http.posts] == [_A]


async def test_one_dead_channel_does_not_block_the_others():
    """Adding a second channel must not make notifications LESS reliable — a revoked
    Workflows URL or a deleted channel must not swallow the remaining posts."""
    http = _FakeHttp(fail_for={_A})
    await _notifier(http, teams_webhook_url=_A, teams_webhook_urls=[_B]).send(_message())
    assert [u for u, _ in http.posts] == [_A, _B]      # B still got it


async def test_http_error_status_does_not_block_the_others():
    http = _FakeHttp(status_for={_A: 403})
    await _notifier(http, teams_webhook_url=_A, teams_webhook_urls=[_B]).send(_message())
    assert [u for u, _ in http.posts] == [_A, _B]


async def test_disabled_when_nothing_configured():
    http = _FakeHttp()
    n = _notifier(http, teams_webhook_url="", teams_webhook_urls=[])
    assert n.is_enabled is False
    await n.send(_message())
    assert http.posts == []


def test_only_the_host_is_logged_never_the_token():
    """A Workflows URL's query string carries the token that authorises posting to the
    channel, so it must never reach the log."""
    assert _host(_A) == "a.example"
    assert "sig" not in _host(_A)
    assert _host("not a url") == "?"


def test_webhooks_property_keeps_order_and_drops_blanks():
    s = Settings(teams_webhook_url=" ", teams_webhook_urls=[_B, "", _A, _B])
    assert s.teams_webhooks == [_B, _A]
