"""Time windows: when the autopilot may work, and when it may interrupt a human.

Two different questions, one piece of arithmetic:

* **Schedule window** — may a run START now? Guards the poller.
* **Notification window** — may we PING someone now? Guards the notification channels.

They are deliberately separate. A team is usually happy for the autopilot to keep
working in the evening; what they do not want is their phone going off at 22:40 about a
task nobody can act on until morning. Conflating the two means turning off the work to
turn off the noise.

**Both windows are timezone-explicit.** They used to be compared against
``datetime.now(UTC)``, so a team in UTC+7 that set 08:00–18:00 was really configuring
15:00–01:00 their own time — it ran, reported no error, and did close to the opposite of
what it said.

The two windows treat a MISSING timezone differently, on purpose:

* the **work** window falls back to the machine's own zone (what cron means by "18:00",
  and identical to the old behaviour on a UTC server) — because silently ceasing to
  apply a window somebody configured would leave the autopilot running all night;
* the **notification** window stays off until a timezone is set — because guessing wrong
  there suppresses notifications through the workday and delivers them at midnight,
  which is precisely what it was turned on to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger

_log = get_logger("scheduling")

_DAY_ALIASES = {
    "MON": 0, "MONDAY": 0,
    "TUE": 1, "TUESDAY": 1,
    "WED": 2, "WEDNESDAY": 2,
    "THU": 3, "THURSDAY": 3,
    "FRI": 4, "FRIDAY": 4,
    "SAT": 5, "SATURDAY": 5,
    "SUN": 6, "SUNDAY": 6,
}


def resolve_tz(name: str) -> tzinfo | None:
    """The configured timezone, or ``None`` when it is blank/unknown."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _log.warning("unknown timezone", timezone=name)
        return None


def schedule_tz(cfg: Settings) -> tzinfo:
    """Timezone for the WORK window: configured, else the machine's own.

    The machine's own is the least surprising fallback — it is what cron means by
    "18:00" — and on a UTC server it is identical to the old behaviour, so nothing
    changes for an existing install. What it must NOT do is silently stop applying a
    window somebody configured: they asked the autopilot to stand down in the evening,
    and quietly running all night instead is the worse failure.
    """
    return resolve_tz(cfg.timezone) or datetime.now().astimezone().tzinfo or UTC


def parse_days(value: str) -> set[int]:
    """``"Mon,Tue"`` → ``{0, 1}``. An unparsable entry is dropped, not fatal."""
    return {
        _DAY_ALIASES[d.strip().upper()]
        for d in (value or "").split(",")
        if d.strip().upper() in _DAY_ALIASES
    }


def parse_time(value: str) -> time | None:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def in_window(
    now: datetime, start: str, end: str, days: str, tz: tzinfo | None
) -> bool:
    """Is ``now`` inside ``start``–``end`` on an allowed day, read in ``tz``?

    A blank start/end, an unparsable one, or no timezone all mean "no window" → always
    true. That keeps the guard fail-open: a typo in a time field must not silently stop
    an autopilot from working (or from telling anyone that it stopped).

    ``end < start`` is read as an overnight window (22:00–06:00).
    """
    begin, finish = parse_time(start), parse_time(end)
    if begin is None or finish is None or tz is None:
        return True

    local = now.astimezone(tz)
    allowed = parse_days(days)
    current = local.time()
    within_day = (
        begin <= current <= finish
        if begin <= finish
        else current >= begin or current <= finish
    )
    if not allowed:
        return within_day
    if begin <= finish:
        return local.weekday() in allowed and within_day
    # Overnight: the tail after midnight belongs to the day the window STARTED on, or
    # "Fri 22:00–06:00" would stop at midnight and leave Saturday morning uncovered.
    if current >= begin:
        return local.weekday() in allowed
    return (local.weekday() - 1) % 7 in allowed


class ScheduleGuard:
    """May a run start now? (Blank config → always.)"""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("scheduling.guard")

    def is_within_window(self, now: datetime | None = None) -> bool:
        cfg = self._config
        if not cfg.schedule_start or not cfg.schedule_end:
            return True
        tz = schedule_tz(cfg)
        if not cfg.timezone:
            # Falling back is defensible; leaving it undiscussed is not — on a UTC
            # server this window means UTC hours, which is rarely what was meant.
            self._log.debug(
                "schedule window using the machine timezone — set `timezone` to be sure",
                tz=str(tz),
            )
        ok = in_window(
            now or datetime.now(tz), cfg.schedule_start, cfg.schedule_end,
            cfg.schedule_days, tz,
        )
        if not ok:
            self._log.debug("outside schedule window")
        return ok


class QuietHours:
    """May we interrupt a human now? (Blank config → always.)

    The counterpart to ``ScheduleGuard``: work may continue after hours, notifications
    wait. Nothing is discarded — held notices are delivered as ONE summary when the
    window opens, so the morning brings a single line instead of forty overnight pings,
    and nothing that happened is lost.

    Unlike the work window this one REQUIRES an explicit ``timezone`` and stays off
    without it. The two are not symmetrical: guessing wrong on the work window costs a
    run at an odd hour, while guessing wrong here suppresses notifications through the
    workday and delivers them at midnight — the exact behaviour it was turned on to
    prevent. This feature is new, so requiring the setting surprises nobody.
    """

    def __init__(self, config: Settings) -> None:
        self._config = config

    @property
    def enabled(self) -> bool:
        cfg = self._config
        return bool(
            cfg.notify_hours_start and cfg.notify_hours_end and resolve_tz(cfg.timezone)
        )

    def is_quiet(self, now: datetime | None = None) -> bool:
        """True when a notification should be held rather than sent."""
        cfg = self._config
        if not self.enabled:
            return False
        tz = resolve_tz(cfg.timezone)
        return not in_window(
            now or datetime.now(tz), cfg.notify_hours_start, cfg.notify_hours_end,
            cfg.notify_days, tz,
        )

    def local_now(self, now: datetime | None = None) -> datetime:
        tz = resolve_tz(self._config.timezone)
        base = now or datetime.now(tz) if tz else (now or datetime.now())
        return base.astimezone(tz) if tz else base


def render_held_summary(held: list, dropped: int = 0) -> tuple[str, str]:
    """``(heading, body)`` for the notices collected while the window was shut.

    One message, not a replay: forty pings delivered at 08:00 is the same wall of noise
    the quiet window was meant to prevent, just later. The detail stays on the work
    items, which is where someone acts on it anyway.
    """
    if not held:
        return "", ""
    lines = [
        f"🔕 {len(held)} thông báo đã giữ lại ngoài giờ làm việc",
        "",
    ]
    by_kind: dict[str, int] = {}
    for row in held:
        by_kind[row.kind or "khác"] = by_kind.get(row.kind or "khác", 0) + 1
    lines.append(" · ".join(f"{k}: {n}" for k, n in sorted(by_kind.items())))
    lines.append("")
    for row in held[:20]:
        when = row.at.strftime("%d/%m %H:%M") if row.at else ""
        title = (row.title or "").strip()
        lines.append(f"• {when} {title}")
    if len(held) > 20:
        lines.append(f"… và {len(held) - 20} thông báo nữa")
    if dropped:
        lines.append(
            f"\n⚠️ {dropped} thông báo cũ hơn đã bị bỏ do vượt hạn mức lưu giữ."
        )
    return f"🔕 {len(held)} thông báo ngoài giờ", "\n".join(lines)
