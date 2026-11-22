"""Code health from git alone — hotspots, knowledge concentration, debt markers.

A dev team asks three questions about its codebase that no ticket answers:

* **Where does change hurt?** A file edited 40 times in a quarter that is also 2,000
  lines long is where bugs, conflicts and slow reviews come from. Change frequency is
  the strongest single predictor of defects we can read without running anything;
  size stands in for complexity. ``score = commits × log2(lines)``.
* **Who knows this part?** A module where one person wrote 85% of the changes is a
  risk the day that person is on leave. Worse when that person has not committed for
  a month: the knowledge may already be gone.
* **What did we promise to come back to?** ``TODO`` / ``FIXME`` / ``HACK`` / ``XXX``
  markers are debt the team wrote down itself.

Deterministic, read-only, no model call. Every git failure becomes
``status="unknown"`` with the reason — "could not check" never reads as "clean".
"""

from __future__ import annotations

import math
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape as esc
from pathlib import Path, PurePosixPath
from typing import Any

_GIT_TIMEOUT_SECONDS = 60
_MAX_FILE_BYTES = 2_000_000
_MARKER_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
MARKER_KINDS = ("FIXME", "HACK", "XXX", "TODO")   # most urgent first

# Paths whose churn says nothing about the code people maintain: lockfiles, build
# output, vendored and generated files. Matched on any path segment or suffix.
_IGNORED_DIRS = {
    "node_modules", "dist", "build", "bin", "obj", "out", "vendor", "coverage",
    ".venv", "venv", "__pycache__", ".git", "packages", "wwwroot", ".angular",
}
_IGNORED_SUFFIXES = (
    ".lock", "-lock.json", "package-lock.json", ".min.js", ".min.css", ".map",
    ".designer.cs", ".g.cs", ".snap", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".pdf", ".docx", ".xlsx", ".pptx", ".zip", ".dll", ".exe", ".woff", ".woff2", ".ttf",
)
_IGNORED_NAMES = {"poetry.lock", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "go.sum"}
# EF Core snapshots and migrations are regenerated, not hand-edited.
_IGNORED_PARTS_RE = re.compile(r"(^|/)Migrations/.*\.cs$|ModelSnapshot\.cs$", re.IGNORECASE)

# Knowledge-risk thresholds. A module needs some history before one author's share
# means anything: two commits by one person is a new folder, not a silo.
CONCENTRATION_SHARE = 0.8
CONCENTRATION_MIN_COMMITS = 5
AWAY_DAYS = 30


def is_ignored(path: str) -> bool:
    p = path.replace("\\", "/")
    parts = p.split("/")
    name = parts[-1].lower()
    if name in _IGNORED_NAMES or name.endswith(_IGNORED_SUFFIXES):
        return True
    if any(seg in _IGNORED_DIRS for seg in parts[:-1]):
        return True
    return bool(_IGNORED_PARTS_RE.search(p))


def module_of(path: str) -> str:
    """The unit people own: two folders deep, so ``src/orders/x.py`` → ``src/orders``."""
    parts = PurePosixPath(path).parts
    if len(parts) > 2:
        return "/".join(parts[:2])
    if len(parts) == 2:
        return parts[0]
    return "(gốc repo)"


def _resolve_rename(path: str) -> str:
    """numstat renames: ``a => b`` and ``dir/{old => new}/f`` → the new path."""
    if "=>" not in path:
        return path
    if "{" in path and "}" in path:
        pre, rest = path.split("{", 1)
        inner, post = rest.split("}", 1)
        new = inner.split("=>", 1)[1].strip()
        return (pre + new + post).replace("//", "/")
    return path.split("=>", 1)[1].strip()


@dataclass
class Commit:
    sha: str
    author: str
    email: str
    when: datetime
    files: list[tuple[str, int]]   # (path, lines added + deleted)


