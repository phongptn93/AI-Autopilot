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


class Severity(enum.IntEnum):
    """How much a notice deserves to interrupt someone.

    Ordered so a channel can be configured with a FLOOR (``min_severity``) and the
    comparison is just ``>=``. The names are what a reader picks in the dashboard, so
    they describe the reader's obligation, not the system's internal state:

    ``INFO``     — worth knowing, nobody has to do anything ("run started").
    ``WARNING``  — somebody should look today ("run failed", "reviewer hasn't voted").
    ``CRITICAL`` — something is blocked NOW and a human is the only way past it.
    """

    INFO = 10
    WARNING = 20
    CRITICAL = 30

    @classmethod
    def parse(cls, value: str | int | None, default: Severity = None) -> Severity:
        """Read a configured severity leniently — a typo must not mute a channel.

        Anything unrecognised falls back to ``default`` (INFO when not given), which
        errs towards DELIVERING: a misspelled floor that silently swallowed every
        alert is the failure nobody would notice until it mattered."""
        fallback = cls.INFO if default is None else default
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            return cls(value) if value in {int(m) for m in cls} else fallback
        name = (value or "").strip().upper()
        return cls.__members__.get(name, fallback)


# Event kinds a reader can switch on or off, and the severity each carries. These are
# the vocabulary of ``alert_events`` — the CSV in the dashboard — so they are lowercase
# and stable: a stored config names them, and renaming one would silently disable it.
EVENT_STARTED = "started"
EVENT_COMPLETED = "completed"
EVENT_FAILED = "failed"
EVENT_ERROR = "error"
EVENT_REMINDER = "reminder"
EVENT_DIGEST = "digest"

ALL_EVENTS: tuple[str, ...] = (
    EVENT_STARTED, EVENT_COMPLETED, EVENT_FAILED, EVENT_ERROR, EVENT_REMINDER,
    EVENT_DIGEST,
)

# What each event is worth interrupting for. ``completed`` (success) is deliberately
# INFO: it is the expected outcome, and a channel that pages on success is a channel
# people mute — which then also mutes the failures.
_EVENT_SEVERITY: dict[str, Severity] = {
    EVENT_STARTED: Severity.INFO,
    EVENT_COMPLETED: Severity.INFO,
    EVENT_FAILED: Severity.WARNING,
    EVENT_ERROR: Severity.WARNING,
    EVENT_REMINDER: Severity.WARNING,
    EVENT_DIGEST: Severity.INFO,
}

# Human labels for the dashboard, so the switch list reads as outcomes rather than
# as the enum's own spelling.
EVENT_LABELS: dict[str, str] = {
    EVENT_STARTED: "Bắt đầu chạy",
    EVENT_COMPLETED: "Chạy xong (thành công)",
    EVENT_FAILED: "Chạy xong (thất bại)",
    EVENT_ERROR: "Lỗi hệ thống",
    EVENT_REMINDER: "Nhắc reviewer",
    EVENT_DIGEST: "Digest / tổng hợp",
}


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
    def event(self) -> str:
        """Which switchable event kind this notice is — the key ``alert_events`` uses.

        Derived rather than stored so no caller can raise a notice that is invisible to
        the filter: every message has a type, and every type maps here. The one
        distinction the type alone cannot make is success vs failure — both arrive as
        ``COMPLETED`` — and that is precisely the split a team wants to configure, so
        it is read from the result."""
        if self.type is NotificationType.STARTED:
            return EVENT_STARTED
        if self.type is NotificationType.COMPLETED:
            failed = self.result is not None and not self.result.success
            return EVENT_FAILED if failed else EVENT_COMPLETED
        if self.type is NotificationType.ERROR:
            return EVENT_ERROR
        if self.type is NotificationType.REMINDER:
            return EVENT_REMINDER
        return EVENT_DIGEST

    @property
    def severity(self) -> Severity:
        """How loudly this notice asks to be delivered. See :class:`Severity`."""
        return _EVENT_SEVERITY.get(self.event, Severity.INFO)

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
