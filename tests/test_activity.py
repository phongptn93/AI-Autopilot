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


def test_read_returns_the_tail_without_reading_the_whole_file(tmp_path, monkeypatch):
    """The activity page re-polls this every three seconds. The old form read the
    ENTIRE feed off disk to keep its last few KB, so a long run's log was re-read
    twenty times a minute per open tab. `last_event` in the same module already did
    it the right way, by seeking."""
    monkeypatch.setattr(activity, "_MAX_BYTES", 200)
    ws = str(tmp_path)
    for i in range(200):
        activity.append(ws, 7, f"line {i:04d}")
    path = activity._path(ws, 7)
    assert path.stat().st_size > 1000        # the file is much bigger than the tail

    out = activity.read(ws, 7)
    assert len(out) <= activity._MAX_BYTES
    assert "line 0199" in out                # newest kept
    assert "line 0000" not in out            # oldest dropped
    # The seek lands mid-line; that fragment must not be shown as if it were a line.
    assert not out.startswith("ine ")


def test_read_keeps_a_short_file_whole(tmp_path):
    ws = str(tmp_path)
    activity.append(ws, 8, "only line")
    assert "only line" in activity.read(ws, 8)