def parse_log(text: str) -> list[Commit]:
    """Parse ``git log --numstat --format=%x1e%H%x1f%aN%x1f%aE%x1f%at``."""
    commits: list[Commit] = []
    for block in text.split("\x1e"):
        block = block.strip("\n")
        if not block:
            continue
        head, _, body = block.partition("\n")
        fields = head.split("\x1f")
        if len(fields) < 4:
            continue
        sha, author, email, ts = fields[:4]
        try:
            when = datetime.fromtimestamp(int(ts), tz=UTC)
        except ValueError:
            continue
        files: list[tuple[str, int]] = []
        for line in body.splitlines():
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            added, deleted, path = cols
            # Binary files report "-": they changed, but carry no line count.
            n = (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
            path = _resolve_rename(path)
            if not is_ignored(path):
                files.append((path, n))
        commits.append(Commit(sha, author.strip(), email.strip().lower(), when, files))
    return commits


@dataclass
class Hotspot:
    path: str
    commits: int
    churn: int
    lines: int
    authors: int
    last_changed: datetime
    score: float


@dataclass
class ModuleKnowledge:
    module: str
    commits: int
    authors: int
    main_author: str
    main_share: float           # 0..1 of the module's changed lines
    main_last_seen: datetime | None
    risk: str                   # "" | "concentrated" | "away"


@dataclass
class Marker:
    kind: str
    path: str
    line: int
    text: str


@dataclass
class CodeHealth:
    repo: str
    days: int
    status: str = "ok"          # "ok" | "unknown"
    reason: str = ""
    head: str = ""
    commits: int = 0
    contributors: int = 0
    files_changed: int = 0
    hotspots: list[Hotspot] = field(default_factory=list)
    modules: list[ModuleKnowledge] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    marker_counts: dict[str, int] = field(default_factory=dict)
    marker_files: list[tuple[str, int]] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def risky_modules(self) -> list[ModuleKnowledge]:
        return [m for m in self.modules if m.risk]

    @property
    def marker_total(self) -> int:
        return sum(self.marker_counts.values())


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT_SECONDS, check=False,
    )


def _count_lines(path: Path) -> int:
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return 0
        data = path.read_bytes()
    except OSError:
        return 0
    if b"\x00" in data[:8000]:
        return 0          # binary
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def hotspots(commits: list[Commit], repo: Path, limit: int = 15) -> list[Hotspot]:
    stats: dict[str, dict] = {}
    for c in commits:
        for path, n in c.files:
            s = stats.setdefault(path, {"commits": 0, "churn": 0, "authors": set(),
                                        "last": c.when})
            s["commits"] += 1
            s["churn"] += n
            s["authors"].add(c.email or c.author)
            s["last"] = max(s["last"], c.when)
    out: list[Hotspot] = []
    for path, s in stats.items():
        lines = _count_lines(repo / path)
        if lines == 0:
            continue      # deleted since, or binary: nothing left to maintain
        score = s["commits"] * math.log2(lines + 1)
        out.append(Hotspot(path, s["commits"], s["churn"], lines, len(s["authors"]),
                           s["last"], round(score, 1)))
    out.sort(key=lambda h: (-h.score, h.path))
    return out[:limit]


def knowledge(commits: list[Commit], now: datetime) -> list[ModuleKnowledge]:
    churn: dict[str, Counter[str]] = defaultdict(Counter)
    touches: Counter[str] = Counter()
    names: dict[str, str] = {}
    last_seen: dict[str, datetime] = {}
    # Oldest first, so the name kept for an email is the most recent one used.
    for c in sorted(commits, key=lambda c: c.when):
        who = c.email or c.author
        names[who] = c.author or who
        last_seen[who] = c.when
        mods_in_commit = set()
        for path, n in c.files:
            mod = module_of(path)
            churn[mod][who] += max(n, 1)
            mods_in_commit.add(mod)
        for mod in mods_in_commit:
            touches[mod] += 1
    out: list[ModuleKnowledge] = []
    for mod, by_author in churn.items():
        total = sum(by_author.values())
        who, top = by_author.most_common(1)[0]
        share = top / total if total else 0.0
        seen = last_seen.get(who)
        risk = ""
        if touches[mod] >= CONCENTRATION_MIN_COMMITS and share >= CONCENTRATION_SHARE:
            away = seen is not None and (now - seen).days >= AWAY_DAYS
            risk = "away" if away else "concentrated"
        out.append(ModuleKnowledge(mod, touches[mod], len(by_author), names.get(who, who),
                                   round(share, 3), seen, risk))
    rank = {"away": 0, "concentrated": 1, "": 2}
    out.sort(key=lambda m: (rank[m.risk], -m.commits, m.module))
    return out


