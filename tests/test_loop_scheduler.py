"""Tests for scheduled-loop trigger/branch helpers."""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ai_autopilot.config import ScheduledLoop
from ai_autopilot.services.loop_scheduler import _loop_branch, _trigger


def test_trigger_cron():
    loop = ScheduledLoop(name="deps", prompt="/update-deps", cron="0 6 * * 1")
    assert isinstance(_trigger(loop), CronTrigger)


def test_trigger_interval():
    loop = ScheduledLoop(name="x", prompt="/p", interval_minutes=30)
    assert isinstance(_trigger(loop), IntervalTrigger)


def test_trigger_invalid_cron_returns_none():
    loop = ScheduledLoop(name="x", prompt="/p", cron="not a cron")
    assert _trigger(loop) is None


def test_trigger_none_when_no_cadence():
    assert _trigger(ScheduledLoop(name="x", prompt="/p")) is None


def test_loop_branch_format():
    loop = ScheduledLoop(name="Dependency Sweeper", prompt="/update-deps")
    branch = _loop_branch(loop)
    assert branch.startswith("autopilot/loop/dependency-sweeper-")
    # trailing timestamp YYYYMMDD-HHMM
    stamp = branch.rsplit("-", 2)[-2:]
    assert len("".join(stamp)) == len("20260629") + len("1200")
