"""Alert policy: what gets sent, to whom, how often.

These cover the four ways the old behaviour turned a working integration into noise —
every event broadcast to every channel, a digest posted on schedule whether or not it
had anything to say, that digest ignoring quiet hours, and the same stuck item repeated
verbatim every morning.
"""
from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ai_autopilot.config import Settings, WebhookTarget, parse_event_csv
from ai_autopilot.data.database import Database
from ai_autopilot.data.repository import AlertStateRepository
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.notifications.base import (
    ALL_EVENTS,
    NotificationMessage,
    NotificationType,
    Severity,
)

# ── event classification ──────────────────────────────────────────────────────

def _msg(kind: NotificationType, *, success: bool = True) -> NotificationMessage:
    result = ExecutionResult(work_item_id=7, success=success)
    return NotificationMessage(work_item=WorkItemInfo(id=7), type=kind, result=result)


def test_completed_and_failed_are_separate_events():
    """Both arrive as COMPLETED; a team wants to mute one and keep the other."""
    assert _msg(NotificationType.COMPLETED, success=True).event == "completed"
    assert _msg(NotificationType.COMPLETED, success=False).event == "failed"


def test_failure_outranks_success_in_severity():
    assert _msg(NotificationType.COMPLETED, success=False).severity is Severity.WARNING
    assert _msg(NotificationType.COMPLETED, success=True).severity is Severity.INFO


def test_every_event_kind_has_a_severity():
    """A kind with no severity would compare as INFO and quietly ignore the floor."""
    for name in ALL_EVENTS:
        msg = NotificationMessage(
            work_item=WorkItemInfo(id=1), type=NotificationType.INFO, heading="x"
        )
        object.__setattr__(msg, "heading", "x")
        assert Severity.parse(name, Severity.INFO) is not None


# ── the global gate ───────────────────────────────────────────────────────────

def test_started_is_off_by_default():
    """The default that halves the message count: "bot picked up #123" is not news."""
    cfg = Settings()
    assert not cfg.wants_alert("started", int(Severity.INFO))
    assert cfg.wants_alert("failed", int(Severity.WARNING))


def test_blank_event_list_means_everything_not_nothing():
    """A cleared box is far more likely a slip than a request for total silence."""
    cfg = Settings(alert_events="")
    assert cfg.alert_event_set == frozenset(ALL_EVENTS)


def test_severity_floor_filters_after_the_event_list():
    cfg = Settings(alert_events=",".join(ALL_EVENTS), alert_min_severity="warning")
    assert not cfg.wants_alert("completed", int(Severity.INFO))
    assert cfg.wants_alert("failed", int(Severity.WARNING))


def test_a_misspelled_severity_delivers_rather_than_mutes():
    """Erring towards silence would be the failure nobody notices until it matters."""
    cfg = Settings(alert_min_severity="wrning")
    assert cfg.alert_severity_floor == int(Severity.INFO)


def test_unknown_event_names_are_dropped_not_kept():
    assert parse_event_csv("completed, nope, failed") == ("completed", "failed")


# ── per-channel routing ───────────────────────────────────────────────────────

def test_channel_without_an_opinion_inherits_the_global_policy():
    target = WebhookTarget(url="https://x", name="#dev")
    assert target.wants(
        "failed", int(Severity.WARNING),
        default_events=frozenset({"failed"}), default_severity=int(Severity.INFO),
    )


def test_channel_can_narrow_to_its_own_events():
    """The reason a second channel is worth having: dev takes failures, PM takes digest."""
    dev = WebhookTarget(url="https://a", name="#dev", events=("failed", "error"))
    pm = WebhookTarget(url="https://b", name="#pm", events=("digest",))
    everything = frozenset(ALL_EVENTS)
    assert dev.wants("failed", int(Severity.WARNING),
                     default_events=everything, default_severity=int(Severity.INFO))
    assert not dev.wants("digest", int(Severity.INFO),
                         default_events=everything, default_severity=int(Severity.INFO))
    assert pm.wants("digest", int(Severity.INFO),
                    default_events=everything, default_severity=int(Severity.INFO))
    assert not pm.wants("failed", int(Severity.WARNING),
                        default_events=everything, default_severity=int(Severity.INFO))


