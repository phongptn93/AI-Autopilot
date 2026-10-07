"""Replay evals — an exam built from real merged work.

The configuration evals in :mod:`ai_autopilot.evals` ask the agent questions about its
own rules. They catch a skill that stopped saying the right thing; they cannot catch a
skill that still says it and no longer produces working code. For that the only honest
test is the job itself: take a change a human already shipped, rewind the repo to the
commit just before it, hand the agent the same task, and compare.

Every case is a fact the repo already knows — the commit the work started from, the
files the human touched, the test command that was green when it merged — so nothing has
to be invented to grade a run, and nobody has to agree on what "good" looks like.

Three layers, kept apart on purpose so the expensive one is the only one that needs a
model:

- :func:`harvest` turns git history into case files. Pure git, no network; a tracker
  is consulted only when one is configured, and its absence never fails the harvest.
- :func:`run_replay_case` builds a throwaway worktree at the base commit, lets an
  injected agent work in it, collects what changed and runs the tests. The worktree is
  removed in a ``finally`` — an exam that leaves forty detached worktrees behind gets
  run once.
- :func:`score_case` is a pure function from those observations to a verdict, so the
  rules that decide pass and fail are unit-testable without a model or a repo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.proc import spawn_kwargs, terminate_tree

_log = get_logger("replay_eval")

KIND = "replay"

# A work-item reference in a commit message: "#1234" or Azure Boards' "AB#1234". The
# leading guard keeps "issue#12" and HTML entities like "&#123;" out.
_WI_REF = re.compile(r"(?<![\w#&])(?:AB)?#(\d{1,7})\b")
# The prefix Azure Repos writes into a squash/merge commit. It names the PR, not the
# task, and repeating it into the brief only teaches the agent that tasks begin with
# "Merged PR".
_MERGED_PR_PREFIX = re.compile(r"^\s*Merged PR \d+:\s*", re.I)
# A conflict marker is a line that STARTS with seven of these and a space (or ends the
# line). `=======` alone is left out: a Markdown/RST underline is exactly that.
_CONFLICT_MARKER = re.compile(r"^(?:<{7}|>{7})(?: |$)", re.M)

_GIT_TIMEOUT_SECONDS = 120


# ─────────────────────────────── case format ───────────────────────────────


@dataclass
class ReplayCase:
    """One piece of shipped work, rewound to where it started."""

    name: str
    repo: str                         # folder name under the workspace (or a path)
    base_commit: str
    title: str
    description: str = ""
    test_command: str = ""            # blank = the repo's configured / detected runner
    expected_files: list[str] = field(default_factory=list)
    expected_diff_lines: int = 0      # size of the human change, for scale; 0 = unknown
    timeout_minutes: int = 30
    work_item_id: int = 0
    merge_commit: str = ""            # the commit the human change landed as
    # An example case documents the format and is skipped unless asked for: it names a
    # repo and a commit that exist nowhere, and a suite that fails on its own README
    # trains people to ignore it.
    example: bool = False
    source: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], source: str = "") -> ReplayCase:
        task = data.get("task") or {}
        if isinstance(task, str):       # shorthand: the whole task is one block of text
            title, _, desc = task.strip().partition("\n")
            task = {"title": title, "description": desc.strip()}
        files = data.get("expected_files") or []
        if isinstance(files, str):
            files = [f for f in files.splitlines() if f.strip()]
        return cls(
            name=str(data.get("name") or Path(source).stem or "unnamed"),
            repo=str(data.get("repo") or "").strip(),
            # YAML reads an all-digit SHA as an int; str() of it is still the SHA.
            base_commit=_text(data.get("base_commit")),
            title=str(task.get("title") or "").strip(),
            description=str(task.get("description") or "").strip(),
            test_command=str(data.get("test_command") or "").strip(),
            expected_files=[_norm_path(f) for f in files if str(f).strip()],
            expected_diff_lines=int(data.get("expected_diff_lines") or 0),
            timeout_minutes=int(data.get("timeout_minutes") or 30),
            work_item_id=int(data.get("work_item_id") or 0),
            merge_commit=_text(data.get("merge_commit")),
            example=bool(data.get("example", False)),
            source=source,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": KIND,
            "name": self.name,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "task": {"title": self.title, "description": self.description},
            "expected_files": list(self.expected_files),
            "timeout_minutes": self.timeout_minutes,
        }
        if self.test_command:
            out["test_command"] = self.test_command
        if self.expected_diff_lines:
            out["expected_diff_lines"] = self.expected_diff_lines
        if self.work_item_id:
            out["work_item_id"] = self.work_item_id
        if self.merge_commit:
            out["merge_commit"] = self.merge_commit
        if self.example:
            out["example"] = True
        return out

    def problems(self) -> list[str]:
        """What makes this case unrunnable — empty means it can be replayed."""
        out = []
        if not self.repo:
            out.append("no repo")
        if not self.base_commit:
            out.append("no base_commit")
        if not (self.title or self.description):
            out.append("no task text")
        return out


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm_path(p: Any) -> str:
    text = str(p).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def is_replay_entry(data: Any) -> bool:
    return isinstance(data, dict) and (
        str(data.get("kind") or "").lower() == KIND
        or ("base_commit" in data and "task" in data)
    )


def load_replay_cases(directory: str | Path, *, include_examples: bool = False,
                      name: str = "") -> list[ReplayCase]:
    """Every replay case under ``directory``, by name.

    Same tolerance as :func:`ai_autopilot.evals.load_cases`: a file that will not parse
    is logged and skipped, because one bad case must not hide the verdict of the rest.
    Entries without ``kind: replay`` (or a ``base_commit`` + ``task``) are not ours and
    are ignored, so the two suites can share a directory.
    """
    root = Path(directory)
    paths = [root] if root.is_file() else (sorted(root.rglob("*")) if root.is_dir() else [])
    out: list[ReplayCase] = []
    for path in paths:
        if path.suffix.lower() not in (".yaml", ".yml", ".json") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if path.suffix.lower() == ".json" else _yaml_load(text)
        except Exception as exc:  # noqa: BLE001 — one bad file must not hide the rest
            _log.warning("replay case failed to parse - skipped",
                         path=str(path), error=describe_exc(exc))
            continue
        for entry in (data if isinstance(data, list) else [data]):
            if not is_replay_entry(entry):
                continue
            case = ReplayCase.from_dict(entry, source=str(path))
            if case.example and not include_examples:
                continue
            if name and case.name != name:
                continue
            out.append(case)
    return sorted(out, key=lambda c: c.name)


def _yaml_load(text: str) -> Any:
    import yaml

    return yaml.safe_load(text)


def dump_case_yaml(case: ReplayCase) -> str:
    import yaml

    header = ("# Replay case harvested from git history — see evals/README.md, "
              "\"Replay evals\".\n")
    return header + yaml.safe_dump(case.to_dict(), sort_keys=False, allow_unicode=True,
                                   width=100)


# ─────────────────────────────── scoring ───────────────────────────────

TEST_PASSED = "passed"
TEST_FAILED = "failed"
TEST_SKIPPED = "skipped"     # no runner, or the runner is not installed here
TEST_TIMEOUT = "timeout"


@dataclass
class ReplayScore:
    name: str
    passed: bool
    tests: str = TEST_SKIPPED
    files_overlap: float = 0.0          # Jaccard of touched vs expected
    touched_files: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    missed_files: list[str] = field(default_factory=list)    # human touched, agent did not
    extra_files: list[str] = field(default_factory=list)     # agent touched, human did not
    conflict_files: list[str] = field(default_factory=list)
    diff_lines: int = 0
    expected_diff_lines: int = 0
    reasons: list[str] = field(default_factory=list)          # why it failed
    notes: list[str] = field(default_factory=list)            # informational only
    error: str = ""
    duration_seconds: float = 0.0
    test_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa = {_norm_path(x) for x in a if _norm_path(x)}
    sb = {_norm_path(x) for x in b if _norm_path(x)}
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def find_conflict_markers(contents: Mapping[str, str]) -> list[str]:
    """Files whose text still carries a merge-conflict marker."""
    return sorted(p for p, text in contents.items() if text and _CONFLICT_MARKER.search(text))


def score_case(
    case: ReplayCase,
    *,
    touched_files: Iterable[str],
    test_status: str,
    conflict_files: Iterable[str] = (),
    diff_lines: int = 0,
    error: str = "",
    duration_seconds: float = 0.0,
    test_output: str = "",
) -> ReplayScore:
    """The verdict on one replay, from what was observed. Pure: no git, no model.

    A run passes when ALL of these hold — each a fact, none a judgement of style:

    - the agent changed something (a run that touched nothing did not do the task);
    - the tests did not fail or time out (``skipped`` is not a failure: a machine
      without the runner says nothing about the change, and reading it as red would
      fail every case on a laptop without .NET);
    - no file is left with a conflict marker;
    - when the human change is known, the agent touched at least one of the same
      files. Zero overlap means it solved a different problem, however green.

    Overlap and diff size are reported but do not gate beyond that: two correct fixes
    for one bug routinely touch different helper files, and a size threshold would
    punish the agent for writing a test the human skipped.
    """
    touched = sorted({_norm_path(f) for f in touched_files if _norm_path(f)})
    expected = sorted({_norm_path(f) for f in case.expected_files if _norm_path(f)})
    conflicts = sorted({_norm_path(f) for f in conflict_files if _norm_path(f)})
    score = ReplayScore(
        name=case.name, passed=False, tests=test_status or TEST_SKIPPED,
        touched_files=touched, expected_files=expected,
        missed_files=sorted(set(expected) - set(touched)),
        extra_files=sorted(set(touched) - set(expected)),
        conflict_files=conflicts, diff_lines=max(0, int(diff_lines)),
        expected_diff_lines=case.expected_diff_lines, error=error,
        duration_seconds=duration_seconds, test_output=test_output[-2000:],
    )
    score.files_overlap = jaccard(touched, expected) if expected else 0.0

    if error:
        score.reasons.append(f"run error: {error}")
    if not touched:
        score.reasons.append("agent changed no files")
    if score.tests == TEST_FAILED:
        score.reasons.append("tests failed")
    elif score.tests == TEST_TIMEOUT:
        score.reasons.append("tests timed out")
    if conflicts:
        score.reasons.append(f"conflict markers left in {len(conflicts)} file(s)")
    if expected and touched and not set(expected) & set(touched):
        score.reasons.append("no file in common with the human change")

    if score.tests == TEST_SKIPPED:
        score.notes.append("tests skipped - no runner available")
    if case.expected_diff_lines and score.diff_lines:
        ratio = score.diff_lines / case.expected_diff_lines
        score.notes.append(f"diff {score.diff_lines} lines vs human "
                           f"{case.expected_diff_lines} ({ratio:.1f}x)")
    score.passed = not score.reasons
    return score


# ─────────────────────────────── git helpers ───────────────────────────────


def _git(repo: str | Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[-400:]}")
    return proc.stdout


def work_item_refs(message: str) -> list[int]:
    seen: list[int] = []
    for m in _WI_REF.finditer(message or ""):
        n = int(m.group(1))
        if n and n not in seen:
            seen.append(n)
    return seen


def _diff_stats(repo: str | Path, base: str, head: str = "", *,
                cached: bool = False) -> tuple[list[str], int]:
    """(files changed, lines added + removed) between ``base`` and ``head``/the index."""
    args = ["diff", "--no-renames", "--numstat"]
    if cached:
        args.append("--cached")
    args.append(base)
    if head:
        args.append(head)
    files: list[str] = []
    lines = 0
    for row in _git(repo, *args).splitlines():
        parts = row.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        files.append(_norm_path(path))
        # Binary files report "-": they count as a touched file but not as lines.
        lines += (int(added) if added.isdigit() else 0) + (int(removed) if removed.isdigit() else 0)
    return files, lines


# Signature: (work item ids) -> {id: (title, description text)}. Injected so harvest
# stays pure git and the tracker is an optional enrichment, never a dependency.
TaskFetcher = Callable[[list[int]], Awaitable[Mapping[int, tuple[str, str]]]]


def harvest(repo_dir: str | Path, *, repo_name: str = "", limit: int = 20,
            fetch_tasks: TaskFetcher | None = None) -> list[ReplayCase]:
    """Cases from the repo's own history: the newest ``limit`` changes that name a
    work item.

    Walks the first-parent history (the mainline as it was merged, not every commit on
    every feature branch) and keeps commits whose message references a work item —
    a merge commit or a squash. ``base_commit`` is the first parent, the mainline the
    human started from; ``expected_files`` is what the change touched relative to it.
    One case per work item: a later commit naming the same item is a follow-up, and
    replaying both would grade the same task twice.
    """
    repo_dir = Path(repo_dir)
    name = repo_name or repo_dir.name
    sep, end = "\x1f", "\x1e"
    log = _git(repo_dir, "log", "--first-parent", f"--format=%H{sep}%P{sep}%B{end}")
    cases: list[ReplayCase] = []
    seen: set[int] = set()
    for record in log.split(end):
        record = record.strip("\n")
        if not record.strip():
            continue
        sha, parents, message = (record.split(sep, 2) + ["", ""])[:3]
        parent_list = parents.split()
        if not parent_list:
            continue                                # the root commit has nothing to rewind to
        refs = work_item_refs(message)
        if not refs or refs[0] in seen:
            continue
        base = parent_list[0]
        files, lines = _diff_stats(repo_dir, base, sha)
        if not files:
            continue
        seen.add(refs[0])
        subject, _, body = message.strip().partition("\n")
        title = _MERGED_PR_PREFIX.sub("", subject).strip()
        cases.append(ReplayCase(
            name=f"{_slug(name)}-wi{refs[0]}-{sha[:8]}",
            repo=name, base_commit=base, title=title, description=body.strip(),
            expected_files=files, expected_diff_lines=lines,
            work_item_id=refs[0], merge_commit=sha,
        ))
        if len(cases) >= max(1, limit):
            break

    if fetch_tasks and cases:
        try:
            found = asyncio.run(fetch_tasks([c.work_item_id for c in cases]))
        except Exception as exc:  # noqa: BLE001 — the tracker is optional; git is the source
            _log.warning("replay harvest: work item lookup failed - using commit messages",
                         error=describe_exc(exc))
            found = {}
        for case in cases:
            title, desc = found.get(case.work_item_id, ("", ""))
            if title:
                # The commit message stays: it is what actually shipped, and the work
                # item text alone is often the wish rather than the result.
                commit_text = "\n".join(x for x in (case.title, case.description) if x)
                case.title = title
                case.description = "\n\n".join(
                    x for x in (desc.strip(), f"Commit message:\n{commit_text}") if x)
    return cases


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "repo"


def write_cases(cases: Iterable[ReplayCase], out_dir: str | Path, *,
                overwrite: bool = False) -> tuple[list[Path], list[Path]]:
    """Write each case as ``<name>.yaml``. Returns (written, skipped-because-present).

    Existing files are kept by default: a harvested case is a starting point that
    people trim (a vague commit message, a generated file in ``expected_files``), and a
    re-harvest must not silently throw that editing away.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    skipped: list[Path] = []
    for case in cases:
        path = out / f"{case.name}.yaml"
        if path.exists() and not overwrite:
            skipped.append(path)
            continue
        path.write_text(dump_case_yaml(case), encoding="utf-8")
        written.append(path)
    return written, skipped


