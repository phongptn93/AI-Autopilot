"""Tests for the Teams notification channel — multi-webhook fan-out."""

from __future__ import annotations

import httpx

from ai_autopilot.config import Settings, WebhookTarget
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.notifications.base import NotificationMessage, NotificationType
from ai_autopilot.notifications.teams import TeamsNotifier

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


def test_the_token_never_reaches_the_log():
    """A Workflows URL's query string carries the token that authorises posting to the
    channel, so what identifies a channel in a log must never include it. Naming the
    channel is what makes "which one failed?" answerable — every Workflows URL in a tenant
    shares the same host, so the host alone was no help."""
    assert WebhookTarget(url=_A).label == "a.example"          # no name → host
    assert WebhookTarget(url=_A, name="#dev").label == "#dev"   # name preferred
    for target in (WebhookTarget(url=_A), WebhookTarget(url=_A, name="#dev")):
        assert "sig" not in target.label
        assert "aaa" not in target.label
    assert WebhookTarget(url="not a url").label == "unnamed"    # unparseable, still safe


def test_webhooks_property_keeps_order_and_drops_blanks():
    s = Settings(teams_webhook_url=" ", teams_webhook_urls=[_B, "", _A, _B])
    assert s.teams_webhooks == [_B, _A]


# ── Named channel list ({name, url, active}) ──────────────────────────────────

_C = "https://c.example/workflows/3?sig=ccc"


async def test_named_channels_are_posted_to_with_their_names_available():
    http = _FakeHttp()
    n = _notifier(http, teams_webhook_url="", teams_webhook_channels=[
        {"name": "#dev", "url": _A, "active": True},
        {"name": "#qc", "url": _B, "active": True},
    ])
    assert n.is_enabled is True
    await n.send(_message())
    assert [u for u, _ in http.posts] == [_A, _B]
    assert [t.label for t in n._config.teams_webhook_targets] == ["#dev", "#qc"]


async def test_an_inactive_channel_is_not_posted_to_but_keeps_its_url():
    """Muting has to be non-destructive: a Workflows URL cannot be recovered once the
    channel's flow is deleted, so "remove the row to stop notifying" was the wrong
    affordance."""
    http = _FakeHttp()
    cfg_channels = [
        {"name": "#dev", "url": _A, "active": True},
        {"name": "#qc", "url": _B, "active": False},
    ]
    n = _notifier(http, teams_webhook_url="", teams_webhook_channels=cfg_channels)
    await n.send(_message())
    assert [u for u, _ in http.posts] == [_A]              # #qc skipped
    assert cfg_channels[1]["url"] == _B                     # …but still stored
    assert n._config.muted_teams_channels == ["#qc"]


def test_active_defaults_to_on_when_the_key_is_absent():
    """A hand-written entry without the flag must notify, not silently do nothing."""
    s = Settings(teams_webhook_url="", teams_webhook_channels=[{"name": "#dev", "url": _A}])
    assert s.teams_webhooks == [_A]


def test_all_three_sources_merge_without_double_notifying():
    """The single URL, the named list and the legacy list can each hold the same channel;
    listing it twice must not notify it twice."""
    s = Settings(
        teams_webhook_url=_A,
        teams_webhook_channels=[{"name": "#dev", "url": _A}, {"name": "#qc", "url": _B}],
        teams_webhook_urls=[_B, _C],
    )
    assert s.teams_webhooks == [_A, _B, _C]
    # First name seen wins, so the primary keeps its label rather than being renamed.
    assert [t.label for t in s.teams_webhook_targets] == ["primary", "#qc", "c.example"]


def test_a_plain_url_string_in_the_list_is_accepted():
    """Pasting a bare list of URLs into config.yaml has to keep working."""
    s = Settings(teams_webhook_url="", teams_webhook_channels=[_A, {"url": _B}])
    assert s.teams_webhooks == [_A, _B]


def test_malformed_entries_are_skipped_not_crashed_on():
    """A bad hand-edit must not stop the autopilot from starting."""
    s = Settings(teams_webhook_url="", teams_webhook_channels=[
        None, 42, {}, {"name": "no url"}, {"url": "   "}, {"url": _A},
    ])
    assert s.teams_webhooks == [_A]


def test_a_channel_with_no_name_still_gets_a_safe_label():
    s = Settings(teams_webhook_url="", teams_webhook_channels=[{"url": _A}])
    label = s.teams_webhook_targets[0].label
    assert label == "a.example" and "sig" not in label
