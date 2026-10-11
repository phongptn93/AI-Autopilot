"""Which code a review reads — and making sure it is the code that actually ships.

A review used to read the workspace checkout as it sat on disk. On a real machine that
checkout is whatever branch somebody last left there: one repo current, another two
months and 1 571 commits behind. The front end then called endpoints the stale back
end "did not have", and the review reported working features as missing — correct
about the disk, wrong about the system.

So a review now states its code version, and by default reads the LATEST upstream of
each repo's branch through a throwaway git worktree:

* ``git fetch`` per repo (best-effort; offline falls back to the last fetched tip);
* ``git worktree add --detach`` at that tip, inside the autopilot's own data folder —
  the user's checkout, index and stash are never touched;
* the workspace's ``CLAUDE.md`` / ``.claude`` are copied in so the agent keeps the
  team's rules;
* trees are removed after ``TREE_TTL_HOURS`` (follow-ups and challenges reuse them).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_autopilot.logging_config import get_logger
from ai_autopilot.workspace import discover_repos

_log = get_logger(__name__)
TREE_TTL_HOURS = 24
_GIT_TIMEOUT = 120


@dataclass
class RepoState:
    repo: str
    branch: str = ""
    upstream: str = ""
    commit: str = ""          # the commit the review READ
    commit_date: str = ""
    subject: str = ""
    behind: int = 0           # local checkout vs upstream (after fetch)
    ahead: int = 0
    dirty: bool = False       # local checkout has uncommitted changes
    source: str = "checkout"  # "upstream" | "checkout" | "head-fallback"
    note: str = ""


def _git(cwd: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                         timeout=timeout, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "git failed").strip()[:300])
    return out.stdout.strip()


def _try(cwd: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> str:
    try:
        return _git(cwd, *args, timeout=timeout)
    except Exception:  # noqa: BLE001 — every probe is optional
        return ""


def state(repo_dir: Path, *, fetch: bool) -> RepoState:
    """Branch, upstream and how far the local checkout lags it."""
    st = RepoState(repo=repo_dir.name)
    st.branch = _try(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    st.upstream = _try(repo_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if fetch and st.upstream:
        remote = st.upstream.split("/", 1)[0]
        branch = st.upstream.split("/", 1)[1] if "/" in st.upstream else ""
        if not _try(repo_dir, "fetch", "--quiet", remote, branch) and not _try(
                repo_dir, "rev-parse", "--verify", st.upstream):
            st.note = "không fetch được — dùng bản đã fetch gần nhất"
    if st.upstream:
        counts = _try(repo_dir, "rev-list", "--left-right", "--count", f"HEAD...{st.upstream}")
        if counts:
            a, b = (counts.split() + ["0", "0"])[:2]
            st.ahead, st.behind = int(a or 0), int(b or 0)
    st.dirty = bool(_try(repo_dir, "status", "--porcelain", "--untracked-files=no"))
    return st


def _describe(repo_dir: Path, ref: str, st: RepoState) -> None:
    line = _try(repo_dir, "log", "-1", "--format=%h%x09%cI%x09%s", ref)
    if line:
        parts = (line.split("\t") + ["", "", ""])[:3]
        st.commit, st.subject = parts[0], parts[2][:120]
        st.commit_date = parts[1][:16].replace("T", " ")


def inspect(workspace: str, *, fetch: bool = False) -> list[RepoState]:
    """State of every repo in the workspace — for the page, before anything runs."""
    out = []
    for name in discover_repos(workspace):
        st = state(Path(workspace) / name, fetch=fetch)
        _describe(Path(workspace) / name, "HEAD", st)
        out.append(st)
    return out


def prepare(workspace: str, trees_root: Path, review_id: int, *, latest: bool,
            ) -> tuple[str, list[RepoState]]:
    """``(directory the agent runs in, what it reads)``.

    ``latest=False`` → the workspace itself (as before), but described honestly.
    ``latest=True``  → a fresh folder of detached worktrees at each repo's upstream tip.
    Any repo that cannot be prepared that way is read from its checkout and SAYS so.
    """
    repos = discover_repos(workspace)
    if not latest or not repos:
        states = []
        for name in repos:
            st = state(Path(workspace) / name, fetch=False)
            _describe(Path(workspace) / name, "HEAD", st)
            states.append(st)
        return workspace, states

    cleanup(trees_root)
    root = trees_root / f"review-{review_id}"
    if root.exists():
        remove_tree(root)
    root.mkdir(parents=True, exist_ok=True)
    ws = Path(workspace)
    for extra in ("CLAUDE.md", ".claude", ".mcp.json"):
        src = ws / extra
        try:
            if src.is_dir():
                shutil.copytree(src, root / extra, ignore=shutil.ignore_patterns(".git"))
            elif src.is_file():
                shutil.copy2(src, root / extra)
        except OSError:
            pass
    states = []
    for name in repos:
        repo_dir = ws / name
        st = state(repo_dir, fetch=True)
        usable = st.upstream and _try(repo_dir, "rev-parse", "--verify", st.upstream)
        ref = st.upstream if usable else "HEAD"
        try:
            _git(repo_dir, "worktree", "add", "--detach", "--force", str(root / name), ref,
                 timeout=600)
            st.source = "upstream" if ref != "HEAD" else "head-fallback"
            if ref == "HEAD":
                st.note = st.note or "nhánh không có upstream — rà bản commit hiện tại"
        except Exception as exc:  # noqa: BLE001
            # Fall back to the checkout for THIS repo, visibly.
            st.source, st.note = "checkout", f"không tạo được bản mới nhất: {exc}"[:200]
            _link_checkout(repo_dir, root / name)
            ref = "HEAD"
        _describe(repo_dir, ref, st)
        states.append(st)
    (root / ".review-snapshot.json").write_text(
        json.dumps([asdict(s) for s in states], ensure_ascii=False), encoding="utf-8")
    return str(root), states


def _link_checkout(src: Path, dst: Path) -> None:
    """Make a repo that could not be checked out still readable under the tree root."""
    try:
        dst.symlink_to(src, target_is_directory=True)
    except OSError:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "node_modules", "bin",
                                                                  "obj", "dist", ".aiwt"),
                        dirs_exist_ok=True)


def remove_tree(root: Path) -> None:
    """Remove one review's worktrees and folder; prune the repos' worktree lists."""
    for child in root.iterdir() if root.exists() else []:
        gitfile = child / ".git"
        if gitfile.is_file():
            try:
                main = gitfile.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()
                repo = Path(main).parents[2]           # <repo>/.git/worktrees/<name>
                _try(repo, "worktree", "remove", "--force", str(child), timeout=120)
            except (IndexError, OSError):
                pass
    shutil.rmtree(root, ignore_errors=True)


def cleanup(trees_root: Path, ttl_hours: float = TREE_TTL_HOURS) -> int:
    """Drop review trees older than the TTL. Returns how many were removed."""
    if not trees_root.exists():
        return 0
    cutoff = time.time() - ttl_hours * 3600
    removed = 0
    for d in trees_root.glob("review-*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                remove_tree(d)
                removed += 1
        except OSError:
            continue
    return removed


def from_json(raw: str) -> list[RepoState]:
    try:
        return [RepoState(**d) for d in json.loads(raw or "[]")]
    except (ValueError, TypeError):
        return []


def to_json(states: list[RepoState]) -> str:
    return json.dumps([asdict(s) for s in states], ensure_ascii=False)
