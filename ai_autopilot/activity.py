"""Live agent activity feed — what the agent is doing right now, per work item.

The executor appends one line per streamed event (assistant message, tool call)
to ``<workspace>/.autopilot/runs/<id>.activity.log``; the dashboard tails it so an
operator can watch the agent work in real time instead of staring at a silent
"In progress".
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path

RUNS_SUBDIR = Path(".autopilot") / "runs"
_MAX_BYTES = 200_000  # only keep the readable tail


def _path(workspace: str, item_id: int) -> Path:
    return Path(workspace) / RUNS_SUBDIR / f"{item_id}.activity.log"


def clear(workspace: str, item_id: int) -> None:
    with contextlib.suppress(OSError):
        _path(workspace, item_id).unlink()


def append(workspace: str, item_id: int, line: str) -> None:
    """Append one timestamped activity line (best-effort; never raises)."""
    with contextlib.suppress(OSError):
        path = _path(workspace, item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")


def read(workspace: str, item_id: int) -> str:
    """Return the tail of the activity log, or '' if none."""
    try:
        data = _path(workspace, item_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-_MAX_BYTES:] if len(data) > _MAX_BYTES else data


def tool_summary(name: str, tool_input: dict | None) -> str:
    """One-line summary of a tool call (name + the most telling argument)."""
    tool_input = tool_input or {}
    for key in ("file_path", "path", "command", "pattern", "skill", "url", "prompt", "query"):
        if key in tool_input and tool_input[key]:
            return f"{name} · {str(tool_input[key])[:120]}"
    return name
