"""How the dashboard renders time — the two questions it must not confuse."""

from __future__ import annotations

from ai_autopilot.dashboard import _fmt_ago, _fmt_duration

_HOUR = 3600
_DAY = 24 * _HOUR


def test_duration_stays_precise_because_a_run_length_is_a_measurement():
    assert _fmt_duration(45) == "45s"
    assert _fmt_duration(150) == "2.5m"
    assert _fmt_duration(2 * _HOUR) == "2.0h"


def test_age_drops_to_days_instead_of_counting_hours_forever():
    """A machine last seen three days ago read "73.9h trước" on the fleet page.

    Nobody counts past a day in hours, and the tenth of an hour implies a precision that
    means nothing at that distance — 73.9h and 74.1h are the same fact. Both figures now
    say the thing a person would say.
    """
    assert _fmt_ago(73.9 * _HOUR) == "3 ngày"
    assert _fmt_ago(74.1 * _HOUR) == "3 ngày"
    assert _fmt_ago(8 * _DAY) == "1 tuần"
    assert _fmt_ago(40 * _DAY) == "1 tháng"


def test_age_keeps_useful_resolution_while_the_machine_is_live():
    assert _fmt_ago(10) == "vài giây"
    assert _fmt_ago(186) == "3 phút"
    assert _fmt_ago(2 * _HOUR) == "2 giờ"


def test_age_never_renders_a_negative_clock_skew_as_the_future():
    """Worker and central clocks drift; a slightly negative age must not read as "-1 phút"."""
    assert _fmt_ago(-30) == "vài giây"
