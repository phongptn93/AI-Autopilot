"""Schedule window tests (ported from ScheduleGuard)."""

from __future__ import annotations

from datetime import UTC, datetime

from ai_autopilot.config import Settings
from ai_autopilot.scheduling import ScheduleGuard


def _guard(**overrides) -> ScheduleGuard:
    # Pin the zone: without it the window follows the MACHINE's timezone, so this suite
    # would pass in CI (UTC) and fail on a laptop in UTC+7 — the very confusion the
    # timezone setting exists to end.
    overrides.setdefault("timezone", "UTC")
    return ScheduleGuard(Settings(**overrides))


def test_no_schedule_always_allowed():
    assert _guard().is_within_window() is True


def test_within_day_window():
    guard = _guard(schedule_start="08:00", schedule_end="18:00", schedule_days="Mon")
    monday_noon = datetime(2026, 6, 29, 12, 0, tzinfo=UTC)  # Monday
    assert guard.is_within_window(monday_noon) is True


def test_outside_time_window():
    guard = _guard(schedule_start="08:00", schedule_end="18:00", schedule_days="Mon")
    monday_evening = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
    assert guard.is_within_window(monday_evening) is False


def test_disallowed_day():
    guard = _guard(schedule_start="08:00", schedule_end="18:00", schedule_days="Mon")
    sunday = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)  # Sunday
    assert guard.is_within_window(sunday) is False


def test_overnight_window():
    guard = _guard(schedule_start="22:00", schedule_end="06:00", schedule_days="Mon,Tue")
    early = datetime(2026, 6, 30, 2, 0, tzinfo=UTC)  # Tuesday 02:00
    assert guard.is_within_window(early) is True
