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


# ── recovery: the agent wrote the file somewhere else, or only printed it ──────


def test_find_result_picks_up_a_per_repo_copy(tmp_path: Path):
    # "Do NOT edit anything outside these repos" → it wrote the file inside one.
    _write(tmp_path / "Backend-Fresh", 6, {
        "status": "completed",
        "artifacts": [{"repo": "Backend-Fresh", "pr_url": "https://pr/6"}],
    })
    r = rc.find_result(str(tmp_path), 6)
    assert r is not None and r.is_completed and r.pr_url == "https://pr/6"


def test_find_result_prefers_the_workspace_root(tmp_path: Path):
    _write(tmp_path, 7, {"status": "completed", "summary": "root"})
    _write(tmp_path / "Backend-Fresh", 7, {"status": "failed", "summary": "stray"})
    r = rc.find_result(str(tmp_path), 7)
    assert r is not None and r.summary == "root"


def test_clear_result_also_clears_stray_copies(tmp_path: Path):
    # A leftover nested file must not be read as THIS run's outcome.
    _write(tmp_path / "Backend-Fresh", 8, {"status": "completed", "summary": "old run"})
    rc.clear_result(str(tmp_path), 8)
    assert rc.find_result(str(tmp_path), 8) is None


def test_parse_result_text_recovers_a_printed_envelope():
    text = (
        "I opened the PR. Here is the result:\n"
        '```json\n{"status": "completed", "summary": "did it", '
        '"artifacts": [{"repo": "r", "branch": "b", "pr_url": "https://pr/9"}]}\n```\n'
    )
    r = rc.parse_result_text(text)
    assert r is not None and r.is_completed and r.pr_url == "https://pr/9"


def test_parse_result_text_takes_the_last_envelope():
    text = '{"status":"failed","summary":"first try"} then {"status":"completed","summary":"done"}'
    r = rc.parse_result_text(text)
    assert r is not None and r.summary == "done"


def test_parse_result_text_ignores_unrelated_json():
    assert rc.parse_result_text('config: {"repo": "x", "branch": "y"}') is None
    assert rc.parse_result_text("no json at all") is None
    assert rc.parse_result_text("") is None


def test_case_outcomes_are_read_from_the_result_file(tmp_path: Path):
    _write(tmp_path, 10, {
        "status": "completed", "summary": "ran them",
        "test_results": [
            {"title": "Filter by schedule", "outcome": "pass"},
            {"title": "Export 10k", "outcome": "fail", "note": "timeout"},
        ],
    })
    res = rc.read_result(str(tmp_path), 10)
    assert [(r.title, r.outcome, r.note) for r in res.test_results] == [
        ("Filter by schedule", "pass", ""),
        ("Export 10k", "fail", "timeout"),
    ]


def test_an_outcome_written_the_way_a_model_writes_it_is_still_read(tmp_path: Path):
    """Asked for one of three words, a model writes "passed", "OK" or "N/A" about as
    often as the exact token. Dropping those rows would report a green run that was
    not one."""
    _write(tmp_path, 11, {
        "status": "completed", "summary": "s",
        "test_results": [
            {"title": "a", "outcome": "Passed"},
            {"title": "b", "outcome": "FAILED"},
            {"title": "c", "outcome": "N/A"},
            {"title": "d", "result": "ok"},            # field named from memory
            {"case": "e", "status": "fail - timeout"},  # verdict is the first word
        ],
    })
    res = rc.read_result(str(tmp_path), 11)
    assert [r.outcome for r in res.test_results] == ["pass", "fail", "blocked", "pass", "fail"]
    assert res.test_results[-1].title == "e"


def test_an_unreadable_outcome_is_blocked_and_never_a_pass(tmp_path: Path):
    """The asymmetry is the point: read as a pass, an outcome nobody can parse hides
    exactly the case a reader is looking for."""
    _write(tmp_path, 12, {
        "status": "completed", "summary": "s",
        "test_results": [
            {"title": "a", "outcome": "???"},
            {"title": "b"},                 # no outcome at all
            "c",                            # a bare string is a title
        ],
    })
    res = rc.read_result(str(tmp_path), 12)
    assert [r.outcome for r in res.test_results] == ["blocked", "blocked", "blocked"]


def test_an_outcome_with_no_title_is_dropped(tmp_path: Path):
    """A row that names no case cannot be read by anyone."""
    _write(tmp_path, 13, {
        "status": "completed", "summary": "s",
        "test_results": [{"outcome": "fail", "note": "something broke"}, {"title": "real"}],
    })
    assert [r.title for r in rc.read_result(str(tmp_path), 13).test_results] == ["real"]


def test_a_result_file_with_no_outcomes_reads_as_none(tmp_path: Path):
    _write(tmp_path, 14, {"status": "completed", "summary": "s"})
    assert rc.read_result(str(tmp_path), 14).test_results == []