def test_channel_severity_floor_is_read_from_the_channel():
    quiet = WebhookTarget(url="https://c", name="#exec", min_severity="critical")
    everything = frozenset(ALL_EVENTS)
    assert not quiet.wants("failed", int(Severity.WARNING),
                           default_events=everything, default_severity=int(Severity.INFO))
    assert quiet.wants("failed", int(Severity.CRITICAL),
                       default_events=everything, default_severity=int(Severity.INFO))


def test_channel_config_is_parsed_off_the_dashboard_shape():
    cfg = Settings(teams_webhook_channels=[
        {"name": "#dev", "url": "https://a", "events": "failed,error", "severity": "warning"},
        {"name": "#pm", "url": "https://b"},
        {"name": "#muted", "url": "https://c", "active": False},
    ])
    targets = {t.name: t for t in cfg.teams_webhook_targets}
    assert targets["#dev"].events == ("failed", "error")
    assert targets["#dev"].min_severity == "warning"
    assert targets["#pm"].events == ()          # no opinion → global policy
    assert "#muted" not in targets


# ── alert state: dedup, escalation, ack, snooze ───────────────────────────────

@pytest.fixture
async def repo():
    db = Database(f"sqlite+aiosqlite:///{os.path.join(tempfile.mkdtemp(), 'a.db')}")
    await db.create_all()
    yield AlertStateRepository(db)
    await db.dispose()


T0 = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


async def test_first_sighting_is_always_reported(repo):
    d = await repo.decide("stale", 1, age_hours=26, repeat_hours=24, now=T0)
    assert d.send and d.first_time


async def test_the_same_news_is_not_repeated(repo):
    await repo.decide("stale", 1, age_hours=26, repeat_hours=24, now=T0)
    d = await repo.decide("stale", 1, age_hours=27, repeat_hours=24,
                          now=T0 + timedelta(hours=1))
    assert not d.send


async def test_a_doubled_wait_escalates_before_the_repeat_window(repo):
    """"Still waiting" and "now seriously stuck" are different messages."""
    await repo.decide("stale", 1, age_hours=26, repeat_hours=24, now=T0)
    d = await repo.decide("stale", 1, age_hours=52, repeat_hours=24,
                          now=T0 + timedelta(hours=2))
    assert d.send and d.escalated


async def test_an_unactioned_alert_returns_after_the_repeat_window(repo):
    await repo.decide("stale", 1, age_hours=26, repeat_hours=24, now=T0)
    d = await repo.decide("stale", 1, age_hours=30, repeat_hours=24,
                          now=T0 + timedelta(hours=25))
    assert d.send and not d.escalated


async def test_repeat_hours_zero_reports_once_and_stays_quiet(repo):
    await repo.decide("stale", 1, age_hours=26, repeat_hours=0, now=T0)
    d = await repo.decide("stale", 1, age_hours=40, repeat_hours=0,
                          now=T0 + timedelta(days=30))
    assert not d.send


async def test_ack_silences_the_alert_indefinitely(repo):
    """Not time-limited on purpose: asking again in an hour teaches people to ignore it."""
    await repo.decide("stale", 1, age_hours=26, repeat_hours=24, now=T0)
    assert await repo.ack(1, by="phong") == 1
    d = await repo.decide("stale", 1, age_hours=500, repeat_hours=24,
                          now=T0 + timedelta(days=20))
    assert not d.send


async def test_snooze_expires_and_the_alert_comes_back(repo):
    await repo.decide("merge_ready", 2, age_hours=30, repeat_hours=24, now=T0)
    assert await repo.snooze(2, days=3) == 1
    assert not (await repo.decide("merge_ready", 2, age_hours=40, repeat_hours=24,
                                  now=datetime.now(UTC) + timedelta(days=1))).send
    assert (await repo.decide("merge_ready", 2, age_hours=99, repeat_hours=24,
                              now=datetime.now(UTC) + timedelta(days=4))).send


async def test_an_expired_snooze_stops_counting_as_muted(repo):
    """Otherwise the dashboard reports an alert as silenced while it is being sent."""
    await repo.decide("merge_ready", 2, age_hours=30, repeat_hours=24, now=T0)
    await repo.snooze(2, days=1)
    await repo.decide("merge_ready", 2, age_hours=99, repeat_hours=24,
                      now=datetime.now(UTC) + timedelta(days=2))
    assert await repo.muted_count() == 0


async def test_unack_makes_the_alert_reportable_again(repo):
    await repo.decide("stale", 1, age_hours=26, repeat_hours=24, now=T0)
    await repo.ack(1)
    assert await repo.unack(1) == 1
    assert (await repo.decide("stale", 1, age_hours=60, repeat_hours=24,
                              now=T0 + timedelta(hours=30))).send


