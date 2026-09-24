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

import contextlib
import json
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
            # No early return: emptying a repo is a mutation like any other, and
            # skipping the sync below left the deleted lines in the file the agent
            # reads — deleted from the dashboard, still in front of the model.
            path.unlink(missing_ok=True)   # an empty file shows as a ghost repo
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "\n".join(_format(le) for le in items) + "\n", encoding="utf-8"
            )
    except OSError:
        return False
    # Every mutation funnels through here, so this is the one place that has to keep
    # Claude's own memory in step. Doing it anywhere else means a path that edits
    # lessons without refreshing what the agent reads.
    sync_memory(workspace)
    return True


# ── Claude's project memory ───────────────────────────────────────────────────
#
# Where these lines actually belong. The agent already runs with
# ``setting_sources=["user", "project", "local"]`` (see
# ai_autopilot/execution/claude_executor.py), which loads the workspace's CLAUDE.md and
# its ``.claude/`` rules by itself — so a workspace that has been set up properly is
# already carrying 20KB of hand-written convention into every run, chosen by the model
# as it becomes relevant.
#
# Against that, prepending the newest eight lines to EVERY brief was the weaker channel
# in every respect that matters: it ignored whether a lesson had anything to do with the
# task, it capped the whole store at eight slots that a burst of machine noise could
# take, and it lived in a dotfolder nobody reviews. Measured on the real workspace this
# was written for: 21 rule files and a 20,250-byte CLAUDE.md on one side, and a lessons
# store holding exactly one line on the other.
#
# So the loop writes into the memory Claude already reads, and the injection stays only
# as an escape hatch (``lessons_max_injected``, now 0 by default).
# Two destinations, split on the line the store already draws.
#
# A rule a human typed is an INSTRUCTION: short, meant to hold on every run, and the
# reason somebody typed it was so it would always apply. It stays in `.claude/rules`,
# which the runtime loads whole, every time.
#
# A line the machine inferred from one bad run is a GUESS. It accumulates (50 per repo),
# it is situational, and most of it has nothing to do with any given task. It becomes a
# SKILL — indexed by name and description, body read only when invoked.
#
# What makes the skill reliable is that nothing has to guess whether to open it. The
# brief builder already knows which repos a work item touches (`lessons.recent` has
# always been repo-scoped), so it names the exact skill for those repos in one line. The
# model is told, not left to discover — and `_build_prompt` already had the precedent of
# instructing a skill run.
#
# What this does NOT do, plainly: inside one repo there is no semantic selection. Open
# the skill and you get that repo's lessons, ordered by recency and by how often the
# thing has actually recurred. Keyword matching over free-text lessons in two languages
# would be wrong more often than right, and embedding retrieval is a new subsystem for
# fifty items. The count ("đã gặp 8 lần") is the honest cheap signal.
MEMORY_FILE = Path(".claude") / "rules" / "autopilot-lessons.md"
_SKILLS_SUBDIR = Path(".claude") / "skills"
_SKILL_PREFIX = "autopilot-lessons"
#: Markers around the one line this module owns inside CLAUDE.md. A pointer rather than
#: the content: CLAUDE.md is hand-written and large, and generated text merged into it is
#: how somebody loses an afternoon to a conflict.
_CLAUDE_MD_START = "<!-- autopilot:lessons:start -->"
_CLAUDE_MD_END = "<!-- autopilot:lessons:end -->"


def memory_path(workspace: str) -> Path:
    """Where the generated rule file lives for ``workspace``."""
    return Path(workspace) / MEMORY_FILE


def memory_is_live(workspace: str) -> bool:
    """Is the generated rule actually on disk for the agent to read?

    Asked of this module rather than stat'd by the caller: the file is this module's,
    and a dashboard route that pokes at the filesystem itself is also a blocking call on
    the event loop.
    """
    if not workspace:
        return False
    try:
        return memory_path(workspace).is_file()
    except OSError:
        return False


def _label(repo: str) -> str:
    return "mọi repo" if repo == SHARED_BUCKET else repo


