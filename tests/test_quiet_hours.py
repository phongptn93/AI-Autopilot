"""Tests for the notification window: when the autopilot may interrupt a human."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ai_autopilot.ado.notifier import AdoNotifier
from ai_autopilot.config import Settings
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.notifications.base import NotificationMessage, NotificationType
from ai_autopilot.scheduling import (
    QuietHours,
    ScheduleGuard,
    in_window,
    render_held_summary,
    resolve_tz,
)

VN = ZoneInfo("Asia/Ho_Chi_Minh")          # UTC+7
WORK = dict(timezone="Asia/Ho_Chi_Minh", notify_hours_start="08:00",
            notify_hours_end="18:00", notify_days="Mon,Tue,Wed,Thu,Fri")


def _at(y, m, d, hh, mm=0, tz=VN) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=tz)


# ── the bug this feature exists to stop repeating ────────────────────────────

def test_window_is_read_in_the_configured_timezone_not_utc():
    """A team in UTC+7 setting 08:00–18:00 used to configure 15:00–01:00 their own
    time, because the window was compared against `datetime.now(UTC)`. The feature ran,
    reported no error, and did close to the opposite of what it said."""
    quiet = QuietHours(Settings(**WORK))
    # 02:00 UTC on a Monday is 09:00 in Ho Chi Minh — the middle of the workday.
    utc_morning = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)
    assert quiet.is_quiet(utc_morning) is False
    # 14:00 UTC is 21:00 local — after work, whatever the server clock says.
    assert quiet.is_quiet(datetime(2026, 8, 24, 14, 0, tzinfo=UTC)) is True


def test_quiet_hours_stay_off_until_a_timezone_is_set():
    """Guessing wrong here suppresses notifications through the workday and delivers
    them at midnight — precisely what the feature was turned on to prevent. So it waits
    for an explicit timezone instead of reading the machine's."""
    quiet = QuietHours(Settings(notify_hours_start="08:00", notify_hours_end="18:00"))
    assert quiet.enabled is False
    assert quiet.is_quiet(_at(2026, 8, 24, 23)) is False


def test_the_work_window_keeps_applying_without_a_timezone():
    """The asymmetry is deliberate: silently ceasing to apply a work window somebody
    configured would leave the autopilot running all night."""
    guard = ScheduleGuard(Settings(
        schedule_start="08:00", schedule_end="18:00", schedule_days="Mon,Tue,Wed,Thu,Fri",
    ))
    machine_midnight = datetime(2026, 8, 24, 23, 0).astimezone()
    assert guard.is_within_window(machine_midnight) is False


def test_unknown_timezone_disables_rather_than_crashes():
    assert resolve_tz("Mars/Olympus") is None
    assert QuietHours(Settings(timezone="Mars/Olympus", **{
        k: v for k, v in WORK.items() if k != "timezone"})).enabled is False


# ── the window itself ────────────────────────────────────────────────────────

def test_after_work_and_weekends_are_quiet():
    quiet = QuietHours(Settings(**WORK))
    assert quiet.is_quiet(_at(2026, 8, 24, 9)) is False      # Mon 09:00 — working
    assert quiet.is_quiet(_at(2026, 8, 24, 18)) is False     # Mon 18:00 — edge, still in
    assert quiet.is_quiet(_at(2026, 8, 24, 18, 1)) is True   # Mon 18:01 — after work
    assert quiet.is_quiet(_at(2026, 8, 24, 7, 59)) is True   # before work
    assert quiet.is_quiet(_at(2026, 8, 22, 10)) is True      # Saturday
    assert quiet.is_quiet(_at(2026, 8, 23, 10)) is True      # Sunday


def test_an_overnight_window_covers_the_morning_after():
    """"Fri 22:00–06:00" must not stop at midnight, or Saturday morning is uncovered —
    the tail belongs to the day the window STARTED on."""
    tz = VN
    args = ("22:00", "06:00", "Fri", tz)
    assert in_window(_at(2026, 8, 21, 23), *args) is True     # Fri 23:00
    assert in_window(_at(2026, 8, 22, 3), *args) is True      # Sat 03:00 — Friday's tail
    assert in_window(_at(2026, 8, 22, 23), *args) is False    # Sat 23:00 — not Friday
    assert in_window(_at(2026, 8, 21, 12), *args) is False    # Fri midday