async def test_a_cleared_problem_that_returns_is_fresh_news(repo):
    """Even after an ack — the ack was about the earlier occurrence."""
    await repo.decide("stale", 1, age_hours=26, repeat_hours=24, now=T0)
    await repo.ack(1)
    assert await repo.clear("stale", keep_ids=set()) == 1
    d = await repo.decide("stale", 1, age_hours=30, repeat_hours=24,
                          now=T0 + timedelta(days=9))
    assert d.send and d.first_time


async def test_clear_keeps_alerts_that_are_still_active(repo):
    await repo.decide("stale", 1, age_hours=26, repeat_hours=24, now=T0)
    await repo.decide("stale", 2, age_hours=26, repeat_hours=24, now=T0)
    assert await repo.clear("stale", keep_ids={1}) == 1
    assert [a.work_item_id for a in await repo.open_alerts()] == [1]


# ── the digest ────────────────────────────────────────────────────────────────

class _Report:
    """Minimal stand-in for a DeliveryReport — only what the digest filter reads."""

    def __init__(self, actions):
        self.actions = actions


class _Action:
    def __init__(self, kind, wid, age, title="t", pr_id=0):
        self.kind, self.work_item_id, self.age_hours = kind, wid, age
        self.title, self.pr_id, self.repo, self.owner = title, pr_id, "", ""

    @property
    def age_label(self):
        return f"{int(self.age_hours)} giờ"


async def test_dedup_off_reports_everything(repo):
    from ai_autopilot import teams_agent

    container = SimpleNamespace(
        config=Settings(alert_dedup_enabled=False), alert_state_repo=repo
    )
    report = _Report([_Action("stale", 1, 40), _Action("stale", 2, 50)])
    rows, suppressed = await teams_agent.filter_new_actions(container, report, now=T0)
    assert len(rows) == 2 and suppressed == 0


async def test_second_digest_holds_back_what_it_already_said(repo):
    from ai_autopilot import teams_agent

    container = SimpleNamespace(
        config=Settings(alert_dedup_enabled=True, alert_repeat_hours=24),
        alert_state_repo=repo,
    )
    report = _Report([_Action("stale", 1, 40), _Action("stale", 2, 50)])
    rows, _ = await teams_agent.filter_new_actions(container, report, now=T0)
    assert len(rows) == 2
    rows, suppressed = await teams_agent.filter_new_actions(
        container, report, now=T0 + timedelta(hours=2)
    )
    assert rows == [] and suppressed == 2


async def test_a_missing_repo_reports_rather_than_swallows(repo):
    """Failing open: a repeated line costs a line, a swallowed one costs a stuck PR."""
    from ai_autopilot import teams_agent

    container = SimpleNamespace(config=Settings())
    report = _Report([_Action("stale", 1, 40)])
    rows, suppressed = await teams_agent.filter_new_actions(container, report, now=T0)
    assert len(rows) == 1 and suppressed == 0


async def test_pr_only_actions_do_not_collide_on_work_item_zero(repo):
    """They all reported id 0, so the first one silenced every other PR alert."""
    from ai_autopilot import teams_agent

    container = SimpleNamespace(
        config=Settings(alert_dedup_enabled=True), alert_state_repo=repo
    )
    report = _Report([
        _Action("blocked_pr", 0, 40, pr_id=11),
        _Action("blocked_pr", 0, 40, pr_id=22),
    ])
    rows, suppressed = await teams_agent.filter_new_actions(container, report, now=T0)
    assert len(rows) == 2 and suppressed == 0


def test_digest_names_why_a_line_came_back():
    from ai_autopilot import teams_agent

    action = _Action("stale", 1, 96, title="Sửa báo cáo")
    body = teams_agent.build_digest(
        SimpleNamespace(
            actions=[action], kpis=[], people=[], window_days=14, flow_is_partial=False
        ),
        Settings(), rows=[(action, "tăng từ 26 giờ")], now=T0,
    )
    assert "tăng từ 26 giờ" in body


def test_digest_says_how_much_it_held_back():
    """Silence a reader cannot account for reads as a broken integration."""
    from ai_autopilot import teams_agent

    action = _Action("stale", 1, 96)
    body = teams_agent.build_digest(
        SimpleNamespace(
            actions=[action, _Action("stale", 2, 30)], kpis=[], people=[],
            window_days=14, flow_is_partial=False,
        ),
        Settings(), rows=[(action, "")], suppressed=1, now=T0,
    )
    assert "1 cảnh báo đã báo trước đó" in body
    assert "(1/2)" in body       # new vs total, so the count is not misread as "only 1 open"