# ─────────────────────────────── running ───────────────────────────────

# Signature: (brief, cwd, timeout_seconds) -> the agent's final text. Injected so the
# whole loop — worktree, collection, tests, cleanup — runs in tests without a model.
ReplayAgent = Callable[[str, str, int], Awaitable[str]]
# Signature: (command, cwd, timeout_seconds) -> (status, output tail).
TestRunner = Callable[[str, str, int], Awaitable[tuple[str, str]]]


def brief_for(case: ReplayCase) -> str:
    """The prompt for one replay. States the task and the fence around it."""
    task = case.title + (f"\n\n{case.description}" if case.description else "")
    return (
        "You are being evaluated on a task that was already completed once by a human. "
        "Implement it in this repository, which is checked out at the commit the work "
        "started from.\n\n"
        f"## Task\n{task.strip()}\n\n"
        "## Rules\n"
        "- Change the code in this working directory only.\n"
        "- Do NOT push, do NOT create branches or pull requests, do NOT contact any "
        "tracker. Committing locally is allowed but not required.\n"
        "- Leave no merge-conflict markers in any file.\n"
        "- Add or update tests where the change warrants it; run them if you can.\n"
        "- Finish with a short plain-text summary of what you changed.\n"
    )


def resolve_repo_dir(repo: str, workspace: str | Path) -> Path | None:
    """Where ``repo`` lives: an absolute path, a folder under the workspace, or the
    workspace itself when it IS that repo (single-repo installs)."""
    p = Path(repo)
    if p.is_absolute() and p.is_dir():
        return p
    if workspace:
        root = Path(workspace)
        if (root / repo).is_dir():
            return root / repo
        if root.name == repo and root.is_dir():
            return root
    if p.is_dir():
        return p.resolve()
    return None


