"""Scheduled loops: cadence helpers, and the split between building and reporting."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ai_autopilot.config import ScheduledLoop, Settings
from ai_autopilot.models import ExecutionResult
from ai_autopilot.services.loop_scheduler import LoopScheduler, _loop_branch, _trigger


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


# ── report loops ────────────────────────────────────────────────────────────
#
# A build loop is judged by its diff; a report loop is judged by what it found, and
# changing a file would be the defect. Expressed in the build-shaped loop, an audit was
# recorded as "No file changes produced" and then asked to open an empty PR — so these
# tests are mostly about the two paths staying apart.


class _FakeExecutor:
    def __init__(self, output: str = "", success: bool = True):
        self.audits: list[tuple] = []
        self.loops: list[tuple] = []
        self._output, self._success = output, success

    async def run_audit(self, name, prompt, repo, base, project=""):
        self.audits.append((name, prompt, repo, base, project))
        result = (ExecutionResult.ok(0, "audit", self._output) if self._success
                  else ExecutionResult.fail(0, "audit", "claude exploded"))
        result.duration_seconds = 1.5
        return result

    async def run_loop(self, *args, **kwargs):
        self.loops.append((args, kwargs))
        return ExecutionResult.ok(0, "loop", "built")


class _FakeReportRepo:
    def __init__(self):
        self.saved: list[tuple] = []

    async def save(self, report, html_path: str = "", error: str = ""):
        self.saved.append((report, html_path, error))
        return len(self.saved)


def _scheduler(tmp_path, executor, **cfg_kwargs):
    cfg = Settings(workspace_directory=str(tmp_path), **cfg_kwargs)
    reports_repo = _FakeReportRepo()
    container = SimpleNamespace(
        config=cfg,
        executor=executor,
        loop_report_repo=reports_repo,
        execution_repo=SimpleNamespace(
            start_execution=_noop_int, complete_execution=_noop,
        ),
        cost_tracker=SimpleNamespace(track=_noop),
        notifier=SimpleNamespace(notify=_noop),
    )
    svc = LoopScheduler.__new__(LoopScheduler)
    svc._c, svc._config = container, cfg
    svc._running = set()        # __init__ is bypassed; the run guard lives there
    svc._log = type("L", (), {
        "info": lambda *a, **k: None, "warning": lambda *a, **k: None,
    })()
    return svc, reports_repo


async def _noop(*_a, **_kw):
    return None


async def _noop_int(*_a, **_kw):
    return 1


async def test_a_report_loop_never_takes_the_build_path(tmp_path):
    """The bug in one line: an audit routed through run_loop is failed for the diff it
    was never supposed to produce."""
    executor = _FakeExecutor(output='ok\n\n```json\n{"summary":"s","findings":[]}\n```')
    svc, repo = _scheduler(tmp_path, executor)
    loop = ScheduledLoop(
        name="code-review-daily", prompt="Review today", cron="7 18 * * 1-5",
        mode="report", agents=["agent-pr-reviewer"], repo_path=str(tmp_path / "repo"),
    )

    await svc._run_loop(loop)

    assert executor.loops == []                 # no branch, no PR
    assert len(executor.audits) == 1
    name, prompt, used_repo, _base, _project = executor.audits[0]
    assert name == "code-review-daily"
    assert used_repo == str(tmp_path / "repo")
    # The prompt is assembled, not stored: a loop written before the contract existed
    # still comes back parseable.
    assert "`agent-pr-reviewer`" in prompt and "```json" in prompt
    assert "Review today" in prompt
    assert len(repo.saved) == 1


async def test_a_build_loop_is_untouched_by_the_report_path(tmp_path):
    executor = _FakeExecutor()
    svc, repo = _scheduler(tmp_path, executor)
    loop = ScheduledLoop(name="deps", prompt="/update-deps", cron="0 6 * * 1",
                         repo_path=str(tmp_path / "repo"))

    await svc._run_loop(loop)

    assert len(executor.loops) == 1 and executor.audits == []
    assert repo.saved == []                     # a build produces no audit row


async def test_findings_and_counts_reach_the_stored_report(tmp_path):
    executor = _FakeExecutor(output=(
        'Two things.\n\n```json\n{"summary": "two things", "findings": ['
        '{"severity": "critical", "title": "SQLi", "file": "db.py"},'
        '{"severity": "nit", "title": "typo"}]}\n```'
    ))
    svc, repo = _scheduler(tmp_path, executor)

    await svc._run_loop(ScheduledLoop(
        name="sec", prompt="Audit", cron="23 2 * * 6", mode="report",
        repo_path=str(tmp_path / "repo"), agents=["agent-security-reviewer"],
    ))

    report, html_path, _error = repo.saved[0]
    assert report.summary == "two things"
    assert report.worst == "critical"
    assert report.counts["critical"] == 1 and report.counts["low"] == 1
    assert report.agents == ["agent-security-reviewer"]
    assert Path(html_path).exists()             # the forwardable copy
    assert "SQLi" in Path(html_path).read_text(encoding="utf-8")


async def test_a_failed_audit_is_still_recorded(tmp_path):
    """"The nightly review did not run" is the fact with no other symptom — a silent
    failure looks exactly like a clean report."""
    executor = _FakeExecutor(success=False)
    svc, repo = _scheduler(tmp_path, executor)

    await svc._run_loop(ScheduledLoop(
        name="sec", prompt="Audit", cron="23 2 * * 6", mode="report",
        repo_path=str(tmp_path / "repo"),
    ))

    report, _html, error = repo.saved[0]
    assert report.status == "failed"
    assert "claude exploded" in error


async def test_html_is_skipped_without_a_workspace_and_the_row_still_saves(tmp_path):
    """No workspace means no place to put a file — the same rule the activity feed
    follows. It must not cost the report itself."""
    executor = _FakeExecutor(output="fine")
    svc, repo = _scheduler(tmp_path, executor)
    svc._config.workspace_directory = ""

    await svc._run_loop(ScheduledLoop(
        name="sec", prompt="Audit", cron="23 2 * * 6", mode="report",
        repo_path=str(tmp_path / "repo"),
    ))

    report, html_path, _error = repo.saved[0]
    assert html_path == "" and report.status == "success"


async def test_run_now_only_runs_an_enabled_loop_of_that_name(tmp_path):
    executor = _FakeExecutor(output="fine")
    svc, _repo = _scheduler(
        tmp_path,
        executor,
        scheduled_loops=[
            ScheduledLoop(name="on", prompt="p", cron="7 18 * * *", mode="report",
                          repo_path=str(tmp_path / "repo")),
            ScheduledLoop(name="off", prompt="p", cron="7 18 * * *", mode="report",
                          repo_path=str(tmp_path / "repo"), enabled=False),
        ],
    )

    assert await svc.run_now("on") is True
    assert await svc.run_now("off") is False        # disabled stays disabled
    assert await svc.run_now("nope") is False
    assert [a[0] for a in executor.audits] == ["on"]


def test_mode_defaults_to_the_build_loop_that_already_existed():
    """Every loop written before report mode must read back unchanged."""
    loop = ScheduledLoop(name="deps", prompt="/update-deps")
    assert loop.mode == "pr" and loop.is_report is False
    assert loop.agents == [] and loop.report_html is True


async def test_one_loop_does_not_run_twice_at_once(tmp_path):
    """APScheduler's max_instances covers the SCHEDULED path only; the page's Run does
    not go through it. Two presses would put two agents on one repo."""
    executor = _FakeExecutor(output="fine")
    svc, repo = _scheduler(tmp_path, executor)
    loop = ScheduledLoop(name="sec", prompt="Audit", cron="23 2 * * 6", mode="report",
                         repo_path=str(tmp_path / "repo"))

    svc._running.add("sec")             # as if a run were already in flight
    await svc._run_loop(loop)

    assert executor.audits == [] and repo.saved == []

    svc._running.discard("sec")
    await svc._run_loop(loop)
    assert len(executor.audits) == 1
    # The guard releases itself — a skipped or finished run must not wedge the loop.
    assert svc.is_running("sec") is False


async def test_a_failed_run_still_releases_the_guard(tmp_path):
    """Otherwise one crash silently retires the loop until the service restarts."""
    class _Boom(_FakeExecutor):
        async def run_audit(self, *_a, **_kw):
            raise RuntimeError("claude is on fire")

    svc, _repo = _scheduler(tmp_path, _Boom())
    loop = ScheduledLoop(name="sec", prompt="Audit", cron="23 2 * * 6", mode="report",
                         repo_path=str(tmp_path / "repo"))

    with pytest.raises(RuntimeError):
        await svc._run_loop(loop)
    assert svc.is_running("sec") is False


def test_a_loops_feed_key_is_its_own():
    """item id 0 for every loop meant one shared feed that each overwrote in turn."""
    from ai_autopilot import activity

    assert activity.loop_key("Code Review Daily") == "loop-code-review-daily"
    assert activity.loop_key("sec") != activity.loop_key("perf")


def _repo(root, name):
    """A workspace subfolder that looks like a git repo to discover_repos."""
    path = root / name
    (path / ".git").mkdir(parents=True)
    return path


def test_a_bare_repo_name_resolves_inside_the_workspace(tmp_path):
    """The field's placeholder promised "the workspace's repo", and a bare name typed
    into it went to git as a path relative to the SERVICE's process directory — which is
    nobody's intention and fails somewhere else entirely."""
    from ai_autopilot.config import ScheduledLoop, Settings
    from ai_autopilot.services.loop_scheduler import loop_repo

    ws = tmp_path / "ws"
    _repo(ws, "Backend-Fresh")
    cfg = Settings(workspace_directory=str(ws))
    loop = ScheduledLoop(name="l", prompt="p", cron="7 18 * * *", repo_path="Backend-Fresh")

    assert loop_repo(loop, cfg) == str(ws / "Backend-Fresh")


