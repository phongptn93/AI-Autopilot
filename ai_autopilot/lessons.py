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
from datetime import date, datetime, timedelta
from pathlib import Path

#: Where a stored line came from. The distinction is load-bearing, not decorative:
#: a rule a human typed is a standing instruction, while a line the machine inferred
#: from one bad run is a guess. They must not compete for the same slot on equal terms
#: — so an authored line is injected FIRST and is never the one dropped when the file
#: hits its cap.
SOURCE_LEARNED = "learned"
SOURCE_AUTHORED = "authored"
#: Handed down by the central after a human there approved it. Treated like an
#: authored rule — pinned ahead of local guesses and never dropped when the file fills
#: — because that is exactly what it is: somebody vetted it, just not on this machine.
SOURCE_FLEET = "fleet"

#: Sources whose lines are standing instructions rather than inferences.
_PINNED = (SOURCE_AUTHORED, SOURCE_FLEET)

_LESSONS_SUBDIR = Path(".autopilot") / "lessons"
#: Bucket for lessons that belong to the workspace rather than one repo — a PR
#: rejection or a reopen is attached to a work item, which may span several repos or
#: none that we can name at that point. Always read alongside the named repos so an
#: unattributable signal still teaches.
SHARED_BUCKET = "_workspace"
_MAX_LESSONS = 50  # keep the file bounded — oldest lines drop off
_SAFE_REPO = re.compile(r"[^A-Za-z0-9._-]+")
# ``- [2026-09-18] text``            legacy + machine-learned, seen once
# ``- [2026-09-18][a] text``         a human typed this one
# ``- [2026-09-18][a,n3] text``      …and it has been recorded 3 times
# The meta block is optional and only recognised when it contains nothing but the
# tokens below, so a lesson whose text legitimately starts with "[something]" is left
# alone instead of being silently eaten by the parser.
_LINE = re.compile(
    r"^\s*-\s*\[(?P<date>[^\]]*)\](?:\[(?P<meta>(?:a|f|n\d+)(?:,(?:a|f|n\d+))*)\])?"
    r"\s*(?P<text>.*)$"
)
#: Trailing context varies per occurrence ("reopened from state X; cleared [...]") while
#: the sentence in front of it is the actual lesson. Five reopens produced five lines
#: that a human reads as one — and they ate five of the eight injection slots.
_CONTEXT_TAIL = re.compile(r"\s*(?:Context|Ngữ cảnh)\s*:.*$", re.IGNORECASE | re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Lesson:
    """One stored line, split into its parts for display."""

    repo: str
    date: str
    text: str
    #: ``authored`` (a human typed it) or ``learned`` (the machine inferred it).
    source: str = SOURCE_LEARNED
    #: How many times this lesson has been recorded. >1 means the same thing keeps
    #: happening, which is the single best signal of how much it matters.
    count: int = 1

    @property
    def authored(self) -> bool:
        return self.source == SOURCE_AUTHORED

    @property
    def pinned(self) -> bool:
        """Is this a standing instruction (typed by a human, here or at the centre)?"""
        return self.source in _PINNED


def normalize(text: str) -> str:
    """The key two lines are 'the same lesson' by.

    Case, spacing and the per-occurrence context tail are all noise: what makes two
    lines the same lesson is the sentence itself.
    """
    stripped = _CONTEXT_TAIL.sub("", text or "")
    return _WHITESPACE.sub(" ", stripped).strip().lower()


def _strongest(*sources: str) -> str:
    """The source that wins when the same lesson arrives twice.

    A human typing a rule the machine had merely guessed PROMOTES it: it stops being
    an inference and stops being droppable. The centre's approval does the same, one
    rung below a rule written on this machine — local intent beats remote intent.
    """
    for rank in (SOURCE_AUTHORED, SOURCE_FLEET):
        if rank in sources:
            return rank
    return SOURCE_LEARNED


def _meta(source: str, count: int) -> str:
    """The ``[a,n3]`` block for a line, or '' when it carries no news."""
    bits = []
    if source == SOURCE_AUTHORED:
        bits.append("a")
    elif source == SOURCE_FLEET:
        bits.append("f")
    if count > 1:
        bits.append(f"n{count}")
    return f"[{','.join(bits)}]" if bits else ""


def _format(lesson: Lesson) -> str:
    """One Lesson as the line stored on disk."""
    return f"- [{lesson.date}]{_meta(lesson.source, lesson.count)} {lesson.text}"


def _parse(line: str, repo: str = "") -> Lesson | None:
    """One stored line back into a Lesson, or None when there is nothing on it."""
    if not line.strip():
        return None
    match = _LINE.match(line)
    if match is None:
        text = line.strip()
        return Lesson(repo=repo, date="", text=text) if text else None
    meta = match.group("meta") or ""
    text = (match.group("text") or "").strip()
    if not text:
        return None
    count = 1
    for token in meta.split(","):
        if token.startswith("n") and token[1:].isdigit():
            count = max(1, int(token[1:]))
    tokens = meta.split(",")
    source = SOURCE_LEARNED
    if "a" in tokens:
        source = SOURCE_AUTHORED
    elif "f" in tokens:
        source = SOURCE_FLEET
    return Lesson(
        repo=repo, date=(match.group("date") or "").strip(), text=text,
        source=source, count=count,
    )


def _read(workspace: str, repo: str) -> list[Lesson]:
    """Every stored lesson for ``repo``, oldest first (the order on disk)."""
    try:
        lines = _lessons_path(workspace, repo).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [le for le in (_parse(ln, repo) for ln in lines) if le is not None]


def _write(workspace: str, repo: str, items: list[Lesson]) -> bool:
    """Replace the file with ``items`` (oldest first). Removes it when empty."""
    path = _lessons_path(workspace, repo)
    try:
        if not items:
            path.unlink(missing_ok=True)   # an empty file shows as a ghost repo
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(_format(le) for le in items) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def _capped(items: list[Lesson]) -> list[Lesson]:
    """Trim to ``_MAX_LESSONS``, dropping the oldest MACHINE-LEARNED lines first.

    A plain tail-slice dropped whatever was oldest, which on a busy repo quietly
    deleted the rules a human had typed — the only lines nothing will ever regenerate.
    """
    if len(items) <= _MAX_LESSONS:
        return items
    authored = [le for le in items if le.pinned]
    learned = [le for le in items if not le.pinned]
    room = max(0, _MAX_LESSONS - len(authored))
    keep = set(map(id, authored[-_MAX_LESSONS:])) | set(map(id, learned[-room:]))
    return [le for le in items if id(le) in keep]


def _lessons_dir(workspace: str) -> Path:
    return Path(workspace) / _LESSONS_SUBDIR


def _lessons_path(workspace: str, repo: str) -> Path:
    safe = _SAFE_REPO.sub("_", repo) or "repo"
    return _lessons_dir(workspace) / f"{safe}.md"


def record_lessons(
    workspace: str, repo: str, lessons: list[str], *, now: datetime,
    source: str = SOURCE_LEARNED,
) -> None:
    """Record ``lessons`` for ``repo``, merging anything already stored.

    Merging rather than appending is the whole point. Dedup used to be an exact
    string match, so five reopens of five different work items wrote five lines that
    say the identical thing and differ only in their ``Context:`` tail — and since the
    brief takes the NEWEST eight, those five crowded out everything worth knowing. A
    repeat now bumps the existing line's date and its count, which both collapses the
    noise and records the one fact the five copies were really carrying: this keeps
    happening.

    Best-effort: any filesystem error is swallowed — learning must never break a run.
    """
    if not workspace or not repo:
        return
    clean = [line.strip() for line in lessons if line and line.strip()]
    if not clean:
        return
    items = _read(workspace, repo)
    by_key = {normalize(le.text): i for i, le in enumerate(items)}
    stamp = now.date().isoformat()
    changed = False
    for text in clean:
        key = normalize(text)
        if not key:
            continue
        at = by_key.get(key)
        if at is None:
            items.append(Lesson(repo=repo, date=stamp, text=text, source=source, count=1))
            by_key[key] = len(items) - 1
            changed = True
            continue
        was = items[at]
        # A human typing a rule the machine had already guessed PROMOTES the line:
        # it stops being a guess, and stops being droppable when the file fills up.
        items[at] = Lesson(
            repo=repo, date=stamp, text=text if source in _PINNED else was.text,
            source=_strongest(was.source, source), count=was.count + 1,
        )
        changed = True
    if changed:
        _write(workspace, repo, _capped(items))


def read_lessons(workspace: str, repo: str, *, limit: int = 10) -> list[str]:
    """Most recent lesson texts (newest last), stripped of the date prefix. []
    when learning is unused / the file is missing / unreadable."""
    if not workspace or not repo:
        return []
    return [le.text for le in _read(workspace, repo)][-limit:]


def recent(workspace: str, repos: list[str], *, limit: int = 8) -> list[str]:
    """The lesson texts that would be injected for ``repos``, in the order the brief
    carries them — capped at ``limit``.

    The brief and the "how many were injected" counter both read THIS, so the number
    the dashboard shows can never drift from what the agent was actually told.

    Two rules decide what survives the cap:

    * **Authored first.** A rule a human typed is a standing instruction; a learned
      line is an inference from one bad run. Sorting purely by recency meant a burst
      of machine noise on a Tuesday could push out every rule the team had written,
      silently and with nothing on screen to say so.
    * **Then the most recent learned lines**, deduped across repos by meaning rather
      than by exact string, so the same lesson filed under two repos does not take
      two slots.
    """
    if limit <= 0:
        return []
    picked: list[Lesson] = []
    seen: set[str] = set()
    for repo in [*repos, SHARED_BUCKET]:
        for lesson in _read(workspace, repo):
            key = normalize(lesson.text)
            if key and key not in seen:
                seen.add(key)
                picked.append(lesson)
    authored = [le.text for le in picked if le.pinned]
    learned = [le.text for le in picked if not le.pinned]
    # Authored lines keep the whole budget if they need it; whatever is left goes to
    # the newest learned ones (the tail, because these lists are oldest-first).
    room = max(0, limit - len(authored))
    return [*authored[-limit:], *learned[-room:]]


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
    return list(reversed(_read(workspace, repo)))


def add(workspace: str, repo: str, text: str) -> bool:
    """Store one piece of knowledge a HUMAN typed. True when the file changed.

    The page could delete and forget, but never add — so the only thing that could
    put a line in front of the agent was the agent's own post-mortem of its mistakes.
    A team's actual conventions, the ones that stop the mistake being made in the
    first place, had no way in at all.
    """
    if not workspace or not repo or not text.strip():
        return False
    before = _read(workspace, repo)
    record_lessons(workspace, repo, [text.strip()],
                   now=datetime.now(), source=SOURCE_AUTHORED)  # noqa: DTZ005 — local day
    return _read(workspace, repo) != before


def add_many(workspace: str, repo: str, blob: str) -> int:
    """Take a pasted block and store each line as its own piece of knowledge.

    Markdown bullets, numbering and blank lines are tolerated: people paste from a
    conventions document, and demanding they reformat it first is how an import
    feature goes unused. Returns how many lines actually landed.
    """
    added = 0
    for raw in (blob or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s*", "", raw).strip()
        if line and add(workspace, repo, line):
            added += 1
    return added


def edit(workspace: str, repo: str, old: str, new: str) -> bool:
    """Reword one stored line in place, keeping its date, source and count.

    Pruning was the only correction available, so fixing a lesson that was 90% right
    meant deleting it and retyping it as a new one — losing both when it was learned
    and how often it has recurred, which is most of what makes it worth reading.
    """
    if not workspace or not repo or not old.strip() or not new.strip():
        return False
    items = _read(workspace, repo)
    target, replacement = old.strip(), new.strip()
    out, hit = [], False
    for le in items:
        if not hit and le.text == target:
            out.append(Lesson(repo=repo, date=le.date, text=replacement,
                              source=le.source, count=le.count))
            hit = True
        else:
            out.append(le)
    return _write(workspace, repo, out) if hit else False


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
    items = _read(workspace, repo)
    target = text.strip()
    kept = [le for le in items if le.text != target]
    if len(kept) == len(items):
        return False
    return _write(workspace, repo, kept)


def clear(workspace: str, repo: str) -> bool:
    """Forget everything learned about ``repo``. True when a file was removed."""
    if not workspace or not repo:
        return False
    try:
        _lessons_path(workspace, repo).unlink()
    except OSError:
        return False
    return True


def per_day(workspace: str, *, today: str = "") -> list[tuple[str, int]]:
    """(date, new-lessons-recorded) across all repos, oldest → newest, NO GAPS.

    A falling tail is the loop working: fewer NEW findings per day means the agent
    stopped re-earning the same flags. That reading only holds if quiet days are IN
    the series — skipping them drew two busy days side by side and called it a trend,
    when the truth was "busy on the 12th, nothing since the 21st". Every day from the
    first lesson to today is present, zeros included.
    """
    counts: dict[str, int] = {}
    for lesson in all_entries(workspace):
        if lesson.date:
            counts[lesson.date] = counts.get(lesson.date, 0) + 1
    if not counts:
        return []
    try:
        start = date.fromisoformat(min(counts))
        end = date.fromisoformat(today) if today else date.today()  # noqa: DTZ011 — local day
    except ValueError:  # a hand-edited date we cannot parse: fall back to what we have
        return sorted(counts.items())
    end = max(end, date.fromisoformat(max(counts)))
    out: list[tuple[str, int]] = []
    day = start
    while day <= end:
        key = day.isoformat()
        out.append((key, counts.get(key, 0)))
        day += timedelta(days=1)
    return out


def apply_fleet(workspace: str, items: list[tuple[str, str]]) -> int:
    """Store knowledge the central approved. ``items`` is (repo, text) pairs.

    Written with :data:`SOURCE_FLEET` so the page can say where a line came from, and
    so it is pinned and uncapped like a local rule — it has already been through a
    human at the centre, which is more review than a locally learned line ever gets.

    Returns how many lines are NEW here. A line this machine already knows is merged
    (its count goes up, its source is promoted) rather than duplicated, so the same
    lesson does not appear twice just because it took two routes to arrive.
    """
    if not workspace:
        return 0
    added = 0
    now = datetime.now()  # noqa: DTZ005 — local day, same clock the rest of the file uses
    by_repo: dict[str, list[str]] = {}
    for repo, text in items:
        if str(text or "").strip():
            by_repo.setdefault(str(repo or SHARED_BUCKET), []).append(str(text).strip())
    for repo, texts in by_repo.items():
        known = {normalize(le.text) for le in _read(workspace, repo)}
        added += sum(1 for t in texts if normalize(t) not in known)
        record_lessons(workspace, repo, texts, now=now, source=SOURCE_FLEET)
    return added


def contributions(workspace: str) -> list[dict]:
    """This machine's shareable knowledge, in the shape the central accepts.

    Everything it knows, minus nothing — the filtering that matters happens at the
    CENTRAL, which will not redistribute anything a human has not approved. Sending
    the machine's own view whole is what lets the centre count how many independent
    machines hit the same thing, which is the signal it promotes on.
    """
    out: list[dict] = []
    for repo in list_repos(workspace):
        for le in _read(workspace, repo):
            key = normalize(le.text)
            if not key:
                continue
            out.append({
                "key": key[:500], "text": le.text, "repo": repo,
                "source": le.source, "count": le.count,
            })
    return out
