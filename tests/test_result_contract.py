"""Tests for the agent result contract (.autopilot/runs/<id>.json)."""

from __future__ import annotations

import json
from pathlib import Path

from ai_autopilot.execution import result_contract as rc


def _write(ws: Path, item_id: int, data: dict) -> None:
    runs = ws / ".autopilot" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{item_id}.json").write_text(json.dumps(data), encoding="utf-8")


def test_read_completed_with_pr(tmp_path: Path):
    _write(tmp_path, 1, {
        "status": "completed",
        "summary": "did it",
        "artifacts": [{"repo": "Backend-Fresh", "branch": "feature/x", "pr_url": "https://pr/1"}],
    })
    r = rc.read_result(str(tmp_path), 1)
    assert r is not None
    assert r.is_completed
    assert r.pr_url == "https://pr/1"
    assert r.artifacts[0].repo == "Backend-Fresh"


def test_missing_file_returns_none(tmp_path: Path):
    assert rc.read_result(str(tmp_path), 999) is None


def test_invalid_json_returns_none(tmp_path: Path):
    runs = tmp_path / ".autopilot" / "runs"
    runs.mkdir(parents=True)
    (runs / "5.json").write_text("not json", encoding="utf-8")
    assert rc.read_result(str(tmp_path), 5) is None


def test_needs_human_status(tmp_path: Path):
    _write(tmp_path, 2, {"status": "needs_human", "reason": "AC unclear"})
    r = rc.read_result(str(tmp_path), 2)
    assert r is not None and r.needs_human and not r.is_completed
    assert r.reason == "AC unclear"


def test_needs_human_via_flag_overrides_completed(tmp_path: Path):
    _write(tmp_path, 3, {"status": "completed", "needs_human": True, "reason": "hmm"})
    r = rc.read_result(str(tmp_path), 3)
    assert r is not None and r.needs_human and not r.is_completed


def test_clear_result_is_idempotent(tmp_path: Path):
    _write(tmp_path, 4, {"status": "completed"})
    rc.clear_result(str(tmp_path), 4)
    assert rc.read_result(str(tmp_path), 4) is None
    rc.clear_result(str(tmp_path), 4)  # again — must not raise
