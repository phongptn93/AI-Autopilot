"""The Scheduled agents page: defining a loop without editing a file on the server.

These loops existed only in ``config.yaml``, so trying one cost a file edit and a
restart — which is why the four audits the page ships as presets had never been set up.
The page is tested for saving a whole loop in one row, and for refusing the two
mistakes that produce a loop which looks configured and never fires.
"""

from __future__ import annotations

import re

import yaml
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import ScheduledLoop, Settings


def _client(tmp_path, **overrides):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}", **overrides
    )
    return TestClient(create_app(settings))


def _agents(tmp_path, *names):
    """Write sub-agent definitions where the page looks for them."""
    agents_dir = tmp_path / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (agents_dir / f"{name}.md").write_text(f"# {name}", encoding="utf-8")
    return str(tmp_path)


def _agent_fields(page: str, row: int) -> list[str]:
    """Sub-agent checkbox values posted under ROW ``row``'s field name."""
    return re.findall(rf'name="loop_{row}_agents" value="([^"]+)"', page)


def _checked_agents(page: str, row: int) -> list[str]:
    """Which of row ``row``'s sub-agents render as ticked."""
    return re.findall(rf'name="loop_{row}_agents" value="([^"]+)"\s*checked>', page)


def test_the_page_shows_each_loop_with_its_mode_cadence_and_agents(tmp_path):
    workspace = _agents(tmp_path, "agent-pr-reviewer", "agent-security-reviewer")
    with _client(
        tmp_path,
        workspace_directory=workspace,
        scheduled_loops=[
            ScheduledLoop(name="code-review-daily", prompt="Review today",
                          cron="7 18 * * 1-5", mode="report",
                          agents=["agent-pr-reviewer"]),
        ],
    ) as client:
        page = client.get("/dashboard/loops").text

        assert "code-review-daily" in page and "7 18 * * 1-5" in page
        assert "loop_0_mode" in page and "loop_0_agents" in page
        # The workspace's agents are offered, not typed from memory. Asserted by NAME
        # and CHECKED state, not by the string appearing somewhere: every agent's name
        # is on this page anyway — in the chip list and again in the presets — so
        # "agent-pr-reviewer" in page was true even when nothing was picked.
        assert _checked_agents(page, 0) == ["agent-pr-reviewer"]
        # Presets are on the page — that is the point of it existing.
        assert "security-audit-weekly" in page