def skill_name(repo: str) -> str:
    """The skill that holds ``repo``'s machine-learned lessons.

    A skill name is read by people and quoted in briefs, so the shared bucket's internal
    ``_workspace`` is spelled out rather than leaked: ``autopilot-lessons-_workspace``
    reads like a slip. Underscores and dots elsewhere become dashes for the same reason.
    """
    if repo == SHARED_BUCKET:
        return f"{_SKILL_PREFIX}-workspace"
    safe = re.sub(r"[^a-z0-9]+", "-", repo.lower()).strip("-")
    return f"{_SKILL_PREFIX}-{safe or 'repo'}"


def skill_dir(workspace: str, repo: str) -> Path:
    return Path(workspace) / _SKILLS_SUBDIR / skill_name(repo)


_GENERATED_NOTE = (
    "> ⚠️ **Do AI-Autopilot sinh ra và ghi đè.** Sửa tay ở đây sẽ mất ở lần ghi kế "
    "tiếp — sửa trên trang Learning của dashboard (`/dashboard/learning`).\n"
    "> Nguồn: `.autopilot/lessons/*.md`"
)


def render_rules(workspace: str) -> str:
    """The lines a HUMAN wrote, as one always-loaded rule file. '' when there are none.

    Only pinned lines (typed here, or approved at the fleet centre) go in. They are
    short, they are instructions, and the reason somebody typed one was so that it would
    hold on every run — which is exactly what `.claude/rules` does. The machine's own
    inferences do not belong in a file that is loaded whole, every time; they go to a
    skill (see :func:`render_skill`), and this file points at it.
    """
    blocks: list[str] = []
    pointers: list[str] = []
    for repo in list_repos(workspace):
        items = _read(workspace, repo)
        pinned = [le for le in items if le.pinned]
        learned = [le for le in items if not le.pinned]
        if pinned:
            blocks.append(
                f"## {_label(repo)}\n\n"
                + "\n".join(f"- {le.text}" for le in pinned)
            )
        if learned:
            pointers.append(
                f"- **{_label(repo)}** — {len(learned)} bài học máy tự rút: "
                f"chạy skill `{skill_name(repo)}`"
            )
    if not blocks and not pointers:
        return ""
    out = [f"# Tri thức AI-Autopilot\n\n{_GENERATED_NOTE}\n"]
    if blocks:
        out.append(
            "## Quy tắc do người viết\n\n"
            "Áp dụng như mọi convention khác của workspace.\n\n"
            + "\n\n".join(blocks).replace("## ", "### ", 1).replace("\n## ", "\n### ")
        )
    if pointers:
        # Named, not pasted. The bodies live in skills so they cost nothing until a run
        # actually touches that repo — and the brief names the right one (see
        # `lessons_pointer`), so opening it is never left to chance.
        out.append(
            "## Bài học máy tự rút\n\n"
            "Đây là **suy luận từ một lần hỏng**, không phải quy tắc đã chốt. Khi làm "
            "việc trên repo tương ứng, đọc skill của nó trước khi mở PR:\n\n"
            + "\n".join(pointers)
        )
    return "\n\n".join(out) + "\n"


def render_skill(workspace: str, repo: str) -> str:
    """One repo's machine-learned lessons as a SKILL.md. '' when it has none.

    The frontmatter is what decides whether this is ever useful on its own, so the
    description names the repo and what the lessons are about rather than describing the
    mechanism. It is belt and braces: the brief names this skill outright for the repos
    in scope, and a description that stands up on its own covers the case where somebody
    runs the agent by hand.
    """
    learned = [le for le in _read(workspace, repo) if not le.pinned]
    if not learned:
        return ""
    # Newest first: the same order the store shows a human, and the order in which a
    # reader who stops halfway has read the most useful half.
    learned = list(reversed(learned))
    repeated = [le for le in learned if le.count > 1]
    topic = "; ".join(le.text[:60].rstrip() for le in (repeated or learned)[:3])
    desc = (
        f"Bài học AI-Autopilot đã rút ra khi làm việc trên {_label(repo)} — "
        f"{len(learned)} mục từ review và rework của các run trước. "
        f"ĐỌC TRƯỚC KHI mở PR trên repo này để không lặp lại. Gồm: {topic}."
    )
    body = "\n".join(
        f"- {f'**Đã gặp {le.count} lần.** ' if le.count > 1 else ''}{le.text}"
        for le in learned
    )
    return (
        "---\n"
        f"name: {skill_name(repo)}\n"
        f"description: {desc}\n"
        "---\n\n"
        f"# Bài học từ các run trước — {_label(repo)}\n\n"
        f"{_GENERATED_NOTE}\n\n"
        "Mỗi dòng dưới đây là suy luận từ **một lần hỏng thật**, không phải quy tắc đã "
        "chốt — cân nhắc theo ngữ cảnh. Số lần gặp là tín hiệu đáng tin nhất về mức độ "
        "nghiêm trọng: cái gì lặp lại nhiều là cái đang cắn liên tục.\n\n"
        f"{body}\n"
    )


