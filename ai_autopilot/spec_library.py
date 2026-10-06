"""The workspace's specs, as something a BA can find and read.

Specs were written (``spec-with-ui-mockup`` puts a spec, a dev brief and a mockup under
``specs/active/<slug>/``) and then effectively lost: the only place the dashboard
showed them was a task room's preview tab, as the forty most recent HTML files of the
whole workspace, unfiltered — and a Markdown spec, the actual document, not at all.

This module finds them, links each one to its work item when the file says which, and
reads one safely. Read-only: nothing here writes a file.

**Containment is the whole security story.** A path arrives in a query string, so it
is resolved and must land inside the workspace AND inside one of the spec folders,
and it must be a ``.md`` or ``.html`` file. A repo holds keys and source; "read any
file under the workspace" would be an exfiltration endpoint.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Where specs live, relative to the workspace (or one level down, inside a repo).
SPEC_DIRS = ("specs", "specs-dxfac", "docs/specs")
_SUFFIXES = (".md", ".html")
_MAX_FILES = 400
_MAX_READ = 2_000_000          # bytes — a spec is a document, not a dump

# "ADO: #8526", "Work item: 8526", "work_item_id: 8526", "[#8526]", "ID: 8526" — the
# shapes the spec skills and people actually write, matched near the top of the file.
_ITEM_REFS = (
    re.compile(r"(?:ADO|work[\s_-]*item(?:[\s_-]*id)?|ticket|requirement|req|ID)"
               r"\s*[:#=]\s*#?(\d{3,7})", re.IGNORECASE),
    re.compile(r"_workitems/edit/(\d{3,7})"),
    re.compile(r"\[#(\d{3,7})\]"),
)
_H1 = re.compile(r"^\s*#\s+(.+?)\s*#*\s*$", re.MULTILINE)
_HTML_TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass
class Spec:
    rel: str                      # path relative to the workspace, POSIX
    title: str
    kind: str                     # "md" | "html"
    folder: str                   # the feature folder it sits in
    item_id: int = 0
    modified: float = 0.0
    siblings: list[str] = field(default_factory=list)   # other files in the folder


def _roots(workspace: Path) -> list[Path]:
    """Every spec folder: in the workspace itself, and one level down in each repo."""
    roots: list[Path] = []
    for base in (workspace, *(p for p in _children(workspace) if p.is_dir())):
        for sub in SPEC_DIRS:
            candidate = base / sub
            if candidate.is_dir():
                roots.append(candidate)
    return roots


def _children(path: Path) -> list[Path]:
    try:
        return [p for p in path.iterdir() if not p.name.startswith(".")]
    except OSError:
        return []


def _head(path: Path, size: int = 6000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(size)
    except OSError:
        return ""


def item_ref(text: str) -> int:
    """The work item a spec names in its opening lines, or 0."""
    for pattern in _ITEM_REFS:
        hit = pattern.search(text or "")
        if hit:
            return int(hit.group(1))
    return 0


def title_of(text: str, kind: str, fallback: str) -> str:
    hit = (_H1 if kind == "md" else _HTML_TITLE).search(text or "")
    title = re.sub(r"\s+", " ", hit.group(1)).strip() if hit else ""
    return title or fallback


def discover(workspace: str) -> list[Spec]:
    """Every spec file under the workspace's spec folders, newest first."""
    root = Path(workspace or "")
    if not root.is_dir():
        return []
    root = root.resolve()
    found: list[Spec] = []
    for base in _roots(root):
        try:
            paths = [p for p in base.rglob("*") if p.suffix.lower() in _SUFFIXES and p.is_file()]
        except OSError:
            continue
        for path in paths[:_MAX_FILES]:
            try:
                stat = path.stat()
            except OSError:
                continue
            kind = path.suffix.lower().lstrip(".")
            head = _head(path)
            found.append(Spec(
                rel=path.relative_to(root).as_posix(),
                title=title_of(head, kind, path.stem.replace("-", " ")),
                kind=kind, folder=path.parent.relative_to(root).as_posix(),
                item_id=item_ref(head), modified=stat.st_mtime,
            ))
    by_folder: dict[str, list[Spec]] = {}
    for spec in found:
        by_folder.setdefault(spec.folder, []).append(spec)
    for group in by_folder.values():
        # A mockup or a brief rarely names the item; its spec does. Same folder, same
        # feature — so every file in the folder inherits the id one of them states.
        ids = {s.item_id for s in group if s.item_id}
        shared = ids.pop() if len(ids) == 1 else 0
        for spec in group:
            spec.item_id = spec.item_id or shared
            spec.siblings = [s.rel for s in group if s.rel != spec.rel]
    found.sort(key=lambda s: s.modified, reverse=True)
    return found


def resolve(workspace: str, rel: str) -> Path | None:
    """The file ``rel`` names, if — and only if — it is a spec file we may show."""
    root = Path(workspace or "")
    if not root.is_dir() or not rel:
        return None
    root = root.resolve()
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return None
    if target.suffix.lower() not in _SUFFIXES or not target.is_file():
        return None
    allowed = _roots(root)
    if not any(_inside(target, base.resolve()) for base in allowed):
        return None
    return target


def _inside(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def read(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(_MAX_READ)
    except OSError:
        return ""