def test_saving_writes_the_loop_and_applies_it_live(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(cfg_file))
    workspace = _agents(tmp_path, "agent-pr-reviewer")
    with _client(tmp_path, workspace_directory=workspace) as client:
        r = client.post("/dashboard/loops", data={
            "loop_0_name": "code-review-daily",
            "loop_0_prompt": "Review yesterday's commits.",
            "loop_0_cron": "7 18 * * 1-5",
            "loop_0_interval": "",
            "loop_0_mode": "report",
            "loop_0_agents": ["agent-pr-reviewer"],
            "loop_0_project": "",
            "loop_0_repo": "/srv/repo",
            "loop_0_base": "main",
            "loop_0_enabled": "on",
            "loop_0_html": "on",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)

        loops = client.app.state.container.config.scheduled_loops
        assert len(loops) == 1
        saved_loop = loops[0]
        # Applied live as a MODEL, not a dict — the scheduler reads attributes.
        assert isinstance(saved_loop, ScheduledLoop)
        assert saved_loop.is_report is True
        assert saved_loop.agents == ["agent-pr-reviewer"]
        assert saved_loop.repo_path == "/srv/repo"

        saved = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert saved["scheduled_loops"][0]["cron"] == "7 18 * * 1-5"
        assert saved["scheduled_loops"][0]["mode"] == "report"


def test_a_loop_with_no_valid_cadence_is_refused_rather_than_saved(tmp_path, monkeypatch):
    """The failure with no symptom: enabled, listed, looks configured, never fires.
    The scheduler only warns about it once at startup."""
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    with _client(tmp_path) as client:
        r = client.post("/dashboard/loops", data={
            "loop_0_name": "broken",
            "loop_0_prompt": "Audit",
            "loop_0_cron": "not a cron",
            "loop_0_interval": "0",
            "loop_0_mode": "report",
            "loop_0_enabled": "on",
        }, follow_redirects=False)

        assert r.status_code in (302, 303)
        assert client.app.state.container.config.scheduled_loops == []


def test_a_disabled_loop_may_keep_a_blank_cadence(tmp_path, monkeypatch):
    """Refusing this would make "switch it off while I work out the schedule"
    impossible — and an off loop cannot fail to fire."""
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    with _client(tmp_path) as client:
        client.post("/dashboard/loops", data={
            "loop_0_name": "draft",
            "loop_0_prompt": "Audit",
            "loop_0_cron": "",
            "loop_0_interval": "0",
            "loop_0_mode": "report",
        }, follow_redirects=False)

        loops = client.app.state.container.config.scheduled_loops
        assert len(loops) == 1 and loops[0].enabled is False


def test_two_loops_of_one_name_are_refused(tmp_path, monkeypatch):
    """The name is the scheduler's job id, so a duplicate does not make two loops — the
    second replaces the first, and one row on the page silently never runs."""
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    with _client(tmp_path) as client:
        r = client.post("/dashboard/loops", data={
            "loop_0_name": "audit", "loop_0_prompt": "a", "loop_0_cron": "7 18 * * *",
            "loop_0_mode": "report", "loop_0_enabled": "on",
            "loop_1_name": "audit", "loop_1_prompt": "b", "loop_1_cron": "9 18 * * *",
            "loop_1_mode": "report", "loop_1_enabled": "on",
        }, follow_redirects=False)

        assert r.status_code in (302, 303)
        assert client.app.state.container.config.scheduled_loops == []


def test_a_blank_name_drops_the_row(tmp_path, monkeypatch):
    """How a loop is removed without a separate delete for every row."""
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    with _client(
        tmp_path,
        scheduled_loops=[ScheduledLoop(name="old", prompt="p", cron="7 18 * * *")],
    ) as client:
        client.post("/dashboard/loops", data={
            "loop_0_name": "",           # cleared → dropped
            "loop_1_name": "kept", "loop_1_prompt": "p", "loop_1_cron": "9 18 * * *",
            "loop_1_mode": "report", "loop_1_enabled": "on",
        }, follow_redirects=False)

        names = [le.name for le in client.app.state.container.config.scheduled_loops]
        assert names == ["kept"]


def test_delete_removes_one_loop_and_leaves_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    with _client(
        tmp_path,
        scheduled_loops=[
            ScheduledLoop(name="a", prompt="p", cron="7 18 * * *"),
            ScheduledLoop(name="b", prompt="p", cron="9 18 * * *"),
        ],
    ) as client:
        client.post("/dashboard/loops/delete", data={"name": "a"}, follow_redirects=False)

        names = [le.name for le in client.app.state.container.config.scheduled_loops]
        assert names == ["b"]


def test_an_agent_the_workspace_does_not_define_is_flagged_on_the_page(tmp_path):
    """A loop delegating to a name that does not exist still runs — it just does the
    whole job itself, which is not what it was set up to do."""
    workspace = _agents(tmp_path, "agent-pr-reviewer")
    with _client(
        tmp_path,
        workspace_directory=workspace,
        scheduled_loops=[
            ScheduledLoop(name="audit", prompt="p", cron="7 18 * * *", mode="report",
                          agents=["agent-pr-reviewer", "agent-that-left"]),
        ],
    ) as client:
        page = client.get("/dashboard/loops").text
        assert "agent-that-left" in page
        assert "not in this workspace" in page


def test_the_reports_page_says_where_to_start_when_empty(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/dashboard/reports").text
        assert "No audits yet" in page
        assert "/dashboard/loops" in page


def test_a_stored_report_renders_on_both_the_list_and_the_detail_page(tmp_path):
    """End to end through the real repository: parse → store → list → detail."""
    from ai_autopilot import reports

    with _client(tmp_path) as client:
        container = client.app.state.container
        summary, findings = reports.parse_findings(
            'Checked yesterday.\n\n```json\n{"summary": "one critical",'
            ' "findings": [{"severity": "critical", "title": "Missing auth check",'
            ' "file": "api/users.py", "line": 88, "detail": "anyone can read",'
            ' "agent": "agent-security-reviewer"}]}\n```'
        )
        report = reports.Report(
            loop="code-review-daily", summary=summary, findings=findings,
            body_md="Checked yesterday.", agents=["agent-security-reviewer"],
            duration_seconds=42.0,
        )
        report_id = _save(container, report)

        listing = client.get("/dashboard/reports").text
        assert "code-review-daily" in listing
        assert "one critical" in listing
        assert "critical 1" in listing

        detail = client.get(f"/dashboard/reports/{report_id}").text
        assert "Missing auth check" in detail
        assert "api/users.py:88" in detail
        assert "agent-security-reviewer" in detail
        # The machine-readable half is not shown twice.
        assert "```json" not in detail


def _save(container, report) -> int:
    """Run the async save from a sync test, on its own loop."""
    import asyncio

    return asyncio.run(container.loop_report_repo.save(report, html_path=""))


def test_a_loops_live_feed_key_is_accepted_by_the_activity_guard(tmp_path):
    """The guard holds feed keys to the shapes we mint, and this one was new: the
    executor wrote `loop-<slug>.activity.log` and the page read "" back from it."""
    from ai_autopilot.dashboard import _feed_key

    assert _feed_key("loop-code-review-daily") == "loop-code-review-daily"
    assert _feed_key("pr-2470") == "pr-2470" and _feed_key("8965") == "8965"
    # Still a path guard first: the charset is what makes it one.
    assert _feed_key("loop-../../etc/passwd") == ""
    assert _feed_key("../../secrets") == ""


def test_the_page_links_each_loop_to_its_own_live_feed(tmp_path):
    with _client(
        tmp_path,
        scheduled_loops=[ScheduledLoop(name="Code Review Daily", prompt="p",
                                       cron="7 18 * * *", mode="report")],
    ) as client:
        page = client.get("/dashboard/loops").text
        assert "/dashboard/activity/loop-code-review-daily" in page


def test_running_a_loop_that_is_already_running_says_so_instead_of_starting_it(tmp_path):
    with _client(
        tmp_path,
        scheduled_loops=[ScheduledLoop(name="busy", prompt="p", cron="7 18 * * *",
                                       mode="report")],
    ) as client:
        scheduler = client.app.state.loop_scheduler
        scheduler._running.add("busy")

        r = client.post("/dashboard/loops/run", data={"name": "busy"},
                        follow_redirects=False)

        assert r.status_code in (302, 303)
        assert r.cookies.get("autopilot_flash") == "loop_busy"


def test_each_row_posts_its_sub_agents_under_its_own_row_index(tmp_path):
    """The bug this exists for: Jinja's `loop` names the INNERMOST loop, so inside the
    sub-agent picker `loop.index0` counted AGENTS, not rows. Every chip in every row was
    posted as `loop_<agentIndex>_agents`, so on save a row read back whichever agent sat
    at its own index — usually none — and the picks disappeared. Two rows and three
    agents is the smallest shape that tells the two numberings apart."""
    workspace = _agents(tmp_path, "agent-pr-reviewer", "agent-security-reviewer",
                        "agent-test-writer")
    with _client(
        tmp_path,
        workspace_directory=workspace,
        scheduled_loops=[
            ScheduledLoop(name="review", prompt="p", cron="7 18 * * *", mode="report",
                          agents=["agent-pr-reviewer"]),
            ScheduledLoop(name="security", prompt="p", cron="23 2 * * 6", mode="report",
                          agents=["agent-security-reviewer", "agent-test-writer"]),
        ],
    ) as client:
        page = client.get("/dashboard/loops").text

        # Every row offers every agent, under ITS OWN name — three chips each, not one
        # chip per agent index spread across the rows.
        assert _agent_fields(page, 0) == [
            "agent-pr-reviewer", "agent-security-reviewer", "agent-test-writer"]
        assert _agent_fields(page, 1) == _agent_fields(page, 0)
        # The blank row is index 2 and offers them too.
        assert _agent_fields(page, 2) == _agent_fields(page, 0)

        # …and each row ticks only its own.
        assert _checked_agents(page, 0) == ["agent-pr-reviewer"]
        assert _checked_agents(page, 1) == [
            "agent-security-reviewer", "agent-test-writer"]
        assert _checked_agents(page, 2) == []


def test_sub_agents_survive_a_save_and_come_back_ticked(tmp_path, monkeypatch):
    """The round trip a person actually performs: tick, save, look. Storing them was
    already tested; that the PAGE shows them again was not, which is how the mismatch
    between what the form emitted and what the handler read stayed invisible."""
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "config.yaml"))
    workspace = _agents(tmp_path, "agent-pr-reviewer", "agent-security-reviewer")
    with _client(tmp_path, workspace_directory=workspace) as client:
        client.post("/dashboard/loops", data={
            "loop_0_name": "code-review-daily",
            "loop_0_prompt": "Review today",
            "loop_0_cron": "7 18 * * 1-5",
            "loop_0_mode": "report",
            "loop_0_agents": ["agent-pr-reviewer", "agent-security-reviewer"],
            "loop_0_enabled": "on",
        }, follow_redirects=False)

        page = client.get("/dashboard/loops").text
        assert _checked_agents(page, 0) == [
            "agent-pr-reviewer", "agent-security-reviewer"]
        assert "None picked" not in page.split('name="loop_1_name"')[0]


