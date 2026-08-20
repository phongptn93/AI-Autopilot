"""Notification channel abstraction (ported from ``INotificationChannel``)."""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ai_autopilot.models import ExecutionResult, WorkItemInfo


class NotificationType(enum.Enum):
    STARTED = "Started"
    COMPLETED = "Completed"
    ERROR = "Error"
    REMINDER = "Reminder"
    INFO = "Info"        # a free-form notice (digest) — not about one work item


@dataclass
class NotificationMessage:
    work_item: WorkItemInfo
    type: NotificationType
    skill: str = ""
    result: ExecutionResult | None = None
    error: str | None = None
    # Optional call-to-action links (label, url) rendered as buttons on channels that
    # support them (Teams Adaptive Card Action.OpenUrl). E.g. ("Open PR", "https://…").
    actions: list[tuple[str, str]] = field(default_factory=list)
    # A notice that is NOT about one work item (e.g. a periodic digest). When set,
    # these are what every channel renders — the per-type wording below all reads
    # ``work_item``, which such a notice has none of.
    heading: str = ""
    text: str = ""

    @property
    def title(self) -> str:
        if self.heading:
            return self.heading
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
        if self.type is NotificationType.REMINDER:
            return f"👋 Review reminder — PR !{wid}"
        return f"📋 #{wid}"

    @property
    def assignee(self) -> str:
        """Who the item belongs to — display name, else email, else "unassigned".

        On a shared channel a notice said what happened and to which item but never for
        whom, so a reader could not tell whose work it was."""
        item = self.work_item
        return (item.assigned_to or "").strip() or (item.assigned_to_email or "").strip() \
            or "unassigned"

    @property
    def summary(self) -> str:
        if self.text:
            return self.text
        item = self.work_item
        if self.type is NotificationType.STARTED:
            return (f"**{item.title}**\nSkill: `{self.skill}` | Category: {item.category}"
                    f" | For: {self.assignee}")
        if self.type is NotificationType.COMPLETED and self.result and self.result.success:
            r = self.result
            duration = _mmss(r.duration_seconds)
            extra = ""
            if r.pr_url:
                extra += f"\nPR: {r.pr_url}"
            if r.files_changed:
                extra += f"\nFiles: {len(r.files_changed)} changed"
            return (f"**{item.title}**\nSkill: `{r.skill_used}` | Duration: {duration}"
                    f" | For: {self.assignee}{extra}")
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
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
