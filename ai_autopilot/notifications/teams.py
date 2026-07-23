"""MS Teams notifications via Workflows Webhook (Adaptive Card)."""

from __future__ import annotations

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
        return bool(self._config.teams_webhook_url)

    async def send(self, message: NotificationMessage) -> None:
        if not self.is_enabled:
            return
        try:
            resp = await self._http.post(
                self._config.teams_webhook_url, json=self._payload(message)
            )
            if resp.status_code >= 400:
                self._log.warning("teams webhook failed", status=resp.status_code, body=resp.text)
            else:
                self._log.debug("teams notification sent", title=message.title)
        except httpx.HTTPError as exc:
            self._log.warning("teams notification failed", error=str(exc))

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
