"""Tests for the AI-native executor flow: result mapping + brief building."""

from __future__ import annotations

from ai_autopilot.config import Settings
from ai_autopilot.execution.auto_reviewer import AutoReviewer
from ai_autopilot.execution.claude_executor import ClaudeExecutor
from ai_autopilot.execution.result_contract import AgentResult, Artifact
from ai_autopilot.models import TaskCategory, WorkItemInfo


def _executor(**overrides) -> ClaudeExecutor:
    base = {"workspace_directory": r"C:\ws", "repo_working_directory": r"C:\ws\Backend-Fresh"}
    base.update(overrides)
    cfg = Settings(**base)
    return ClaudeExecutor(cfg, AutoReviewer(cfg))


def _item() -> WorkItemInfo:
    wi = WorkItemInfo(id=7, title="Add constraint", work_item_type="Task")
    wi.category = TaskCategory.BACKEND_TASK
    return wi


def test_map_completed_with_pr():
    ex = _executor()
    agent = AgentResult(
        status="completed",
        summary="done",
        artifacts=[Artifact(repo="Backend-Fresh", branch="feature/be/7-x", pr_url="https://pr")],
    )
    r = ex._result_from_agent(_item(), agent, "assisted")
    assert r.success and r.pr_url == "https://pr" and r.branch_name == "feature/be/7-x"


def test_map_needs_human():
    ex = _executor()
    agent = AgentResult(status="needs_human", needs_human=True, reason="AC ambiguous")
    r = ex._result_from_agent(_item(), agent, "assisted")
    assert not r.success and r.needs_human and r.error == "AC ambiguous"


def test_map_no_result_file_is_failure():
    ex = _executor()
    r = ex._result_from_agent(_item(), None, "assisted")
    assert not r.success and not r.needs_human and "no result file" in r.error.lower()


def test_map_completed_without_pr_is_failure_when_not_report():
    ex = _executor()
    agent = AgentResult(status="completed", summary="s", artifacts=[])
    r = ex._result_from_agent(_item(), agent, "assisted")
    assert not r.success  # claimed completed but produced no PR → don't trust it


def test_map_report_mode_completes_without_pr():
    ex = _executor()
    agent = AgentResult(status="completed", summary="planned", artifacts=[])
    r = ex._result_from_agent(_item(), agent, "report")
    assert r.success and r.pr_url is None


def test_build_brief_includes_contract_and_autonomy():
    ex = _executor()
    brief = ex._build_brief(
        _item(), ["Backend-Fresh", "Micro-Frontend"], autonomy="assisted", draft_pr=True
    )
    assert ".autopilot/runs/7.json" in brief
    assert "DRAFT" in brief
    assert "needs_human" in brief
    assert "Backend-Fresh" in brief
    assert "Micro-Frontend" in brief          # lists all allowed repos


def test_build_brief_includes_repo_descriptions():
    ex = _executor(repo_descriptions=["Backend-Fresh = .NET API", "Micro-Frontend = Angular FE"])
    brief = ex._build_brief(
        _item(), ["Backend-Fresh", "Micro-Frontend"], autonomy="assisted", draft_pr=True
    )
    assert "./Backend-Fresh — .NET API" in brief
    assert "./Micro-Frontend — Angular FE" in brief


def test_parse_repo_descriptions():
    from ai_autopilot.workspace import parse_repo_descriptions

    assert parse_repo_descriptions(["Backend-Fresh = .NET API", "no-desc", "Gitops: manifests"]) == {
        "backend-fresh": ".NET API",
        "gitops": "manifests",
    }


def test_allowed_repos_whitelist(tmp_path):
    # discovered repos are filtered by the configured whitelist
    for name in ("Backend-Fresh", "Micro-Frontend", "Secret"):
        (tmp_path / name / ".git").mkdir(parents=True)
    ex = _executor(workspace_directory=str(tmp_path), allowed_repos=["Backend-Fresh"])
    assert ex._allowed_repos(str(tmp_path)) == ["Backend-Fresh"]
    ex2 = _executor(workspace_directory=str(tmp_path))  # empty whitelist → all
    assert set(ex2._allowed_repos(str(tmp_path))) == {"Backend-Fresh", "Micro-Frontend", "Secret"}
