"""Flaky tests: the gate reruns a red suite once and records what did not repeat."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ai_autopilot import flaky_tests
from ai_autopilot.config import Settings
from ai_autopilot.execution import test_gate as tg

RED_AB = "FAILED tests/a.py::test_a\nFAILED tests/b.py::test_b\n"
RED_B = "FAILED tests/b.py::test_b\n"


def _gate(tmp_path, monkeypatch, outputs, **cfg):
    settings = Settings(dry_run=True, test_gate_enabled=True, test_command="pytest",
                        workspace_directory=str(tmp_path), **cfg)
    gate = tg.TestGate(settings)
    calls = []

    async def fake_shell(cmd, cwd, limit_s, env=None):
        calls.append(cmd)
        out = outputs[len(calls) - 1]
        if isinstance(out, BaseException):
            raise out
        return out

    async def fake_repo(_):
        return "orders-api"

    monkeypatch.setattr(gate, "_shell", fake_shell)
    monkeypatch.setattr(tg, "repo_name_for", fake_repo)
    return gate, calls


@pytest.mark.asyncio
async def test_all_failures_gone_on_rerun_passes_and_is_recorded(tmp_path, monkeypatch):
    gate, calls = _gate(tmp_path, monkeypatch, [(1, RED_AB), (0, "2 passed")])
    res = await gate.run(str(tmp_path))
    assert res.passed and len(calls) == 2
    assert res.flaky == ["test tests/a.py::test_a", "test tests/b.py::test_b"]
    assert "flaky" in res.summary
    recorded = flaky_tests.load(str(tmp_path))
    assert {f.signature for f in recorded} == set(res.flaky)
    assert all(f.repo == "orders-api" and f.count == 1 for f in recorded)


@pytest.mark.asyncio
async def test_a_failure_that_repeats_still_blocks(tmp_path, monkeypatch):
    gate, _ = _gate(tmp_path, monkeypatch, [(1, RED_AB), (1, RED_B)])
    res = await gate.run(str(tmp_path))
    assert not res.passed
    assert res.failures == ["test tests/b.py::test_b"]
    assert res.flaky == ["test tests/a.py::test_a"]
    assert "flaky" in res.summary


@pytest.mark.asyncio
async def test_a_rerun_that_cannot_finish_keeps_the_first_verdict(tmp_path, monkeypatch):
    gate, _ = _gate(tmp_path, monkeypatch, [(1, RED_AB), TimeoutError()])
    res = await gate.run(str(tmp_path))
    assert not res.passed and res.flaky == []
    assert len(res.failures) == 2
    assert flaky_tests.load(str(tmp_path)) == []


@pytest.mark.asyncio
async def test_rerun_can_be_switched_off(tmp_path, monkeypatch):
    gate, calls = _gate(tmp_path, monkeypatch, [(1, RED_AB)], test_gate_flaky_rerun=False)
    res = await gate.run(str(tmp_path))
    assert not res.passed and len(calls) == 1


@pytest.mark.asyncio
async def test_a_green_first_run_is_not_rerun(tmp_path, monkeypatch):
    gate, calls = _gate(tmp_path, monkeypatch, [(0, "ok")])
    res = await gate.run(str(tmp_path))
    assert res.passed and len(calls) == 1 and res.flaky == []


def test_ledger_counts_sightings_and_sorts_most_frequent_first(tmp_path):
    ws = str(tmp_path)
    t1 = datetime(2026, 11, 1, tzinfo=UTC)
    t2 = datetime(2026, 11, 20, tzinfo=UTC)
    flaky_tests.record(ws, "api", ["test x"], now=t1)
    flaky_tests.record(ws, "api", ["test x", "test y"], now=t2)
    rows = flaky_tests.load(ws)
    assert [(r.signature, r.count) for r in rows] == [("test x", 2), ("test y", 1)]
    assert rows[0].first_seen == t1 and rows[0].last_seen == t2
    data = json.loads((tmp_path / ".autopilot" / "flaky-tests.json").read_text("utf-8"))
    assert data["api"]["test x"]["count"] == 2


def test_a_corrupt_ledger_reads_as_empty_and_is_rewritten(tmp_path):
    path = tmp_path / ".autopilot" / "flaky-tests.json"
    path.parent.mkdir()
    path.write_text("{not json", encoding="utf-8")
    assert flaky_tests.load(str(tmp_path)) == []
    flaky_tests.record(str(tmp_path), "api", ["test z"])
    assert [r.signature for r in flaky_tests.load(str(tmp_path))] == ["test z"]


def test_no_workspace_records_nothing():
    flaky_tests.record("", "api", ["test z"])
    assert flaky_tests.load("") == []