def parse_markers(text: str, limit: int = 40) -> tuple[dict[str, int], list[tuple[str, int]],
                                                       list[Marker]]:
    """Parse ``git grep -n -I -E`` output → (counts per kind, top files, sample)."""
    counts: Counter[str] = Counter()
    per_file: Counter[str] = Counter()
    sample: list[Marker] = []
    for line in text.splitlines():
        path, sep, rest = line.partition(":")
        num, sep2, content = rest.partition(":")
        if not sep or not sep2 or not num.isdigit() or is_ignored(path):
            continue
        m = _MARKER_RE.search(content)
        if not m:
            continue
        kind = m.group(1)
        counts[kind] += 1
        per_file[path] += 1
        sample.append(Marker(kind, path, int(num), content.strip()[:160]))
    order = {k: i for i, k in enumerate(MARKER_KINDS)}
    sample.sort(key=lambda mk: (order[mk.kind], mk.path, mk.line))
    return ({k: counts[k] for k in MARKER_KINDS if counts[k]},
            per_file.most_common(10), sample[:limit])


def analyze(repo: str | Path, days: int = 90, now: datetime | None = None) -> CodeHealth:
    root = Path(repo)
    days = max(7, min(int(days or 90), 730))
    report = CodeHealth(repo=str(root), days=days)
    now = now or datetime.now(UTC)
    if not (root / ".git").exists():
        report.status, report.reason = "unknown", "Thư mục này không phải git repo."
        return report
    try:
        head = _git(root, "rev-parse", "--short", "HEAD")
        if head.returncode != 0:
            report.status = "unknown"
            report.reason = (head.stderr.strip() or "git rev-parse lỗi")[:300]
            return report
        report.head = head.stdout.strip()
        log = _git(root, "log", f"--since={days}.days", "--no-merges", "--numstat",
                   "--format=%x1e%H%x1f%aN%x1f%aE%x1f%at")
        if log.returncode != 0:
            report.status = "unknown"
            report.reason = (log.stderr.strip() or "git log lỗi")[:300]
            return report
        grep = _git(root, "grep", "-n", "-I", "-E", r"\b(TODO|FIXME|HACK|XXX)\b")
    except FileNotFoundError:
        report.status, report.reason = "unknown", "Không tìm thấy lệnh git trên máy này."
        return report
    except subprocess.TimeoutExpired:
        report.status = "unknown"
        report.reason = f"git chạy quá {_GIT_TIMEOUT_SECONDS} giây — repo quá lớn để quét tại chỗ."
        return report

    commits = parse_log(log.stdout)
    report.commits = len(commits)
    report.contributors = len({c.email or c.author for c in commits})
    report.files_changed = len({p for c in commits for p, _ in c.files})
    report.hotspots = hotspots(commits, root)
    report.modules = knowledge(commits, now)
    # git grep: 0 = matches, 1 = none, anything else = it could not look.
    if grep.returncode in (0, 1):
        report.marker_counts, report.marker_files, report.markers = parse_markers(grep.stdout)
    else:
        why = (grep.stderr.strip() or "git grep lỗi")[:200]
        report.reason = f"Không đếm được TODO/FIXME: {why}"
    return report


# One scan per (repo, window, HEAD): a page reload on an unchanged repo is free.
_CACHE: dict[tuple[str, int, str], CodeHealth] = {}
_CACHE_MAX = 16


def cached_analyze(repo: str | Path, days: int = 90) -> CodeHealth:
    root = Path(repo)
    try:
        head = _git(root, "rev-parse", "HEAD").stdout.strip() if (root / ".git").exists() else ""
    except (OSError, subprocess.TimeoutExpired):
        head = ""
    key = (str(root), int(days), head)
    if head and key in _CACHE:
        return _CACHE[key]
    report = analyze(root, days)
    if head and report.status == "ok":
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = report
    return report


# ── Where the agent's changes land on that map ─────────────────────────────


@dataclass(frozen=True)
class RepoChoice:
    key: str     # what a query string carries
    label: str
    path: str