async def test_digest_can_be_switched_off_like_any_other_event():
    """It reaches Teams through the bot, not through AdoNotifier, so it needs its own
    gate — otherwise unticking "digest" on the Settings page visibly does nothing."""
    from ai_autopilot import teams_agent

    sent = []

    class _Storage:
        async def all_keys(self):
            sent.append("read")
            return []

    container = SimpleNamespace(
        config=Settings(alert_events="failed,error"), alert_state_repo=None
    )
    await teams_agent._send_digest(
        container=container, app=None, adapter=None, storage=_Storage(),
        message_factory=None, window_hours=24,
    )
    assert sent == []           # returned before it even looked for conversations


# ── chat commands ─────────────────────────────────────────────────────────────

def test_ack_and_snooze_commands_parse():
    """A broken regex here does not error — it falls through to the slow agent path and
    looks like the bot ignoring the message."""
    from ai_autopilot import teams_agent as ta

    assert ta._ACK_RE.match("/ack 5312").group(1) == "5312"
    assert ta._UNACK_RE.match("/unack 5312").group(1) == "5312"

    m = ta._SNOOZE_RE.match("/snooze 5312 7")
    assert (m.group(1), m.group(2)) == ("5312", "7")

    m = ta._SNOOZE_RE.match("/snooze 5312")      # days optional → config default
    assert (m.group(1), m.group(2)) == ("5312", None)


def test_alert_commands_are_case_insensitive_and_reject_junk():
    from ai_autopilot import teams_agent as ta

    assert ta._ACK_RE.match("/ACK 12")
    assert ta._ACK_RE.match("/ack") is None          # no id → falls through to usage help
    assert ta._ACK_RE.match("/ack abc") is None
    assert ta._SNOOZE_RE.match("/snooze 12 3 4") is None


def test_alert_commands_are_listed_in_help():
    """A command nobody is told about is a command nobody uses."""
    from ai_autopilot import teams_agent as ta

    text = ta._help_text(Settings())
    for cmd in ("/alerts", "/ack", "/snooze", "/unack"):
        assert cmd in text, cmd


# ── no sender may bypass the policy ───────────────────────────────────────────

def test_no_service_sends_straight_to_a_channel():
    """A guard against the bug this section fixed.

    Three services (scheduled loops, the reviewer nudge, the budget alarm) each built a
    NotificationMessage and iterated `channels` themselves, so no alert setting and no
    quiet window applied to any of them. Everything must go through AdoNotifier, which
    is the one place the policy is enforced — a rule applied at seven call sites is a
    rule that will be missed at the eighth.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "ai_autopilot"
    pattern = re.compile(r"\bawait\s+\w+\.send\(")
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        # notifications/* IS the channel layer; notifier.py holds the single fan-out
        # point; tracking.py keeps a fallback for construction without a notifier.
        if rel.startswith("notifications/") or rel in {"ado/notifier.py", "tracking.py"}:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{rel}:{n}")
    assert not offenders, (
        "these send to a channel directly, skipping the alert policy: " + ", ".join(offenders)
    )


async def test_the_reviewer_nudge_obeys_quiet_hours(repo):
    """The politeness feature was the one most likely to get the bot muted."""
    from ai_autopilot.ado.notifier import AdoNotifier

    cfg = Settings(
        timezone="Asia/Ho_Chi_Minh", notify_hours_start="08:00",
        notify_hours_end="18:00", notify_days="Mon,Tue,Wed,Thu,Fri",
    )
    sent = []

    class _Channel:
        name = "t"
        is_enabled = True

        async def send(self, message):
            sent.append(message)

    class _Hold:
        held = []

        async def hold(self, **kw):
            self.held.append(kw)
            return 0

    notifier = AdoNotifier(ado=None, config=cfg, channels=[_Channel()], hold_repo=_Hold())
    msg = NotificationMessage(
        work_item=WorkItemInfo(id=1), type=NotificationType.REMINDER
    )
    class _Quiet:
        enabled = True

        def is_quiet(self, now=None):
            return True

    notifier._quiet = _Quiet()
    await notifier.notify(msg)
    assert sent == []                      # held, not delivered