def _runner_available(runner: str, cwd: str) -> bool:
    return bool(shutil.which(runner)) or Path(cwd, runner).exists()


async def run_test_command(command: str, cwd: str, timeout_seconds: int) -> tuple[str, str]:
    """Run ``command`` in ``cwd``. A runner that is not installed is ``skipped``."""
    first = command.split()[0] if command.split() else ""
    if first and not _runner_available(first, cwd):
        return TEST_SKIPPED, f"test runner '{first}' not found on PATH"
    # A shell on purpose: the command comes from the case file or the operator's own
    # configuration (pipes, `&&`), never from a model or a work item.
    # autopilot:ignore[py-shell-true] author-written test command, not untrusted input
    proc = await asyncio.create_subprocess_shell(
        command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        **spawn_kwargs(),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await terminate_tree(proc)
        return TEST_TIMEOUT, f"timed out after {timeout_seconds}s"
    text = (out or b"").decode("utf-8", "replace")
    if proc.returncode != 0 and re.search(
            r"is not recognized as an internal or external command|command not found", text):
        return TEST_SKIPPED, text[-2000:]
    return (TEST_PASSED if proc.returncode == 0 else TEST_FAILED), text[-2000:]


def _rm_tree(path: Path) -> None:
    def _force(func, p, _exc):  # read-only files under .git on Windows
        with contextlib.suppress(OSError):
            os.chmod(p, stat.S_IWRITE)
            func(p)
    shutil.rmtree(path, onexc=_force)


def _remove_worktree(repo_dir: Path, wt: Path, tmp: Path) -> None:
    with contextlib.suppress(Exception):
        _git(repo_dir, "worktree", "remove", "--force", str(wt), check=False)
    with contextlib.suppress(Exception):
        if tmp.exists():
            _rm_tree(tmp)
    # Prune AFTER the folder is gone, so a removal that failed above still leaves git's
    # worktree list clean instead of pointing at a directory that no longer exists.
    with contextlib.suppress(Exception):
        _git(repo_dir, "worktree", "prune", check=False)


async def run_replay_case(
    case: ReplayCase,
    *,
    workspace: str | Path,
    agent: ReplayAgent,
    test_command_for: Callable[[str, str], str] | None = None,
    test_runner: TestRunner = run_test_command,
    tmp_root: str | Path | None = None,
) -> ReplayScore:
    """Replay one case end to end and score it. Never raises; the worktree never
    outlives the call.

    ``test_command_for(repo, worktree)`` resolves the command when the case names
    none — the caller wires it to the configured per-repo command and the test gate's
    detection, so a replay runs the same tests the real pipeline would.
    """
    started = time.monotonic()

    def fail(msg: str) -> ReplayScore:
        return score_case(case, touched_files=(), test_status=TEST_SKIPPED, error=msg,
                          duration_seconds=time.monotonic() - started)

    if problems := case.problems():
        return fail("invalid case: " + ", ".join(problems))
    repo_dir = resolve_repo_dir(case.repo, workspace)
    if repo_dir is None:
        return fail(f"repo {case.repo!r} not found under workspace {str(workspace)!r}")

    tmp = Path(tempfile.mkdtemp(prefix="replay-", dir=str(tmp_root) if tmp_root else None))
    wt = tmp / "wt"
    try:
        try:
            _git(repo_dir, "worktree", "add", "--detach", str(wt), case.base_commit)
        except Exception as exc:  # noqa: BLE001 — unknown commit, dirty lock, …
            return fail(f"could not create worktree at {case.base_commit[:12]}: "
                        f"{describe_exc(exc)}")
        timeout = max(1, case.timeout_minutes) * 60
        error = ""
        try:
            await asyncio.wait_for(agent(brief_for(case), str(wt), timeout), timeout=timeout)
        except TimeoutError:
            error = f"agent timed out after {case.timeout_minutes} min"
        except Exception as exc:  # noqa: BLE001 — a crashed run is a failed case
            error = describe_exc(exc)

        # Stage everything so new files count, then diff the INDEX against the base:
        # that covers both edits left in the tree and anything the agent committed.
        _git(wt, "add", "-A", check=False)
        touched, lines = _diff_stats(wt, case.base_commit, cached=True)
        contents: dict[str, str] = {}
        for rel in touched:
            f = wt / rel
            if f.is_file():
                with contextlib.suppress(OSError):
                    contents[rel] = f.read_text(encoding="utf-8", errors="replace")
        conflicts = find_conflict_markers(contents)

        command = case.test_command or (
            test_command_for(case.repo, str(wt)) if test_command_for else "") or ""
        if not command or not touched or error:
            status = TEST_SKIPPED
            output = "" if command else "no test command configured or detected"
        else:
            try:
                status, output = await test_runner(command, str(wt), timeout)
            except Exception as exc:  # noqa: BLE001 — our own launch problem, not the change's
                status, output = TEST_SKIPPED, f"could not run tests: {describe_exc(exc)}"
        return score_case(
            case, touched_files=touched, test_status=status, conflict_files=conflicts,
            diff_lines=lines, error=error, duration_seconds=time.monotonic() - started,
            test_output=output,
        )
    finally:
        _remove_worktree(repo_dir, wt, tmp)


async def run_replay_suite(cases: list[ReplayCase], **kwargs: Any) -> list[ReplayScore]:
    """Every case, one at a time — each is a full agent session on a full repo."""
    out: list[ReplayScore] = []
    for case in cases:
        _log.info("replay case starting", case=case.name)
        score = await run_replay_case(case, **kwargs)
        _log.info("replay case done", case=case.name, passed=score.passed,
                  reasons=score.reasons[:3])
        out.append(score)
    return out


def claude_agent(config: Any) -> ReplayAgent:
    """A :data:`ReplayAgent` driving the real agent with this machine's configuration.

    Same entry the configuration evals use, with the project settings loaded so the
    replay grades the agent the team actually runs, not a stock one.
    """

    async def _run(brief: str, cwd: str, timeout_seconds: int) -> str:
        from ai_autopilot.execution.claude_client import run_claude

        run = await run_claude(
            brief, cwd,
            permission_mode=getattr(config, "claude_permission_mode", "acceptEdits"),
            model=getattr(config, "claude_model", "") or None,
            timeout_seconds=timeout_seconds,
            setting_sources=["project", "user"],
            # The brief forbids pushing; this makes it a fence rather than a request.
            disallowed_tools=["Bash(git push:*)", "Bash(git push)"],
        )
        return run.text or ""

    return _run


def pass_rate(scores: list[ReplayScore]) -> float:
    """Share passed. Empty is 0.0 — a replay that ran nothing proved nothing."""
    return sum(1 for s in scores if s.passed) / len(scores) if scores else 0.0


def format_replay_report(scores: list[ReplayScore], min_pass_rate: float) -> str:
    passed = sum(1 for s in scores if s.passed)
    lines = [
        "",
        f"  Replay evals - {passed}/{len(scores)} passed ({pass_rate(scores) * 100:.0f}%), "
        f"threshold {min_pass_rate * 100:.0f}%",
        "",
        f"  {'':6} {'case':<44} {'tests':<8} {'overlap':>7} {'files':>9} {'diff':>6} {'time':>6}",
    ]
    for s in scores:
        mark = "PASS" if s.passed else "FAIL"
        files = f"{len(set(s.touched_files) & set(s.expected_files))}/{len(s.expected_files)}"
        lines.append(
            f"  [{mark}] {s.name[:44]:<44} {s.tests:<8} {s.files_overlap * 100:>6.0f}% "
            f"{files:>9} {s.diff_lines:>6} {s.duration_seconds:>5.0f}s")
        for reason in s.reasons[:5]:
            lines.append(f"         x {reason}")
        for note in s.notes[:3]:
            lines.append(f"         - {note}")
    if not scores:
        lines.append("  No cases ran - an empty replay proves nothing, so this fails.")
    lines.append("")
    return "\n".join(lines)


def report_json(scores: list[ReplayScore], min_pass_rate: float) -> dict[str, Any]:
    return {
        "total": len(scores),
        "passed": sum(1 for s in scores if s.passed),
        "pass_rate": pass_rate(scores),
        "threshold": min_pass_rate,
        "cases": [s.to_dict() for s in scores],
    }
