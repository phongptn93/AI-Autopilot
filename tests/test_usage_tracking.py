"""Token and model accounting, from the SDK result through to the History row.

A work item is usually several Claude calls, and History reports per ITEM — so the
question these cover is whether the sum is right, whether it says which model spent it,
and whether a run that reported nothing stays honestly unknown instead of reading as
free.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from ai_autopilot.dashboard import _model_label, _tokens_detail
from ai_autopilot.data.database import Database
from ai_autopilot.data.repository import ExecutionRepository
from ai_autopilot.execution.claude_client import ClaudeRun, Usage, apply_usage
from ai_autopilot.models import ExecutionResult, WorkItemInfo


def _run(inp=0, out=0, cr=0, cc=0, cost=None, models=None) -> ClaudeRun:
    return ClaudeRun(
        input_tokens=inp, output_tokens=out, cache_read_tokens=cr,
        cache_creation_tokens=cc, cost_usd=cost, models=dict(models or {}),
    )


# ── aggregation ───────────────────────────────────────────────────────────────

def test_usage_sums_every_token_bucket():
    u = Usage().add(_run(100, 50, 10, 5), _run(200, 25, 0, 0))
    assert (u.input_tokens, u.output_tokens) == (300, 75)
    assert (u.cache_read_tokens, u.cache_creation_tokens) == (10, 5)
    assert u.tokens == 390


def test_the_busiest_model_is_the_one_reported():
    u = Usage().add(
        _run(10, 5, models={"claude-haiku-4-5-20251001": 15}),
        _run(900, 100, models={"claude-opus-5": 1000}),
    )
    assert u.model == "claude-opus-5"
    assert u.model_label == "claude-opus-5 +1"


def test_a_single_model_is_labelled_without_a_suffix():
    assert Usage().add(_run(1, 1, models={"claude-opus-5": 2})).model_label == "claude-opus-5"


def test_unknown_cost_stays_unknown_rather_than_zero():
    """None means the CLI did not price the run. Reporting $0.0000 would be a lie."""
    assert Usage().add(_run(10, 5)).cost_usd is None


def test_costs_are_summed_once_any_are_known():
    u = Usage().add(_run(cost=0.02), _run(cost=None), _run(cost=0.005))
    assert u.cost_usd == pytest.approx(0.025)


def test_apply_usage_writes_the_whole_breakdown_onto_the_result():
    result = ExecutionResult(work_item_id=1, success=True)
    apply_usage(result, _run(100, 50, 10, 5, cost=0.01, models={"claude-opus-5": 165}))
    assert result.cost_tokens == 165
    assert result.model_used == "claude-opus-5"
    assert (result.input_tokens, result.output_tokens) == (100, 50)
    assert (result.cache_read_tokens, result.cache_creation_tokens) == (10, 5)
    assert result.cost_usd == pytest.approx(0.01)


def test_adding_nothing_is_harmless():
    """SDLC paths pass a run that may be None when a stage never got to run."""
    u = Usage().add(None)
    assert u.tokens == 0 and u.model_label == ""


def test_claude_run_totals_include_cache():
    assert _run(100, 50, 10, 5).total_tokens == 165


# ── persistence ───────────────────────────────────────────────────────────────

@pytest.fixture
async def repo():
    db = Database(f"sqlite+aiosqlite:///{os.path.join(tempfile.mkdtemp(), 'u.db')}")
    await db.create_all()
    yield ExecutionRepository(db)
    await db.dispose()


async def _record(repo) -> int:
    item = WorkItemInfo(id=42, title="Sửa báo cáo tồn kho")
    return await repo.start_execution(item, "crud-full-stack")


async def test_the_breakdown_survives_the_round_trip(repo):
    record_id = await _record(repo)
    result = ExecutionResult(work_item_id=42, success=True, skill_used="x")
    apply_usage(result, _run(1000, 200, 50_000, 300, cost=0.42,
                             models={"claude-opus-5": 51_500}))
    await repo.complete_execution(record_id, result)

    rows = await repo.for_item(42)
    row = rows[0]
    assert row.cost_tokens == 51_500
    assert row.model_used == "claude-opus-5"
    assert row.input_tokens == 1000
    assert row.output_tokens == 200
    assert row.cache_read_tokens == 50_000
    assert row.cost_usd == pytest.approx(0.42)


async def test_a_run_that_reported_no_usage_keeps_nulls(repo):
    """A 0 would be read as "this run was free" — the one answer a cost table must
    never give when it does not know."""
    record_id = await _record(repo)
    await repo.complete_execution(
        record_id, ExecutionResult(work_item_id=42, success=True, skill_used="x")
    )
    row = (await repo.for_item(42))[0]
    assert row.model_used is None
    assert row.cost_usd is None
    assert row.input_tokens is None


# ── presentation ──────────────────────────────────────────────────────────────

def test_model_label_drops_the_vendor_prefix_and_release_date():
    assert _model_label("claude-haiku-4-5-20251001") == "haiku-4-5"
    assert _model_label("claude-opus-5") == "opus-5"


def test_model_label_keeps_the_multi_model_marker():
    assert _model_label("claude-opus-5 +1") == "opus-5 +1"


def test_model_label_of_nothing_is_empty():
    assert _model_label(None) == "" and _model_label("") == ""


def test_tokens_detail_lists_only_what_is_known():
    class _Row:
        input_tokens, output_tokens = 1000, 200
        cache_read_tokens, cache_creation_tokens = 0, None
        cost_usd, model_used = 0.42, "claude-opus-5"

    detail = _tokens_detail(_Row())
    assert "Input: 1,000" in detail and "Output: 200" in detail
    assert "Cache read" not in detail          # zero is not worth a line
    assert "$0.4200" in detail and "claude-opus-5" in detail


def test_tokens_detail_is_empty_for_a_legacy_row():
    """Rows written before these columns existed must render as a bare number."""
    class _Row:
        input_tokens = output_tokens = None
        cache_read_tokens = cache_creation_tokens = None
        cost_usd = model_used = None

    assert _tokens_detail(_Row()) == ""
