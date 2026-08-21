"""Turn a unified diff into something a page can render line by line.

A PR link answers "was there a change"; it does not answer the question a reviewer
actually opens the task with — *which lines*. Following the link means leaving the tool,
finding the PR among several, and picking the right iteration, which is enough friction
that people stop looking and approve on the summary instead.

So the diff is parsed here and rendered in place. Pure text in, structured hunks out:
no git, no ADO, no I/O — the parsing rules (rename headers, binary files, "\\ No newline
at end of file", a file with no hunks at all) are exactly the kind of thing that is
tedious to get right and trivial to test once it is separated from fetching.

Everything is bounded. A diff is attacker-shaped input in the mundane sense: a
generated migration or a lockfile can be a hundred thousand lines, and a page that
renders all of it stops being a page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Bounds. A reviewer skims a task page; anything past these belongs in the PR itself,
# and the renderer says so rather than silently truncating.
MAX_FILES = 60
MAX_LINES_PER_FILE = 400
MAX_TOTAL_LINES = 4000

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


@dataclass
class DiffLine:
    """One rendered line. ``kind`` drives the colour; the numbers drive the gutters."""

    kind: str          # "add" | "del" | "ctx" | "meta"
    text: str = ""
    old_no: int | None = None
    new_no: int | None = None


@dataclass
class FileDiff:
    path: str = ""
    old_path: str = ""            # set only when the file was renamed
    status: str = "modified"      # added | deleted | renamed | modified | binary
    added: int = 0
    removed: int = 0
    lines: list[DiffLine] = field(default_factory=list)
    truncated: bool = False       # this file's body was cut at MAX_LINES_PER_FILE

    @property
    def is_renamed(self) -> bool:
        return bool(self.old_path) and self.old_path != self.path


@dataclass
class Diff:
    files: list[FileDiff] = field(default_factory=list)
    truncated_files: int = 0      # files dropped entirely because MAX_FILES was hit

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)

    @property
    def is_empty(self) -> bool:
        return not self.files


def _strip_prefix(path: str) -> str:
    """``b/src/app.py`` → ``src/app.py``; ``/dev/null`` stays as-is for detection."""
    path = path.strip()
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_unified_diff(text: str) -> Diff:
    """Parse ``git diff`` output. Tolerant: unknown lines are ignored, not fatal.

    Written against ``git diff`` specifically (that is what produces it here), but the
    format is the portable one — a diff pasted from anywhere else parses the same.
    """
    diff = Diff()
    current: FileDiff | None = None
    old_no = new_no = 0
    total = 0

    def close() -> None:
        nonlocal current
        if current is not None:
            diff.files.append(current)
            current = None

    for raw in (text or "").splitlines():
        if raw.startswith("diff --git "):
            close()
            if len(diff.files) >= MAX_FILES:
                diff.truncated_files += 1
                current = None
                continue
            parts = raw.split(" ")
            path = _strip_prefix(parts[-1]) if len(parts) >= 4 else ""
            current = FileDiff(path=path)
            old_no = new_no = 0
            continue
        if current is None:
            continue

        if raw.startswith("--- "):
            src = _strip_prefix(raw[4:])
            if src == "/dev/null":
                current.status = "added"
            else:
                current.old_path = src
            continue
        if raw.startswith("+++ "):
            dst = _strip_prefix(raw[4:])
            if dst == "/dev/null":
                current.status = "deleted"
            elif dst:
                current.path = dst
            continue
        if raw.startswith("rename from "):
            current.old_path, current.status = raw[len("rename from "):].strip(), "renamed"
            continue
        if raw.startswith("rename to "):
            current.path = raw[len("rename to "):].strip()
            continue
        if raw.startswith("new file mode"):
            current.status = "added"
            continue
        if raw.startswith("deleted file mode"):
            current.status = "deleted"
            continue
        if raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            # No lines to show, and no counts to claim: a binary file's "+0 −0" would
            # read as "nothing changed", which is the opposite of the truth.
            current.status = "binary"
            continue

        hunk = _HUNK_RE.match(raw)
        if hunk:
            old_no, new_no = int(hunk.group(1)), int(hunk.group(3))
            if not current.truncated:
                current.lines.append(DiffLine(kind="meta", text=raw))
            continue

        if not raw:
            continue
        marker, body = raw[0], raw[1:]
        if marker not in "+- \\":
            continue
        if marker == "\\":                       # "\ No newline at end of file"
            continue

        if total >= MAX_TOTAL_LINES or len(current.lines) >= MAX_LINES_PER_FILE:
            current.truncated = True
            # Keep counting +/- so the header stays honest even when the body is cut.
            if marker == "+":
                current.added += 1
                new_no += 1
            elif marker == "-":
                current.removed += 1
                old_no += 1
            continue

        if marker == "+":
            current.added += 1
            current.lines.append(DiffLine(kind="add", text=body, new_no=new_no))
            new_no += 1
        elif marker == "-":
            current.removed += 1
            current.lines.append(DiffLine(kind="del", text=body, old_no=old_no))
            old_no += 1
        else:
            current.lines.append(DiffLine(kind="ctx", text=body, old_no=old_no, new_no=new_no))
            old_no += 1
            new_no += 1
        total += 1

    close()
    # A rename with no content change produces headers and no hunks; keep it, because
    # "this file moved" is exactly the kind of change a reviewer must not miss.
    return diff
