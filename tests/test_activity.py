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


def test_pr_runs_are_namespaced_away_from_work_items(tmp_path):
    # A review's work item is often synthetic (the PR id stands in), so a PR run
    # filed under a bare number would overwrite the feed of work item #3882.
    ws = str(tmp_path)
    activity.append(ws, 3882, "work item run")
    activity.append(ws, activity.pr_key(3882), "PR review run")
    assert "work item run" in activity.read(ws, 3882)
    assert "work item run" not in activity.read(ws, activity.pr_key(3882))
    assert "PR review run" in activity.read(ws, "pr-3882")


def test_no_workspace_means_no_feed(tmp_path, monkeypatch):
    # Without a workspace the path would be relative — feed files scattered into
    # whatever directory the process runs from (a checkout, in practice).
    monkeypatch.chdir(tmp_path)
    activity.append("", 42, "would have littered the cwd")
    assert activity.read("", 42) == ""
    assert not (tmp_path / ".autopilot").exists()