def test_the_in_flight_page_names_the_role_a_run_is_for(tmp_path):
    """"interactive:autopilot-9004" names the console, not the work. The page built to
    answer "what is it doing" could not say whether a run was a QC check or the whole
    pipeline — so the role is recorded when the run opens and shown here."""
    import asyncio

    from ai_autopilot.config import SdlcRole
    from ai_autopilot.models import WorkItemInfo

    with _client(
        tmp_path,
        sdlc_roles={"qc": SdlcRole(stages=["test"], waits_in="Ready for Testing")},
    ) as client:
        repo = client.app.state.container.execution_repo
        item = WorkItemInfo(id=9004, title="Export excel", work_item_type="Task",
                            project="TLCL-DxFac")
        asyncio.run(repo.start_execution(item, "interactive:autopilot-9004", profile="qc"))

        page = client.get("/dashboard/now").text
        assert "interactive:autopilot-9004" in page
        assert ">qc<" in page          # the role, named
        assert "test" in page          # …and the steps it stands for


def test_a_run_with_no_role_says_so_rather_than_leaving_a_blank(tmp_path):
    """Blank would read as "unknown"; the truth is "the whole item, not scoped to a
    role", which is a different and useful thing to know."""
    import asyncio

    from ai_autopilot.models import WorkItemInfo

    with _client(tmp_path) as client:
        repo = client.app.state.container.execution_repo
        asyncio.run(repo.start_execution(
            WorkItemInfo(id=8107, title="t", work_item_type="Task"), "agent"
        ))

        page = client.get("/dashboard/now").text
        assert "cả work item" in page