def lessons_pointer(workspace: str, repos: list[str]) -> str:
    """The brief's one line per repo naming the skill to open. '' when there is none.

    This is what replaces pasting the lessons themselves. The work item's repos are
    already known here, so the selection is made by the autopilot on a fact — which repo
    — instead of being left to the model to infer from a description it may never read.
    """
    if not workspace:
        return ""
    seen: set[str] = set()
    lines: list[str] = []
    for repo in [*repos, SHARED_BUCKET]:
        if repo in seen:
            continue
        seen.add(repo)
        learned = [le for le in _read(workspace, repo) if not le.pinned]
        if learned:
            lines.append(
                f"- `{skill_name(repo)}` — {len(learned)} bài học cho **{_label(repo)}**"
            )
    if not lines:
        return ""
    return (
        "\n⚠️ Bài học từ các run trước trên chính codebase này. **Chạy các skill dưới "
        "đây TRƯỚC KHI mở PR** và đừng lặp lại những gì chúng nêu:\n" + "\n".join(lines)
    )


def _sync_skills(workspace: str) -> None:
    """Write one skill per repo that has machine-learned lessons, and remove the rest.

    Pruning matters as much as writing: a repo whose lessons were all deleted would
    otherwise keep a skill on disk advertising knowledge that no longer exists, and the
    brief would go on naming it.
    """
    root = Path(workspace) / _SKILLS_SUBDIR
    wanted: set[str] = set()
    for repo in list_repos(workspace):
        body = render_skill(workspace, repo)
        if not body:
            continue
        wanted.add(skill_name(repo))
        target = skill_dir(workspace, repo) / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
    if not root.is_dir():
        return
    for child in root.iterdir():
        # Only ever touch directories this module owns — never a hand-written skill.
        if (child.is_dir() and child.name.startswith(f"{_SKILL_PREFIX}-")
                and child.name not in wanted):
            with contextlib.suppress(OSError):
                (child / "SKILL.md").unlink(missing_ok=True)
                child.rmdir()


def sync_memory(workspace: str) -> bool:
    """Write the rule file, the per-repo skills, and the CLAUDE.md pointer.

    Best-effort, never raises. Returns True when the rule file is now on disk.
    """
    if not workspace:
        return False
    with contextlib.suppress(OSError):
        _sync_skills(workspace)
    body = render_rules(workspace)
    path = memory_path(workspace)
    try:
        if not body:
            path.unlink(missing_ok=True)
            _link_from_claude_md(workspace, present=False)
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so Python translates nothing: this file is committed, and a machine
        # that rewrote it with its own platform's endings would show the whole file as
        # changed on the next machine that touched it.
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
    except OSError:
        return False
    _link_from_claude_md(workspace, present=True)
    return True


