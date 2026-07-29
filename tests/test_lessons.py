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
