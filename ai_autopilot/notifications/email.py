"""Email (SMTP) notifications via aiosmtplib."""

from __future__ import annotations

from email.message import EmailMessage

import aiosmtplib

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger
from ai_autopilot.notifications.base import NotificationChannel, NotificationMessage


class EmailNotifier(NotificationChannel):
    name = "Email (SMTP)"

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("notifications.email")

    @property
    def is_enabled(self) -> bool:
        return bool(self._config.smtp_host and self._config.email_to)

    async def send(self, message: NotificationMessage) -> None:
        if not self.is_enabled:
            return
        cfg = self._config
        mail = EmailMessage()
        mail["From"] = cfg.email_from or "autopilot@noreply.local"
        mail["To"] = cfg.email_to
        mail["Subject"] = f"[Autopilot] {message.title}"
        mail.set_content(message.summary)
        try:
            await aiosmtplib.send(
                mail,
                hostname=cfg.smtp_host,
                port=cfg.smtp_port,
                username=cfg.smtp_user or None,
                password=cfg.smtp_password or None,
                start_tls=cfg.smtp_port != 25,
            )
            self._log.debug("email sent", title=message.title)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("email notification failed", error=str(exc))