def test_a_typo_fails_open():
    """A bad time field must not silently stop the autopilot from telling anyone
    anything — the failure mode of a notification guard has to be "notify"."""
    assert in_window(_at(2026, 8, 24, 23), "not-a-time", "18:00", "Mon", VN) is True
    assert QuietHours(Settings(timezone="Asia/Ho_Chi_Minh",
                               notify_hours_start="8h", notify_hours_end="18:00")
                      ).is_quiet(_at(2026, 8, 24, 23)) is False


def test_schedule_guard_shares_the_timezone_fix():
    guard = ScheduleGuard(Settings(
        timezone="Asia/Ho_Chi_Minh", schedule_start="08:00", schedule_end="18:00",
        schedule_days="Mon,Tue,Wed,Thu,Fri",
    ))
    assert guard.is_within_window(datetime(2026, 8, 24, 2, tzinfo=UTC)) is True   # 09:00 local
    assert guard.is_within_window(datetime(2026, 8, 24, 14, tzinfo=UTC)) is False  # 21:00 local



# ── holding and delivering ───────────────────────────────────────────────────

class _FakeChannel:
    name = "fake"
    is_enabled = True

    def __init__(self):
        self.sent: list[NotificationMessage] = []

    async def send(self, message):
        self.sent.append(message)


class _FakeHold:
    def __init__(self):
        self.rows: list = []
        self.drained = 0

    async def hold(self, kind, title, body, work_item_id=0, cap=200):
        self.rows.append(SimpleNamespace(
            kind=kind, title=title, body=body, work_item_id=work_item_id,
            at=datetime(2026, 8, 24, 22, 30, tzinfo=UTC),
        ))
        return 0

    async def drain(self):
        rows, self.rows = self.rows, []
        self.drained += 1
        return rows


def _notifier(hold, channel, **overrides):
    cfg = Settings(**{**WORK, **overrides})
    return AdoNotifier(ado=None, config=cfg, channels=[channel], hold_repo=hold)


def _msg(item_id=7):
    return NotificationMessage(
        work_item=WorkItemInfo(id=item_id, title="t"), type=NotificationType.COMPLETED
    )


async def test_after_hours_notices_are_held_not_sent(monkeypatch):
    hold, channel = _FakeHold(), _FakeChannel()
    notifier = _notifier(hold, channel)
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: True)

    await notifier._broadcast(_msg())
    assert channel.sent == []                 # nobody's phone went off
    assert len(hold.rows) == 1                # and nothing was lost


async def test_held_notices_come_back_as_ONE_summary(monkeypatch):
    """Forty pings delivered at 08:00 is the same wall of noise, just later."""
    hold, channel = _FakeHold(), _FakeChannel()
    notifier = _notifier(hold, channel)

    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: True)
    for i in range(5):
        await notifier._broadcast(_msg(i))
    assert channel.sent == []

    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: False)
    assert await notifier.flush_quiet() == 5
    assert len(channel.sent) == 1
    assert "5 thông báo" in channel.sent[0].title
    assert channel.sent[0].summary.count("•") == 5


async def test_in_hours_notices_go_straight_out(monkeypatch):
    hold, channel = _FakeHold(), _FakeChannel()
    notifier = _notifier(hold, channel)
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: False)
    await notifier._broadcast(_msg())
    assert len(channel.sent) == 1 and hold.rows == []


async def test_the_feature_is_off_until_configured():
    """No notify window configured → every notice goes out immediately, as before."""
    hold, channel = _FakeHold(), _FakeChannel()
    notifier = AdoNotifier(None, Settings(), [channel], hold)
    await notifier._broadcast(_msg())
    assert len(channel.sent) == 1 and hold.rows == []


async def test_a_queue_failure_sends_rather_than_swallows(monkeypatch):
    """If holding fails, the notice must still reach someone — losing it entirely is a
    worse outcome than an out-of-hours ping."""
    class _Broken(_FakeHold):
        async def hold(self, *a, **kw):
            raise RuntimeError("db down")

    channel = _FakeChannel()
    notifier = _notifier(_Broken(), channel)
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: True)
    await notifier._broadcast(_msg())
    assert len(channel.sent) == 1


def test_summary_reports_what_had_to_be_dropped():
    rows = [SimpleNamespace(kind="Completed", title=f"#{i}", at=_at(2026, 8, 24, 22))
            for i in range(25)]
    heading, body = render_held_summary(rows, dropped=7)
    assert "25" in heading
    assert "và 5 thông báo nữa" in body      # only the first 20 are listed
    assert "7 thông báo cũ hơn đã bị bỏ" in body
    assert render_held_summary([]) == ("", "")
