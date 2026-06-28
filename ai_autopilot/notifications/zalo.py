"""Zalo OA notifications via the Zalo OA API."""

from __future__ import annotations

import httpx

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger
from ai_autopilot.notifications.base import NotificationChannel, NotificationMessage

_ZALO_API_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"


class ZaloNotifier(NotificationChannel):
    name = "Zalo OA"

    def __init__(self, config: Settings, http: httpx.AsyncClient) -> None:
        self._config = config
        self._http = http
        self._log = get_logger("notifications.zalo")

    @property
    def is_enabled(self) -> bool:
        return bool(self._config.zalo_oa_access_token and self._config.zalo_recipient_user_id)

    async def send(self, message: NotificationMessage) -> None:
        if not self.is_enabled:
            return
        text = f"{message.title}\n\n{message.summary}"
        payload = {
            "recipient": {"user_id": self._config.zalo_recipient_user_id},
            "message": {"text": text},
        }
        headers = {"access_token": self._config.zalo_oa_access_token}
        try:
            resp = await self._http.post(_ZALO_API_URL, json=payload, headers=headers)
            if resp.status_code >= 400:
                self._log.warning("zalo send failed", status=resp.status_code, body=resp.text)
                return
            body = resp.json()
            if body.get("error", -1) != 0:
                self._log.warning("zalo returned error", body=body)
            else:
                self._log.debug("zalo notification sent", title=message.title)
        except httpx.HTTPError as exc:
            self._log.warning("zalo notification failed", error=str(exc))