def _link_from_claude_md(workspace: str, *, present: bool) -> None:
    """Keep one pointer line to the rule file inside CLAUDE.md, between markers.

    The workspace's other rules are discoverable because CLAUDE.md indexes them, so a
    generated rule that nothing points at is relying on undocumented auto-loading. The
    markers make the edit idempotent and reversible: this module only ever replaces what
    is between them, and never reads or rewrites a byte of the rest of the file.
    """
    path = Path(workspace) / "CLAUDE.md"
    line = (
        "- `.claude/rules/autopilot-lessons.md` — bài học AI-Autopilot rút ra từ "
        "review / rework của các run trước (máy tự ghi, sửa ở `/dashboard/learning`)."
    )
    try:
        if not path.is_file():
            return                      # no CLAUDE.md to point from; the rule file stands alone
        # newline="" on BOTH ends, so Python translates nothing in either direction.
        # Reading with the default and writing it back turned this file's 338 LF endings
        # into 342 CRLF — measured — which in a tracked 20KB file is every line showing
        # as changed, from a tool that was only supposed to add one pointer.
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        eol = "\r\n" if "\r\n" in text else "\n"      # follow the file, don't impose
        block = f"{_CLAUDE_MD_START}{eol}{line}{eol}{_CLAUDE_MD_END}"
        start, end = text.find(_CLAUDE_MD_START), text.find(_CLAUDE_MD_END)
        has = start != -1 and end > start
        if not present:
            if not has:
                return
            text = text[:start].rstrip("\r\n") + text[end + len(_CLAUDE_MD_END):]
        elif has:
            if text[start:end + len(_CLAUDE_MD_END)] == block:
                return                  # already right — do not touch the file's mtime
            text = text[:start] + block + text[end + len(_CLAUDE_MD_END):]
        else:
            text = text.rstrip("\r\n") + eol + eol + block + eol
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    except OSError:
        return


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
    # Deleting a line the centre sent is a REFUSAL, not a tidy-up. Applied again on the
    # next beat it would be back within minutes, which taught operators that the delete
    # button on this page does not work.
    if any(le.text == target and le.source == SOURCE_FLEET for le in items):
        decline_fleet(workspace, target)
    return _write(workspace, repo, kept)


def clear(workspace: str, repo: str) -> bool:
    """Forget everything learned about ``repo``. True when a file was removed."""
    if not workspace or not repo:
        return False
    try:
        _lessons_path(workspace, repo).unlink()
    except OSError:
        return False
    # The only mutation that does not go through _write, so it has to say so itself —
    # otherwise "forget this repo" leaves the forgotten lines in front of the agent.
    sync_memory(workspace)
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


# ── What the centre may put on this machine ──────────────────────────────────
#
# The centre approving a line used to be the same event as the line appearing here: the
# next beat wrote it in, pinned and uncapped, and an operator who deleted it got it back
# minutes later. Measured: delete, beat, and the line is on disk again. So a worker had
# no way to refuse — the centre commanded. The asymmetry was the bug: the centre already
# had a human gate (approve / reject), and the machine that has to live with the line
# had none.
#
# A decline is therefore recorded HERE and is permanent. Both state files are kept out
# of the ``*.md`` glob ``list_repos`` reads, so neither can be mistaken for a repo.
_DECLINED_FILE = "declined.txt"
_OFFERS_FILE = "offers.json"

#: How this machine takes what the centre approved.
ACCEPT_AUTO = "auto"       # apply on arrival — but a decline still sticks, forever
ACCEPT_MANUAL = "manual"   # queue it; somebody here presses Nhận


def _state_path(workspace: str, name: str) -> Path:
    return _lessons_dir(workspace) / name


def declined_keys(workspace: str) -> set[str]:
    """Normalised keys this machine has refused. Never applied, never offered again."""
    if not workspace:
        return set()
    try:
        raw = _state_path(workspace, _DECLINED_FILE).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {ln.strip() for ln in raw.splitlines() if ln.strip()}


def decline_fleet(workspace: str, text: str) -> bool:
    """Refuse a line from the centre, for good. True when it was newly refused."""
    key = normalize(text)
    if not workspace or not key:
        return False
    keys = declined_keys(workspace)
    if key in keys:
        return False
    keys.add(key)
    path = _state_path(workspace, _DECLINED_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join(sorted(keys)) + "\n")
    except OSError:
        return False
    drop_offer(workspace, text)
    return True


def offers(workspace: str) -> list[tuple[str, str]]:
    """(repo, text) the centre approved that this machine has not answered yet."""
    if not workspace:
        return []
    try:
        raw = _state_path(workspace, _OFFERS_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    out: list[tuple[str, str]] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        repo, text = str(row.get("repo") or ""), str(row.get("text") or "")
        if text.strip():
            out.append((repo or SHARED_BUCKET, text))
    return out


def _write_offers(workspace: str, rows: list[tuple[str, str]]) -> None:
    path = _state_path(workspace, _OFFERS_FILE)
    try:
        if not rows:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            [{"repo": r, "text": t} for r, t in rows], ensure_ascii=False, indent=1
        )
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body + "\n")
    except OSError:
        return