def repo_choices(cfg: Any) -> list[RepoChoice]:
    """Every git repo this machine is configured to work in, each once.

    The ONLY way a repo reaches :func:`analyze` from outside: by name, from the
    configured workspaces — never a path someone typed.
    """
    import contextlib

    from ai_autopilot import workspaces as workspaces_mod
    from ai_autopilot.workspace import discover_repos

    roots: list[tuple[str, str]] = []   # (workspace label, directory)
    with contextlib.suppress(Exception):
        for ws in workspaces_mod.resolve(cfg):
            if getattr(ws, "directory", ""):
                roots.append((getattr(ws, "name", "") or ws.id, ws.directory))
    if getattr(cfg, "workspace_directory", ""):
        roots.append(("", cfg.workspace_directory))

    found: dict[str, tuple[str, str]] = {}    # resolved path -> (name, workspace)
    for ws_label, root in roots:
        for name in discover_repos(root):
            full = str(Path(root) / name)
            found.setdefault(str(Path(full).resolve()), (name, ws_label))
        if (Path(root) / ".git").exists():   # a workspace that is itself one repo
            found.setdefault(str(Path(root).resolve()), (Path(root).name, ws_label))
    single = getattr(cfg, "repo_working_directory", "") or ""
    if single and (Path(single) / ".git").exists():
        found.setdefault(str(Path(single).resolve()), (Path(single).name, ""))

    names = [n for n, _ in found.values()]
    out: list[RepoChoice] = []
    for path, (name, ws_label) in found.items():
        clash = names.count(name) > 1
        key = f"{ws_label}/{name}" if clash and ws_label else name
        label = f"{name} ({ws_label})" if clash and ws_label else name
        out.append(RepoChoice(key, label, path))
    out.sort(key=lambda r: r.label.lower())
    return out


def repo_path_for(cfg: Any, repo_name: str) -> str:
    """Local checkout of an ADO repo, by name (case-insensitive); "" when not here."""
    want = (repo_name or "").strip().lower()
    for r in repo_choices(cfg):
        if Path(r.path).name.lower() == want:
            return r.path
    return ""


@dataclass
class ReviewHints:
    hotspots: list[Hotspot] = field(default_factory=list)
    owners: list[ModuleKnowledge] = field(default_factory=list)   # modules touched

    @property
    def worth_saying(self) -> bool:
        """Only speak when there is something a reviewer would act on."""
        return bool(self.hotspots) or any(m.risk for m in self.owners)


def _relative(path: str, repo_name: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    prefix = repo_name.lower() + "/"
    return p[len(prefix):] if p.lower().startswith(prefix) else p


def review_hints(report: CodeHealth, files: list[str], repo_name: str = "") -> ReviewHints:
    """What the changed ``files`` touch on the repo's health map."""
    rel = {_relative(f, repo_name) for f in files if f}
    hints = ReviewHints()
    if report.status != "ok" or not rel:
        return hints
    hints.hotspots = [h for h in report.hotspots if h.path in rel]
    touched = {module_of(f) for f in rel}
    hints.owners = [m for m in report.modules if m.module in touched]
    return hints


def render_hints(hints: ReviewHints, days: int) -> str:
    """The PR comment: who knows the code this PR changes, and which files are hot."""
    rows = []
    if hints.owners:
        rows.append("<b>👥 Người nắm phần code bị sửa</b> (gợi ý reviewer):<ul>")
        for m in hints.owners[:6]:
            note = ""
            if m.risk == "away":
                note = f" — ⚠️ đã vắng ≥ {AWAY_DAYS} ngày, cần người thứ hai đọc kỹ"
            elif m.risk == "concentrated":
                note = " — ⚠️ chỉ một người nắm, nên có họ review"
            rows.append(f"<li><code>{esc(m.module)}</code>: {esc(m.main_author)} "
                        f"({round(m.main_share * 100)}% thay đổi){note}</li>")
        rows.append("</ul>")
    if hints.hotspots:
        rows.append("<b>🔥 PR sửa điểm nóng</b> (hay sửa + dài — dễ sinh lỗi, nên có test):<ul>")
        for h in hints.hotspots[:6]:
            rows.append(f"<li><code>{esc(h.path)}</code>: {h.commits} lần sửa / {days} ngày, "
                        f"{h.lines:,} dòng</li>")
        rows.append("</ul>")
    return ("<div><b>🩻 Sức khỏe mã nguồn</b> — đọc từ git, chỉ để tham khảo.<br>"
            + "".join(rows) + "</div>")
