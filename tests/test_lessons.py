"""Tests for the retrospective learning-loop store."""

from __future__ import annotations

from datetime import datetime

from ai_autopilot import lessons


def test_record_and_read_round_trip(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "Backend", ["[High] null check missing"], now=now)
    lessons.record_lessons(ws, "Backend", ["[Medium] add unit test"], now=now)
    got = lessons.read_lessons(ws, "Backend")
    assert got == ["[High] null check missing", "[Medium] add unit test"]  # newest last
    # A stored file with the date prefix exists and is human-readable.
    text = (tmp_path / ".autopilot" / "lessons" / "Backend.md").read_text(encoding="utf-8")
    assert "2026-07-29" in text and "null check missing" in text


def test_record_dedupes(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "Repo", ["same lesson"], now=now)
    lessons.record_lessons(ws, "Repo", ["same lesson", "new one"], now=now)
    assert lessons.read_lessons(ws, "Repo") == ["same lesson", "new one"]


def test_record_caps_history(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "Repo", [f"lesson {i}" for i in range(80)], now=now)
    kept = lessons.read_lessons(ws, "Repo", limit=100)
    assert len(kept) == 50 and kept[-1] == "lesson 79"  # oldest dropped, newest kept


def test_lessons_brief_empty_when_none(tmp_path):
    assert lessons.lessons_brief(str(tmp_path), ["Nope"]) == ""


def test_lessons_brief_lists_across_repos(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "BE", ["BE lesson"], now=now)
    lessons.record_lessons(ws, "FE", ["FE lesson"], now=now)
    brief = lessons.lessons_brief(ws, ["BE", "FE"])
    assert "Lessons from past runs" in brief
    assert "BE lesson" in brief and "FE lesson" in brief


def test_read_missing_is_empty(tmp_path):
    assert lessons.read_lessons(str(tmp_path), "ghost") == []
    assert lessons.read_lessons("", "x") == []  # no workspace → no-op


def test_brief_and_injected_count_agree(tmp_path):
    """The badge counts the SAME list the brief carries — no over-claiming."""
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "BE", [f"lesson {i}" for i in range(10)], now=now)
    picked = lessons.recent(ws, ["BE"], limit=3)
    brief = lessons.lessons_brief(ws, ["BE"], limit=3)
    assert picked == ["lesson 7", "lesson 8", "lesson 9"]        # newest 3
    assert all(p in brief for p in picked) and "lesson 6" not in brief


def test_zero_limit_records_but_injects_nothing(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "BE", ["a lesson"], now=datetime(2026, 7, 29))
    assert lessons.recent(ws, ["BE"], limit=0) == []
    assert lessons.lessons_brief(ws, ["BE"], limit=0) == ""
    assert lessons.read_lessons(ws, "BE") == ["a lesson"]        # still remembered


def test_entries_expose_date_and_repo_newest_first(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "BE", ["older"], now=datetime(2026, 7, 28))
    lessons.record_lessons(ws, "BE", ["newer"], now=datetime(2026, 7, 29))
    got = lessons.entries(ws, "BE")
    assert [e.text for e in got] == ["newer", "older"]
    assert got[0].date == "2026-07-29" and got[0].repo == "BE"
    assert lessons.list_repos(ws) == ["BE"]


def test_delete_removes_one_and_clear_removes_the_file(tmp_path):
    ws = str(tmp_path)
    now = datetime(2026, 7, 29)
    lessons.record_lessons(ws, "BE", ["keep me", "wrong lesson"], now=now)
    assert lessons.delete(ws, "BE", "wrong lesson") is True
    assert lessons.read_lessons(ws, "BE") == ["keep me"]
    assert lessons.delete(ws, "BE", "never stored") is False
    assert lessons.clear(ws, "BE") is True
    assert lessons.list_repos(ws) == []


def test_deleting_the_last_lesson_drops_the_repo(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "BE", ["only one"], now=datetime(2026, 7, 29))
    assert lessons.delete(ws, "BE", "only one") is True
    assert lessons.list_repos(ws) == []          # no ghost repo with an empty file


def test_per_day_counts_new_lessons(tmp_path):
    ws = str(tmp_path)
    lessons.record_lessons(ws, "BE", ["a", "b"], now=datetime(2026, 7, 28))
    lessons.record_lessons(ws, "FE", ["c"], now=datetime(2026, 7, 29))
    assert lessons.per_day(ws) == [("2026-07-28", 2), ("2026-07-29", 1)]


def test_repo_name_cannot_escape_the_lessons_dir(tmp_path):
    """A repo name is used to build a path — traversal must be neutralised."""
    ws = str(tmp_path)
    lessons.record_lessons(ws, "../../etc", ["nope"], now=datetime(2026, 7, 29))
    lessons_dir = tmp_path / ".autopilot" / "lessons"
    written = list(lessons_dir.glob("*.md"))
    assert len(written) == 1
    # Separators are stripped, so the name can never climb out of the lessons dir.
    assert written[0].parent == lessons_dir
    assert "/" not in written[0].name and "\\" not in written[0].name
