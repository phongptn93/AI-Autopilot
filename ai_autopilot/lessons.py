"""Retrospective learning loop — a tiny per-repo "lessons" memory.

The autopilot captures what a run got flagged on (auto-review findings) into a
per-repo markdown file under ``<workspace>/.autopilot/lessons/<repo>.md``, and
injects the most recent ones back into the next run's brief so it stops repeating
the same mistakes. File-based on purpose: no schema/migration, and a human can
read or prune the list directly.

Opt-in via ``learning_loop_enabled`` — the callers no-op when it is off.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_LESSONS_SUBDIR = Path(".autopilot") / "lessons"
_MAX_LESSONS = 50  # keep the file bounded — oldest lines drop off
_SAFE_REPO = re.compile(r"[^A-Za-z0-9._-]+")


def _lessons_path(workspace: str, repo: str) -> Path:
    safe = _SAFE_REPO.sub("_", repo) or "repo"
    return Path(workspace) / _LESSONS_SUBDIR / f"{safe}.md"


def record_lessons(workspace: str, repo: str, lessons: list[str], *, now: datetime) -> None:
    """Append ``lessons`` (deduped against what's already stored) for ``repo``.

    Best-effort: any filesystem error is swallowed — learning must never break a run.
    """
    if not workspace or not repo:
        return
    clean = [line.strip() for line in lessons if line and line.strip()]
    if not clean:
        return
    path = _lessons_path(workspace, repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        seen = {_body(line) for line in existing}
        stamp = now.date().isoformat()
        added = [f"- [{stamp}] {t}" for t in clean if t not in seen]
        if not added:
            return
        kept = (existing + added)[-_MAX_LESSONS:]
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        pass


def read_lessons(workspace: str, repo: str, *, limit: int = 10) -> list[str]:
    """Most recent lesson texts (newest last), stripped of the date prefix. []
    when learning is unused / the file is missing / unreadable."""
    if not workspace or not repo:
        return []
    path = _lessons_path(workspace, repo)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    bodies = [_body(line) for line in lines if line.strip()]
    return [b for b in bodies if b][-limit:]


def _body(line: str) -> str:
    """Strip a stored line's ``- [date] `` prefix, leaving the lesson text."""
    return re.sub(r"^\s*-\s*\[[^\]]*\]\s*", "", line).strip()


def lessons_brief(workspace: str, repos: list[str], *, limit: int = 8) -> str:
    """A brief section listing recent lessons across ``repos`` — '' if none, so the
    caller can append it unconditionally."""
    seen: set[str] = set()
    out: list[str] = []
    for repo in repos:
        for lesson in read_lessons(workspace, repo, limit=limit):
            if lesson not in seen:
                seen.add(lesson)
                out.append(f"- {lesson}")
    if not out:
        return ""
    return (
        "\n⚠️ Lessons from past runs on this codebase — do NOT repeat these:\n"
        + "\n".join(out[-limit:])
    )
