"""MS Teams notifications via Workflows Webhook (Adaptive Card)."""

from __future__ import annotations

import asyncio

import httpx

from ai_autopilot.config import Settings, WebhookTarget
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
        targets = self._config.teams_webhook_targets
        if not targets:
            return
        payload = self._payload(message)  # built once — identical for every channel
        results = await asyncio.gather(
            *(self._post(target, payload) for target in targets), return_exceptions=True
        )
        sent = sum(1 for r in results if r is True)
        if sent != len(targets):
            # Name the channels that failed. "2 of 3 delivered" left you to guess which,
            # and every Workflows URL shares the same host, so the host was no help.
            failed = [t.label for t, r in zip(targets, results, strict=False) if r is not True]
            self._log.warning(
                "teams notification partially delivered",
                sent=sent, total=len(targets), failed=failed, title=message.title,
            )
        else:
            self._log.debug("teams notification sent", title=message.title, channels=sent)

    async def _post(self, target: WebhookTarget, payload: dict) -> bool:
        """True when this one webhook accepted the card. Never raises."""
        try:
            resp = await self._http.post(target.url, json=payload)
        except httpx.HTTPError as exc:
            # Identify the channel by NAME (falling back to host): the full URL carries the
            # token that authorises posting to it, so it must not land in the log.
            self._log.warning("teams webhook error", channel=target.label, error=str(exc))
            return False
        if resp.status_code >= 400:
            self._log.warning(
                "teams webhook failed", channel=target.label,
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


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
