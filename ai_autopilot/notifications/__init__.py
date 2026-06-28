"""Outbound notification channels (Teams, Zalo, Email)."""

from ai_autopilot.notifications.base import (
    NotificationChannel,
    NotificationMessage,
    NotificationType,
)
from ai_autopilot.notifications.email import EmailNotifier
from ai_autopilot.notifications.teams import TeamsNotifier
from ai_autopilot.notifications.zalo import ZaloNotifier

__all__ = [
    "EmailNotifier",
    "NotificationChannel",
    "NotificationMessage",
    "NotificationType",
    "TeamsNotifier",
    "ZaloNotifier",
]