def drop_offer(workspace: str, text: str) -> None:
    """Take one line out of the queue without deciding either way."""
    key = normalize(text)
    if not workspace or not key:
        return
    rows = [(r, t) for r, t in offers(workspace) if normalize(t) != key]
    _write_offers(workspace, rows)


def accept_offer(workspace: str, text: str) -> bool:
    """Take one queued line into this machine's store. True when it landed."""
    key = normalize(text)
    match = [(r, t) for r, t in offers(workspace) if normalize(t) == key]
    if not match:
        return False
    repo, real = match[0]
    record_lessons(
        workspace, repo, [real],
        now=datetime.now(), source=SOURCE_FLEET,  # noqa: DTZ005 — local day
    )
    drop_offer(workspace, real)
    return True


def apply_fleet(
    workspace: str, items: list[tuple[str, str]], mode: str = ACCEPT_AUTO
) -> int:
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
    refused = declined_keys(workspace)
    added = 0
    now = datetime.now()  # noqa: DTZ005 — local day, same clock the rest of the file uses
    by_repo: dict[str, list[str]] = {}
    for repo, text in items:
        clean = str(text or "").strip()
        # A line this machine refused is never applied and never queued again. Without
        # it, "delete" on a fleet line lasted until the next beat — which is not a
        # decision, it is a delay.
        if clean and normalize(clean) not in refused:
            by_repo.setdefault(str(repo or SHARED_BUCKET), []).append(clean)

    if (mode or ACCEPT_AUTO) == ACCEPT_MANUAL:
        # Queue rather than apply: the centre proposes, somebody here accepts.
        known_here = {
            normalize(le.text)
            for known_repo in list_repos(workspace)
            for le in _read(workspace, known_repo)
        }
        pending = list(offers(workspace))
        queued = {normalize(t) for _r, t in pending}
        for repo, texts in by_repo.items():
            for text in texts:
                key = normalize(text)
                if key not in known_here and key not in queued:
                    pending.append((repo, text))
                    queued.add(key)
                    added += 1
        _write_offers(workspace, pending)
        return added

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


#: Lines older builds wrote that teach nothing and cannot be acted on. The pointer one
#: is the whole reason: it says "re-read the review comments on that PR" about a pull
#: request that is closed by the time any future brief reads it, so no run could ever
#: follow it — and two of them sat permanently in the injected set saying only that
#: somebody had once been unhappy.
_DEAD_PATTERNS = (
    re.compile(r"re-?read the review comments on that pr", re.IGNORECASE),
)


def compact(workspace: str, repo: str) -> tuple[int, int]:
    """Merge duplicates and drop dead lines in a file written by an older build.

    Returns ``(merged, dropped)``. Dedup and the dead-line rule both run on WRITE, so
    a file already on disk keeps whatever it accumulated — five identical reopens and
    two useless pointers went on eating seven of the eight injection slots after the
    upgrade that fixed them, which reads as "the fix did nothing".
    """
    items = _read(workspace, repo)
    if not items:
        return (0, 0)
    kept: list[Lesson] = []
    by_key: dict[str, int] = {}
    merged = dropped = 0
    for le in items:
        if any(p.search(le.text) for p in _DEAD_PATTERNS):
            dropped += 1
            continue
        key = normalize(le.text)
        at = by_key.get(key)
        if at is None:
            by_key[key] = len(kept)
            kept.append(le)
            continue
        was = kept[at]
        merged += 1
        kept[at] = Lesson(
            repo=repo, date=max(was.date, le.date), text=was.text,
            source=_strongest(was.source, le.source), count=was.count + le.count,
        )
    if merged or dropped:
        _write(workspace, repo, kept)
    return (merged, dropped)


def compact_all(workspace: str) -> tuple[int, int]:
    """Compact every repo in the workspace. Returns the totals."""
    merged = dropped = 0
    for repo in list_repos(workspace):
        m, d = compact(workspace, repo)
        merged += m
        dropped += d
    return (merged, dropped)