def test_a_loop_that_cannot_run_says_so_on_its_own_row(tmp_path, monkeypatch):
    """The rule was in help text under the repo box, left for the reader to apply to
    their own row — so a loop that stops on its first line every night looked exactly
    like one that works."""
    from ai_autopilot.config import ScheduledLoop

    with _client(
        tmp_path,
        repo_working_directory="",
        scheduled_loops=[ScheduledLoop(name="code-review-daily", prompt="p", cron="7 18 * * *")],
    ) as client:
        page = client.get("/dashboard/loops").text
    assert "Chưa chạy được" in page
    assert "chưa có repo" in page


def test_a_working_loop_carries_no_blocker_banner(tmp_path, monkeypatch):
    from ai_autopilot.config import ScheduledLoop

    with _client(
        tmp_path,
        scheduled_loops=[ScheduledLoop(name="ok-loop", prompt="p", cron="7 18 * * *",
                                       repo_path=str(tmp_path))],
    ) as client:
        page = client.get("/dashboard/loops").text
    assert "Chưa chạy được" not in page


def test_run_now_refuses_a_loop_that_would_stop_on_its_first_line(tmp_path, monkeypatch):
    """Saying "started" and then stopping is what made this look broken rather than
    unconfigured."""
    from ai_autopilot.config import ScheduledLoop

    with _client(
        tmp_path,
        repo_working_directory="",
        scheduled_loops=[ScheduledLoop(name="code-review-daily", prompt="p", cron="7 18 * * *")],
    ) as client:
        page = client.post(
            "/dashboard/loops/run", data={"name": "code-review-daily"}, follow_redirects=True
        ).text
    assert "chưa chạy được" in page.lower()
    assert "Đã chạy ngay" not in page


def test_each_row_offers_the_repos_of_its_own_workspace(tmp_path):
    """A loop bound to another project runs in THAT project's workspace, so offering the
    default workspace's repo names would name repos it cannot reach."""
    from ai_autopilot.config import ScheduledLoop, WorkspaceConfig

    root, other = tmp_path / "root", tmp_path / "other"
    for ws, repo in ((root, "RootRepo"), (other, "OtherRepo")):
        (ws / repo / ".git").mkdir(parents=True)
    with _client(
        tmp_path,
        workspace_directory=str(root),
        workspaces=[WorkspaceConfig(name="B", ado_projects=["ProjB"],
                                    workspace_directory=str(other))],
        scheduled_loops=[ScheduledLoop(name="l-b", prompt="p", cron="7 18 * * *",
                                       project="ProjB")],
    ) as client:
        page = client.get("/dashboard/loops").text
    assert "OtherRepo" in page          # its own workspace's repo is offered…
    assert "blank = OtherRepo" in page  # …and a single repo means the field can stay blank
