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


# ── delivery log: every notice leaves a trace of what happened to it ────────

class _FakeLog:
    def __init__(self):
        self.rows: list[dict] = []

    async def record(self, **kw):
        self.rows.append(kw)


class _FanOut(_FakeChannel):
    """A channel that reports per-webhook results, the way Teams does."""

    name = "teams"

    def __init__(self, report):
        super().__init__()
        self._report = report

    async def send(self, message):
        self.sent.append(message)
        return self._report


def _logged(channel, **overrides):
    log = _FakeLog()
    cfg = Settings(**{**WORK, **overrides})
    notifier = AdoNotifier(None, cfg, [channel], _FakeHold(), log_repo=log)
    return notifier, log


async def test_delivery_log_records_sent_held_and_suppressed(monkeypatch):
    notifier, log = _logged(_FakeChannel(), alert_events="completed,failed")
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: False)
    await notifier._broadcast(_msg())                                    # completed → sent
    await notifier._broadcast(NotificationMessage(                       # reminder → off
        work_item=WorkItemInfo(id=7, title="t"), type=NotificationType.REMINDER))
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: True)
    await notifier._broadcast(_msg())                                    # after hours → held
    assert [r["outcome"] for r in log.rows] == ["sent", "suppressed", "held"]
    assert "alert_events" in log.rows[1]["detail"]


async def test_delivery_log_names_the_webhook_that_refused(monkeypatch):
    notifier, log = _logged(_FanOut((1, 2, ["dev-channel"])))
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: False)
    await notifier._broadcast(_msg())
    assert log.rows[-1]["outcome"] == "partial"
    assert "dev-channel" in log.rows[-1]["detail"]

    notifier, log = _logged(_FanOut((0, 1, ["pm-channel"])))
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: False)
    await notifier._broadcast(_msg())
    assert log.rows[-1]["outcome"] == "failed"


async def test_the_morning_summary_is_logged_too(monkeypatch):
    notifier, log = _logged(_FakeChannel())
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: True)
    await notifier._broadcast(_msg())
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: False)
    await notifier.flush_quiet()
    assert [r["outcome"] for r in log.rows] == ["held", "sent"]
    assert "ngoài giờ" in log.rows[-1]["title"]


def test_summary_collapses_a_notice_raised_repeatedly():
    rows = [SimpleNamespace(kind="Reminder", title="🙋 PR !4318 — conflict cần người giải",
                            at=_at(2026, 9, 27, h)) for h in (9, 11, 14)]
    rows.append(SimpleNamespace(kind="Reminder", title="⚠️ PR !2748 bị merge conflict",
                                at=_at(2026, 9, 27, 10)))
    _, body = render_held_summary(rows)
    assert body.count("PR !4318") == 1 and "×3" in body
    assert "27/09 14:00" in body                       # the latest occurrence is shown


def test_a_notice_without_a_work_item_is_named_not_numbered():
    m = NotificationMessage(work_item=WorkItemInfo(id=0, title="[audit] code-review-daily"),
                            type=NotificationType.COMPLETED)
    assert "#0" not in m.title and "code-review-daily" in m.title


# ── notify_window_applies_to: digest — the window governs digests only ───────

def _digest(title="📋 Sức khoẻ quy trình"):
    return NotificationMessage(work_item=WorkItemInfo(id=0), type=NotificationType.INFO,
                               heading=title, text="…")


async def test_digest_only_window_sends_item_notices_at_night(monkeypatch):
    hold, channel = _FakeHold(), _FakeChannel()
    notifier = _notifier(hold, channel, notify_window_applies_to="digest")
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: True)
    await notifier._broadcast(_msg())                        # a run finished, at 22:40
    assert len(channel.sent) == 1 and hold.rows == []        # sent, not held


async def test_digest_only_window_drops_a_digest_outside_hours(monkeypatch):
    log = _FakeLog()
    hold, channel = _FakeHold(), _FakeChannel()
    cfg = Settings(**{**WORK, "notify_window_applies_to": "digest"})
    notifier = AdoNotifier(None, cfg, [channel], hold, log_repo=log)
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: True)
    await notifier._broadcast(_digest())
    assert channel.sent == [] and hold.rows == []            # dropped, not held
    assert log.rows[-1]["outcome"] == "suppressed"
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: False)
    await notifier._broadcast(_digest())
    assert len(channel.sent) == 1                            # in hours: delivered


async def test_switching_to_digest_only_releases_the_backlog_now(monkeypatch):
    """Notices held under "all" must not wait for a window that no longer governs them."""
    hold, channel = _FakeHold(), _FakeChannel()
    notifier = _notifier(hold, channel)                      # "all": held
    monkeypatch.setattr(notifier._quiet, "is_quiet", lambda now=None: True)
    for i in range(3):
        await notifier._broadcast(_msg(i))
    assert channel.sent == [] and len(hold.rows) == 3
    notifier._config.notify_window_applies_to = "digest"     # operator switches, live
    assert await notifier.flush_quiet() == 3                 # still night — goes out anyway
    assert len(channel.sent) == 1 and "3 thông báo" in channel.sent[0].title


def test_an_unrecognised_scope_keeps_the_window_on_everything():
    assert not Settings(notify_window_applies_to="digests").notify_window_digest_only
    assert Settings(notify_window_applies_to=" Digest ").notify_window_digest_only
