"""Retrospective learning loop — a tiny per-repo "lessons" memory.

The autopilot captures what a run got flagged on (auto-review findings) into a
per-repo markdown file under ``<workspace>/.autopilot/lessons/<repo>.md``, and
injects the most recent ones back into the next run's brief so it stops repeating
the same mistakes. File-based on purpose: no schema/migration, and a human can
read or prune the list directly.

Opt-in via ``learning_loop_enabled`` — the callers no-op when it is off. The
dashboard's Learning page reads the same files through :func:`entries` /
:func:`per_day` and edits them through :func:`delete` / :func:`clear`, so what an
operator sees is exactly what the next brief will carry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_LESSONS_SUBDIR = Path(".autopilot") / "lessons"
#: Bucket for lessons that belong to the workspace rather than one repo — a PR
#: rejection or a reopen is attached to a work item, which may span several repos or
#: none that we can name at that point. Always read alongside the named repos so an
#: unattributable signal still teaches.
SHARED_BUCKET = "_workspace"
_MAX_LESSONS = 50  # keep the file bounded — oldest lines drop off
_SAFE_REPO = re.compile(r"[^A-Za-z0-9._-]+")
_LINE = re.compile(r"^\s*-\s*\[(?P<date>[^\]]*)\]\s*(?P<text>.*)$")


@dataclass(frozen=True)
class Lesson:
    """One stored line, split into its parts for display."""

    repo: str
    date: str
    text: str


def _lessons_dir(workspace: str) -> Path:
    return Path(workspace) / _LESSONS_SUBDIR


def _lessons_path(workspace: str, repo: str) -> Path:
    safe = _SAFE_REPO.sub("_", repo) or "repo"
    return _lessons_dir(workspace) / f"{safe}.md"


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


def recent(workspace: str, repos: list[str], *, limit: int = 8) -> list[str]:
    """The lesson texts that would be injected for ``repos`` — deduped across them,
    newest last, capped at ``limit``.

    The brief and the "how many were injected" counter both read THIS, so the number
    the dashboard shows can never drift from what the agent was actually told.
    """
    if limit <= 0:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for repo in [*repos, SHARED_BUCKET]:
        for lesson in read_lessons(workspace, repo, limit=limit):
            if lesson not in seen:
                seen.add(lesson)
                out.append(lesson)
    return out[-limit:]


def lessons_brief(workspace: str, repos: list[str], *, limit: int = 8) -> str:
    """A brief section listing recent lessons across ``repos`` — '' if none, so the
    caller can append it unconditionally."""
    picked = recent(workspace, repos, limit=limit)
    if not picked:
        return ""
    return (
        "\n⚠️ Lessons from past runs on this codebase — do NOT repeat these:\n"
        + "\n".join(f"- {lesson}" for lesson in picked)
    )


# ── Read/edit surface for the dashboard ───────────────────────────────────────


def list_repos(workspace: str) -> list[str]:
    """Repos that have a lessons file, alphabetically. [] when learning never ran."""
    if not workspace:
        return []
    try:
        return sorted(p.stem for p in _lessons_dir(workspace).glob("*.md") if p.is_file())
    except OSError:
        return []


def entries(workspace: str, repo: str) -> list[Lesson]:
    """Every stored lesson for ``repo`` — newest FIRST (reading order for a human)."""
    if not workspace or not repo:
        return []
    try:
        lines = _lessons_path(workspace, repo).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[Lesson] = []
    for line in lines:
        if not line.strip():
            continue
        match = _LINE.match(line)
        date = match.group("date").strip() if match else ""
        text = (match.group("text") if match else line).strip()
        if text:
            out.append(Lesson(repo=repo, date=date, text=text))
    out.reverse()
    return out


def all_entries(workspace: str) -> list[Lesson]:
    """Every stored lesson across every repo, newest first within each repo."""
    return [lesson for repo in list_repos(workspace) for lesson in entries(workspace, repo)]


def delete(workspace: str, repo: str, text: str) -> bool:
    """Drop every line whose text equals ``text``. True when something was removed.

    Pruning a wrong or stale lesson matters: an unedited list keeps feeding the same
    line into every future brief, so a bad lesson is worse than no lesson.
    """
    if not workspace or not repo or not text.strip():
        return False
    path = _lessons_path(workspace, repo)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    target = text.strip()
    kept = [line for line in lines if line.strip() and _body(line) != target]
    if len(kept) == len([line for line in lines if line.strip()]):
        return False
    try:
        if kept:
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            path.unlink()  # empty file would show as a ghost repo on the page
    except OSError:
        return False
    return True


def clear(workspace: str, repo: str) -> bool:
    """Forget everything learned about ``repo``. True when a file was removed."""
    if not workspace or not repo:
        return False
    try:
        _lessons_path(workspace, repo).unlink()
    except OSError:
        return False
    return True


def per_day(workspace: str) -> list[tuple[str, int]]:
    """(date, new-lessons-recorded) across all repos, oldest → newest.

    A falling tail is the loop working: fewer NEW findings per day means the agent
    stopped re-earning the same flags.
    """
    counts: dict[str, int] = {}
    for lesson in all_entries(workspace):
        if lesson.date:
            counts[lesson.date] = counts.get(lesson.date, 0) + 1
    return sorted(counts.items())
