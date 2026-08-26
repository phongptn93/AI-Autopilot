"""The History page must show what a run cost and which model it ran on.

An end-to-end render rather than a helper test: the columns exist to be READ, and a
template that silently drops a field still passes every unit test around it.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.execution.claude_client import ClaudeRun, apply_usage
from ai_autopilot.models import ExecutionResult, WorkItemInfo


@pytest.fixture
def client(tmp_path) -> TestClient:
    settings = Settings(
        dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'h.db'}"
    )
    with TestClient(create_app(settings)) as c:
        yield c


async def _seed(app, with_usage: bool) -> None:
    repo = app.state.container.execution_repo
    item = WorkItemInfo(id=4242, title="Sửa báo cáo tồn kho")
    record_id = await repo.start_execution(item, "crud-full-stack")
    result = ExecutionResult(work_item_id=4242, success=True, skill_used="crud-full-stack")
    if with_usage:
        apply_usage(result, ClaudeRun(
            input_tokens=1200, output_tokens=340, cache_read_tokens=88_000,
            cache_creation_tokens=500, cost_usd=0.6321,
            models={"claude-opus-5": 90_040},
        ))
    await repo.complete_execution(record_id, result)


def test_history_shows_the_model_and_the_token_breakdown(client: TestClient):
    client.portal.call(_seed, client.app, True)
    html = client.get("/dashboard/history").text

    assert "<th>Model</th>" in html
    assert "opus-5" in html                 # trimmed label in the cell
    assert "claude-opus-5" in html          # full id in the tooltip
    assert "90,040" in html                 # total tokens
    assert "340 out" in html                # the half that scales with cost
    assert "$0.6321" in html                # what it actually cost
    assert "Input: 1,200" in html and "Cache read: 88,000" in html


def test_a_run_with_no_usage_renders_dashes_not_zeroes(client: TestClient):
    """A 0 would read as "this run was free", which is the one wrong answer here."""
    client.portal.call(_seed, client.app, False)
    html = client.get("/dashboard/history").text

    assert "<th>Model</th>" in html
    assert "$0.0000" not in html
    assert "0 out" not in html
