"""Tests for the live agent activity feed."""

from __future__ import annotations

from ai_autopilot import activity


def test_append_read_clear(tmp_path):
    ws = str(tmp_path)
    activity.append(ws, 1, "🚀 started")
    activity.append(ws, 1, "🔧 Edit · file.cs")
    out = activity.read(ws, 1)
    assert "started" in out and "Edit" in out
    activity.clear(ws, 1)
    assert activity.read(ws, 1) == ""


def test_read_missing_returns_empty(tmp_path):
    assert activity.read(str(tmp_path), 99) == ""


def test_tool_summary_picks_telling_arg():
    assert "file.cs" in activity.tool_summary("Edit", {"file_path": "file.cs"})
    assert activity.tool_summary("Bash", {"command": "ls -la"}).startswith("Bash")
    assert activity.tool_summary("Glob", {}) == "Glob"
