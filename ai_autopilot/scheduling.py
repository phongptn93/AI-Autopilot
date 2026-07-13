"""Schedule window guard (ported from ``ScheduleGuard``)."""

from __future__ import annotations

from datetime import UTC, datetime, time

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger

_DAY_ALIASES = {
    "MON": 0, "MONDAY": 0,
    "TUE": 1, "TUESDAY": 1,
    "WED": 2, "WEDNESDAY": 2,
    "THU": 3, "THURSDAY": 3,
    "FRI": 4, "FRIDAY": 4,
    "SAT": 5, "SATURDAY": 5,
    "SUN": 6, "SUNDAY": 6,
}


class ScheduleGuard:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("scheduling.guard")

    def is_within_window(self, now: datetime | None = None) -> bool:
        cfg = self._config
        if not cfg.schedule_start or not cfg.schedule_end:
            return True

        now = now or datetime.now(UTC)

        allowed_days = {
            _DAY_ALIASES[d.strip().upper()]
            for d in cfg.schedule_days.split(",")
            if d.strip().upper() in _DAY_ALIASES
        }
        if allowed_days and now.weekday() not in allowed_days:
            self._log.debug("outside schedule: day not allowed", day=now.weekday())
            return False

        start = _parse_time(cfg.schedule_start)
        end = _parse_time(cfg.schedule_end)
        if start is None or end is None:
            return True

        current = now.time()
        in_window = (
            start <= current <= end
            if start <= end
            else current >= start or current <= end  # overnight window
        )
        if not in_window:
            self._log.debug("outside schedule window", current=str(current))
        return in_window


def _parse_time(value: str) -> time | None:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None