def test_an_absolute_repo_path_is_left_alone(tmp_path):
    from ai_autopilot.config import ScheduledLoop, Settings
    from ai_autopilot.services.loop_scheduler import loop_repo

    loop = ScheduledLoop(name="l", prompt="p", cron="7 18 * * *", repo_path=str(tmp_path))
    assert loop_repo(loop, Settings(workspace_directory=str(tmp_path / "ws"))) == str(tmp_path)


def test_a_workspace_with_one_repo_needs_no_repo_field(tmp_path):
    """"Blank = the workspace's repo" is a promise the code can actually keep when there
    is exactly one — which is the common single-repo workspace."""
    from ai_autopilot.config import ScheduledLoop, Settings
    from ai_autopilot.services.loop_scheduler import loop_blockers, loop_repo

    ws = tmp_path / "ws"
    _repo(ws, "OnlyRepo")
    cfg = Settings(workspace_directory=str(ws))
    loop = ScheduledLoop(name="l", prompt="p", cron="7 18 * * *")

    assert loop_repo(loop, cfg) == str(ws / "OnlyRepo")
    assert loop_blockers(loop, cfg) == []


def test_several_repos_ask_which_one_and_name_them(tmp_path):
    """Guessing would be worse than saying so — and "fill in a path" is not an
    instruction anyone can act on without going to look the names up."""
    from ai_autopilot.config import ScheduledLoop, Settings
    from ai_autopilot.services.loop_scheduler import loop_blockers, loop_repo

    ws = tmp_path / "ws"
    _repo(ws, "Backend-Fresh")
    _repo(ws, "Micro-Frontend")
    cfg = Settings(workspace_directory=str(ws))
    loop = ScheduledLoop(name="l", prompt="p", cron="7 18 * * *")

    assert loop_repo(loop, cfg) == ""
    blocker = loop_blockers(loop, cfg)[0]
    assert "2 repo" in blocker and "Backend-Fresh" in blocker and "Micro-Frontend" in blocker


