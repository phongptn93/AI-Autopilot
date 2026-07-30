"""MS Teams notifications via Workflows Webhook (Adaptive Card)."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger
from ai_autopilot.notifications.base import (
    NotificationChannel,
    NotificationMessage,
    NotificationType,
)

_COLORS = {
    NotificationType.STARTED: "accent",
    NotificationType.ERROR: "warning",
}


class TeamsNotifier(NotificationChannel):
    name = "MS Teams"

    def __init__(self, config: Settings, http: httpx.AsyncClient) -> None:
        self._config = config
        self._http = http
        self._log = get_logger("notifications.teams")

    @property
    def is_enabled(self) -> bool:
        return bool(self._config.teams_webhooks)

    async def send(self, message: NotificationMessage) -> None:
        """Post the card to EVERY configured webhook, concurrently.

        One channel failing (revoked Workflows URL, a channel that was deleted) must not stop
        the others — otherwise adding a second channel would make notifications less reliable
        than having one. So each post is awaited independently and failures are logged per
        URL, never raised."""
        webhooks = self._config.teams_webhooks
        if not webhooks:
            return
        payload = self._payload(message)  # built once — identical for every channel
        results = await asyncio.gather(
            *(self._post(url, payload) for url in webhooks), return_exceptions=True
        )
        sent = sum(1 for r in results if r is True)
        if sent != len(webhooks):
            self._log.warning(
                "teams notification partially delivered",
                sent=sent, total=len(webhooks), title=message.title,
            )
        else:
            self._log.debug("teams notification sent", title=message.title, channels=sent)

    async def _post(self, url: str, payload: dict) -> bool:
        """True when this one webhook accepted the card. Never raises."""
        try:
            resp = await self._http.post(url, json=payload)
        except httpx.HTTPError as exc:
            # Log the webhook's HOST only: the full URL carries the token that authorises
            # posting to the channel, so it must not land in the log.
            self._log.warning("teams webhook error", host=_host(url), error=str(exc))
            return False
        if resp.status_code >= 400:
            self._log.warning(
                "teams webhook failed", host=_host(url),
                status=resp.status_code, body=resp.text[:200],
            )
            return False
        return True

    @staticmethod
    def _payload(message: NotificationMessage) -> dict:
        color = _COLORS.get(message.type)
        if message.type is NotificationType.COMPLETED:
            color = "good" if message.result and message.result.success else "attention"
        color = color or "default"

        facts: list[dict[str, str]] = [
            {"title": "Work Item", "value": f"#{message.work_item.id} {message.work_item.title}"},
            {"title": "Type", "value": message.work_item.work_item_type},
            {"title": "Category", "value": str(message.work_item.category)},
        ]
        if message.skill:
            facts.append({"title": "Skill", "value": message.skill})
        if message.result is not None:
            r = message.result
            facts.append({"title": "Duration", "value": _mmss(r.duration_seconds)})
            if r.branch_name:
                facts.append({"title": "Branch", "value": r.branch_name})
            if r.pr_url:
                facts.append({"title": "PR", "value": r.pr_url})
            if r.error:
                facts.append({"title": "Error", "value": r.error})
        if message.error:
            facts.append({"title": "Error", "value": message.error})

        # Call-to-action buttons — jump straight to the PR / dashboard from Teams.
        # (Action.OpenUrl works from webhook-posted cards; POST-back actions would need
        # a full Bot Framework bot, which this channel intentionally avoids.)
        actions = [
            {"type": "Action.OpenUrl", "title": label, "url": url}
            for label, url in (message.actions or [])
            if url
        ]
        content: dict = {
            "type": "AdaptiveCard",
            "body": [
                {
                    "type": "TextBlock",
                    "size": "Medium",
                    "weight": "Bolder",
                    "text": message.title,
                    "color": color,
                    "wrap": True,
                },
                {"type": "FactSet", "facts": facts},
            ],
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
        }
        if actions:
            content["actions"] = actions
        return {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": content,
                }
            ],
        }


def _host(url: str) -> str:
    """Host of a webhook URL, for logs — the path and query hold the auth token."""
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
