"""Live agent activity feed — what the agent is doing right now, per work item.

The executor appends one line per streamed event (assistant message, tool call)
to ``<workspace>/.autopilot/runs/<id>.activity.log``; the dashboard tails it so an
operator can watch the agent work in real time instead of staring at a silent
"In progress".
"""

from __future__ import annotations

import contextlib
import re
import time
from datetime import datetime
from pathlib import Path

RUNS_SUBDIR = Path(".autopilot") / "runs"
_MAX_BYTES = 200_000  # only keep the readable tail


def pr_key(pr_id: int) -> str:
    """Feed key for a PR-level run (auto-review, /review, /fix on a comment).

    Work-item ids and PR ids are separate ADO namespaces that both land in this one
    directory, and a review's work item is often synthetic (the PR id stands in when
    the branch encodes no item) — so filing a review under a bare number would sooner
    or later overwrite the feed of somebody's work item with the same number.
    """
    return f"pr-{pr_id}"


def loop_key(name: str) -> str:
    """Feed key for a scheduled loop's run.

    A loop has no work item, so every loop run reported itself as item ``0`` — one
    shared feed that each loop overwrote in turn, which is the same collision
    ``pr_key`` exists to prevent. Keyed by name, a nightly audit and a dependency
    sweeper can run in the same hour and each still be watchable.
    """
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (name or "").lower())).strip("-")
    return f"loop-{slug or 'unnamed'}"


def _path(workspace: str, item_id: int | str) -> Path | None:
    """The feed file, or ``None`` when there is no workspace to put it in.

    Without a workspace the path would be relative and the feed would be scattered
    into whatever directory the process happens to run from — files nobody reads,
    written into a checkout. No workspace, no feed.
    """
    if not workspace:
        return None
    return Path(workspace) / RUNS_SUBDIR / f"{item_id}.activity.log"


def clear(workspace: str, item_id: int | str) -> None:
    path = _path(workspace, item_id)
    if path is None:
        return
    with contextlib.suppress(OSError):
        path.unlink()


def append(workspace: str, item_id: int | str, line: str) -> None:
    """Append one timestamped activity line (best-effort; never raises)."""
    path = _path(workspace, item_id)
    if path is None:
        return
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H:%M:%S")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")


def read(workspace: str, item_id: int | str) -> str:
    """Return the tail of the activity log, or '' if none.

    SEEKS to the tail rather than reading the file and throwing most of it away. The
    old form read the whole feed into memory to keep its last few KB — on the activity
    page, which re-polls this every three seconds, that is the entire log of a long run
    read off disk twenty times a minute per open tab. ``last_event`` in this same module
    already did it the right way.
    """
    path = _path(workspace, item_id)
    if path is None:
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _MAX_BYTES))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    # The seek almost certainly landed mid-line; drop that fragment rather than show it.
    if size > _MAX_BYTES:
        cut = data.find(chr(10))
        data = data[cut + 1:] if cut != -1 else data
    return data


def last_event(workspace: str, item_id: int | str) -> tuple[str, float | None]:
    """The feed's newest line and how many seconds ago it was written.

    ``(line, age)`` with ``age`` ``None`` when there is no feed at all. A run that is
    producing lines is working; one whose newest line is minutes old is the case an
    operator actually needs to spot, and neither is visible from a list of start times.
    The file's mtime is the clock, so this costs a stat and does not read the tail.
    """
    path = _path(workspace, item_id)
    if path is None:
        return "", None
    try:
        age = max(0.0, time.time() - path.stat().st_mtime)
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return "", None
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    return (lines[-1] if lines else ""), age


def tool_summary(name: str, tool_input: dict | None) -> str:
    """One-line summary of a tool call (name + the most telling argument)."""
    tool_input = tool_input or {}
    for key in ("file_path", "path", "command", "pattern", "skill", "url", "prompt", "query"):
        if key in tool_input and tool_input[key]:
            return f"{name} · {str(tool_input[key])[:120]}"
    return name
