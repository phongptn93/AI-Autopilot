"""Blast radius of a finished run — is this a change a person must look at first?

The run score grades whether a run did its job. It cannot tell a typo fix from a
migration that drops a column: both "changed some files, opened a PR, tests passed".
Some files are expensive to get wrong no matter how good the run looked — schema
migrations, authentication, deploy configuration, dependency manifests — and a change
that touches a great many files is a risk by size alone. This module only classifies;
the poller decides what a high verdict means (hold the item for a person).

Pure functions, no I/O: the file list comes from the run (or its PR) and the patterns
from configuration, so the classification is trivially testable and cannot fail a run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

HIGH = "high"
NORMAL = "normal"


@dataclass
class RiskVerdict:
    level: str = NORMAL
    # Human-readable, Vietnamese — they are pasted straight into the work-item comment.
    reasons: list[str] = field(default_factory=list)
    # The files that triggered a pattern rule, for the comment's file list.
    files: list[str] = field(default_factory=list)

    @property
    def high(self) -> bool:
        return self.level == HIGH


def normalise(path: str) -> str:
    """One spelling for a path: forward slashes, no leading ``./`` or ``/``.

    PR change lists arrive as ``/src/x.cs``, a local diff as ``src\\x.cs`` on Windows;
    a pattern must match both or the gate quietly misses half the runs.
    """
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    """Glob → regex. ``**/`` = any number of folders (including none), ``**`` = anything,
    ``*`` = anything within one segment, ``?`` = one character within a segment.

    A pattern with no ``/`` matches the file NAME in any folder (``*.sql``,
    ``package.json``) — the way .gitignore reads it, and the way people write them.
    """
    pat = normalise(pattern)
    if "/" not in pat:
        pat = "**/" + pat
    out: list[str] = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


def matches(path: str, pattern: str) -> bool:
    try:
        return bool(_compile(str(pattern or "")).fullmatch(normalise(path)))
    except re.error:
        return False   # a malformed pattern must not take the gate down with it


def classify(
    files: list[str], patterns: list[str] | None, max_files: int = 0
) -> RiskVerdict:
    """The verdict for one run's changed files.

    Every matching pattern is a reason of its own (with the files it caught), so the
    comment says WHY — "migration + appsettings" — rather than just "risky".
    """
    clean = list(dict.fromkeys(normalise(f) for f in (files or []) if str(f or "").strip()))
    verdict = RiskVerdict()
    hit_files: list[str] = []
    for pattern in [p for p in (patterns or []) if str(p or "").strip()]:
        caught = [f for f in clean if matches(f, pattern)]
        if not caught:
            continue
        shown = ", ".join(caught[:5]) + (f" (+{len(caught) - 5})" if len(caught) > 5 else "")
        verdict.reasons.append(f"khớp mẫu rủi ro `{pattern}`: {shown}")
        hit_files += caught
    if max_files and max_files > 0 and len(clean) > max_files:
        verdict.reasons.append(
            f"thay đổi {len(clean)} file — vượt ngưỡng {max_files} file một lần chạy"
        )
    verdict.files = list(dict.fromkeys(hit_files))
    if verdict.reasons:
        verdict.level = HIGH
    return verdict