def test_the_audit_prompt_hands_the_change_set_over_already_computed():
    """The run kept dying at 18 seconds: its first act was to discover what changed, and
    the obvious command for that — a log WITH patches over a day of a busy repo — is
    larger than the context. Telling it "don't run broad commands" only works if it does
    not need to."""
    from ai_autopilot.reports import audit_prompt

    out = audit_prompt("review the last day", [], "C:/ws/Backend-Fresh",
                       digest="Commits:\nabc123 fix(x): thing")
    assert "ALREADY COMPUTED" in out
    assert "abc123 fix(x): thing" in out
    assert "do not re-run broad git" in out


def test_a_repo_with_no_digest_still_gets_a_usable_prompt():
    """A shallow clone, or nothing in the window: the prompt carries no digest rather
    than a heading with nothing under it."""
    from ai_autopilot.reports import audit_prompt

    out = audit_prompt("review the last day", [], "C:/ws/Backend-Fresh", digest="")
    assert "ALREADY COMPUTED" not in out
    assert "review the last day" in out


def test_the_digest_says_how_much_it_left_out():
    """A digest that silently truncated would have the reviewer judge a change set that
    is not the whole one, and report it as if it were."""
    from ai_autopilot.services.loop_scheduler import _clip

    clipped = _clip("\n".join(f"line {i}" for i in range(250)), 100)
    assert clipped.count("\n") == 100        # 100 lines + the note
    assert "+150" in clipped


async def test_the_digest_is_computed_with_bounded_git_commands(tmp_path):
    """Bounded HERE — the point is that the expensive command is never issued at all."""
    from types import SimpleNamespace

    from ai_autopilot.services.loop_scheduler import LoopScheduler

    issued: list[list[str]] = []

    async def fake_git(args, repo, check=True):
        issued.append(list(args))
        return "abc123 fix: thing" if args[0] == "log" else " src/a.py | 2 +-"

    svc = LoopScheduler.__new__(LoopScheduler)
    svc._c = SimpleNamespace(executor=SimpleNamespace(_git=fake_git))
    svc._log = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)

    digest = await svc._change_digest(str(tmp_path), "main")

    assert "abc123 fix: thing" in digest and "src/a.py" in digest
    assert all("-p" not in args and "--patch" not in args for args in issued)
    assert ["log", "--since=24 hours ago", "--oneline", "-n", "100"] in issued
