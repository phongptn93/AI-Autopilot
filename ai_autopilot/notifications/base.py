"""Notification channel abstraction (ported from ``INotificationChannel``)."""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ai_autopilot.models import ExecutionResult, WorkItemInfo


class NotificationType(enum.Enum):
    STARTED = "Started"
    COMPLETED = "Completed"
    ERROR = "Error"


@dataclass
class NotificationMessage:
    work_item: WorkItemInfo
    type: NotificationType
    skill: str = ""
    result: ExecutionResult | None = None
    error: str | None = None

    @property
    def title(self) -> str:
        wid = self.work_item.id
        if self.type is NotificationType.STARTED:
            return f"🤖 Processing #{wid}"
        if self.type is NotificationType.COMPLETED:
            return (
                f"✅ Completed #{wid}"
                if self.result and self.result.success
                else f"❌ Failed #{wid}"
            )
        if self.type is NotificationType.ERROR:
            return f"⚠️ Error #{wid}"
        return f"📋 #{wid}"

    @property
    def summary(self) -> str:
        item = self.work_item
        if self.type is NotificationType.STARTED:
            return f"**{item.title}**\nSkill: `{self.skill}` | Category: {item.category}"
        if self.type is NotificationType.COMPLETED and self.result and self.result.success:
            r = self.result
            duration = _mmss(r.duration_seconds)
            extra = ""
            if r.pr_url:
                extra += f"\nPR: {r.pr_url}"
            if r.files_changed:
                extra += f"\nFiles: {len(r.files_changed)} changed"
            return f"**{item.title}**\nSkill: `{r.skill_used}` | Duration: {duration}{extra}"
        if self.type is NotificationType.COMPLETED:
            skill = self.result.skill_used if self.result else ""
            error = self.result.error if self.result else ""
            return f"**{item.title}**\nSkill: `{skill}` | Error: {error}"
        if self.type is NotificationType.ERROR:
            return f"**{item.title}**\n{self.error}"
        return item.title


class NotificationChannel(ABC):
    """A single outbound notification channel."""

    name: str = "channel"

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        ...

    @abstractmethod
    async def send(self, message: NotificationMessage) -> None:
        ...


def _mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"
