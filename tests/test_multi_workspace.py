"""Executor workspace scoping: a run reads the settings of ITS work item's workspace.

Every path in ``ClaudeExecutor`` reads ``self._config``. These tests pin that the read
follows the work item — including under concurrency, where two runs in different
workspaces must not observe each other's scope.
"""

from __future__ import annotations

import asyncio

from ai_autopilot.config import Settings
from ai_autopilot.doctor import ERROR, OK, WARN, check_projects, check_workspaces
from ai_autopilot.execution.claude_executor import ClaudeExecutor
from ai_autopilot.models import WorkItemInfo


def _executor(**over) -> ClaudeExecutor:
    cfg = Settings(ado_organization="https://dev.azure.com/org", **over)
    return ClaudeExecutor(cfg, reviewer=None)


def _multi_workspace() -> ClaudeExecutor:
    return _executor(
        ado_project="Khatoco",
        workspace_directory="C:/ws/khatoco",
        base_branch="development",
        workspace_map=["CMMS = C:/ws/cmms | base=main"],
    )


def test_outside_a_run_the_root_config_is_used():
    ex = _multi_workspace()
    assert ex._config.workspace_directory == "C:/ws/khatoco"


def test_scope_switches_the_workspace_for_the_block():
    ex = _multi_workspace()
    with ex.workspace_scope("CMMS"):
        assert ex._config.workspace_directory == "C:/ws/cmms"
        assert ex._config.base_branch == "main"
    assert ex._config.workspace_directory == "C:/ws/khatoco"


def test_scope_is_restored_when_the_block_raises():
    ex = _multi_workspace()
    try:
        with ex.workspace_scope("CMMS"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert ex._config.workspace_directory == "C:/ws/khatoco"


def test_unknown_project_falls_back_to_the_root_workspace():
    ex = _multi_workspace()
    with ex.workspace_scope("Unmapped"):
        assert ex._config.workspace_directory == "C:/ws/khatoco"


def test_concurrent_runs_do_not_leak_each_others_workspace():
    # The real risk of a context-scoped config: max_concurrent > 1 running two items
    # from different projects. Each task must see only its own.
    ex = _multi_workspace()
    seen: dict[str, str] = {}

    async def run(project: str, key: str, delay: float) -> None:
        with ex.workspace_scope(project):
            await asyncio.sleep(delay)          # force the two runs to interleave
            seen[key] = ex._config.workspace_directory

    async def main() -> None:
        await asyncio.gather(
            run("CMMS", "cmms", 0.02),
            run("Khatoco", "khatoco", 0.01),
        )

    asyncio.run(main())
    assert seen == {"cmms": "C:/ws/cmms", "khatoco": "C:/ws/khatoco"}


def test_entry_points_scope_themselves_from_the_work_item():
    ex = _multi_workspace()
    captured: dict[str, str] = {}

    async def fake_run_in_workspace(**kw):
        captured["workspace"] = ex._config.workspace_directory
        captured["base_branch"] = kw["base_branch"]

        class _Result:
            lessons_injected = 0
        return _Result()

    ex._run_in_workspace = fake_run_in_workspace
    ex._resolve_repo = lambda item: ("C:/ws/cmms/Api", ex._config.base_branch)
    asyncio.run(ex.execute(WorkItemInfo(id=1, title="t", project="CMMS"), "/implement"))
    assert captured == {"workspace": "C:/ws/cmms", "base_branch": "main"}


# ── doctor ──────────────────────────────────────────────────────────────────


def test_doctor_is_silent_without_extra_projects_or_workspaces():
    cfg = Settings(ado_organization="o", ado_project="Khatoco")
    assert check_projects(cfg) == []
    assert check_workspaces(cfg) == []


def test_doctor_reports_a_workspace_map_that_parsed_to_nothing():
    cfg = Settings(ado_project="Khatoco", workspace_map=["Khatoco C:/ws"])  # missing '='
    findings = check_workspaces(cfg)
    assert [f.level for f in findings] == [ERROR]


def test_doctor_reports_a_missing_workspace_directory(tmp_path):
    cfg = Settings(ado_project="Khatoco", workspace_map=[f"CMMS = {tmp_path}/nope"])
    assert any(f.level == ERROR for f in check_workspaces(cfg))


def test_doctor_warns_when_the_workspace_has_no_claude_folder(tmp_path):
    cfg = Settings(ado_project="Khatoco", workspace_map=[f"CMMS = {tmp_path}"])
    assert any(f.level == WARN for f in check_workspaces(cfg))


def test_doctor_passes_a_complete_workspace(tmp_path):
    (tmp_path / ".claude").mkdir()
    cfg = Settings(ado_project="Khatoco", workspace_map=[f"CMMS = {tmp_path}"])
    assert [f.level for f in check_workspaces(cfg)] == [OK]


def test_doctor_lists_every_polled_project():
    cfg = Settings(ado_project="Khatoco", ado_projects=["CMMS"])
    findings = check_projects(cfg)
    assert findings[0].level == OK
    assert "CMMS" in findings[0].detail


def test_doctor_warns_about_projects_sharing_the_root_workspace(tmp_path):
    (tmp_path / ".claude").mkdir()
    cfg = Settings(
        ado_project="Khatoco", ado_projects=["IIoT"],
        workspace_map=[f"CMMS = {tmp_path}"],
    )
    warnings = [f for f in check_projects(cfg) if f.level == WARN]
    assert warnings and "IIoT" in warnings[0].detail
