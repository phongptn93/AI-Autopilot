"""Merge conflicts on pull requests — what they are, and what "resolved" has to mean.

A PR in conflict cannot be merged, whatever its reviews say. ADO knows (``mergeStatus``)
and the Reviews page showed it, but nothing acted on it: nobody was told, nothing
remembered since when, and an approved PR could sit unmergeable for days while the
delivery report listed it as "just press merge".

Resolution follows one principle: **the target branch is merged INTO the PR branch** —
never a rebase, never a force-push. A rebase rewrites commits that reviewers already
commented on and forces a push over a branch people may have pulled; a merge commit
keeps every reviewed commit and every comment anchored, and shows exactly what the
resolution changed. The model is only asked to settle the hunks git could not; what it
produced is then held to OBJECTIVE checks before anything is pushed (no markers left,
nothing outside the conflicted files touched, tests and the security gate pass). When
it cannot meet them, the merge is aborted and a person is asked — a wrong resolution
merged silently is worse than a conflict left visible.

Leaf module: no package imports, so the service, the executor side and the dashboard
can all use it without a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Status tokens stored on a tracked conflict. Order is the lifecycle.
OPEN = "open"            # ADO reports a conflict; nobody has resolved it yet
RESOLVING = "resolving"  # a resolution run is in flight
ESCALATED = "escalated"  # the resolver tried and handed it to a person
RESOLVED = "resolved"    # the PR merges cleanly again
CLOSED = "closed"        # the PR was completed or abandoned while in conflict
STATUSES = (OPEN, RESOLVING, ESCALATED, RESOLVED, CLOSED)
ACTIVE_STATUSES = (OPEN, RESOLVING, ESCALATED)

# Who/what cleared it — shown on the dashboard, because "it went away" and "the bot
# merged main into it" are different facts for a reviewer.
BY_AGENT = "agent"       # the resolver settled conflicting hunks
BY_CLEAN_MERGE = "clean"  # git merged target in with no conflicts (ADO's view was stale)
BY_OTHER = "external"    # someone/something else fixed it (push, target revert…)

# A conflict marker at the start of a line. Git writes exactly seven characters; a
# resolution that leaves one behind compiles in some languages (``=======`` in a
# markdown file) — so this is checked, never assumed.
_MARKER = re.compile(r"^(<{7}|={7}|>{7}|\|{7})(\s|$)", re.MULTILINE)

# Files a model should not "resolve" by editing text: their content is generated or
# binary, and the right fix is to regenerate them, which is a person's call.
_UNRESOLVABLE = re.compile(
    r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|packages\.lock\.json|poetry\.lock|"
    r"\.(png|jpe?g|gif|ico|pdf|zip|dll|exe|so|dylib|bin|snk|pfx|xlsx|docx|pptx))$",
    re.IGNORECASE,
)


def is_conflicted(pr: dict) -> bool:
    """True when ADO's test merge of this PR failed on conflicts.

    ``mergeStatus`` arrives as a string (``"conflicts"``) from the REST API and as an
    int (2) from some clients. ``queued`` / ``notSet`` mean "not computed yet" and are
    NOT treated as conflicts — ADO recomputes on the next target push.
    """
    ms = pr.get("mergeStatus")
    return ms == "conflicts" or ms == 2


def merge_status(pr: dict) -> str:
    ms = pr.get("mergeStatus")
    return {0: "notSet", 1: "queued", 2: "conflicts", 3: "succeeded",
            4: "rejectedByPolicy", 5: "failure"}.get(ms, str(ms or "notSet"))


def target_commit(pr: dict) -> str:
    """The target-branch commit ADO last test-merged against. A resolution attempt is
    keyed on it: retrying against the SAME target commit reproduces the same conflict,
    so it is attempted once; a new push to the target is a genuinely new situation."""
    return str(((pr.get("lastMergeTargetCommit") or {}).get("commitId")) or "")


def has_markers(text: str) -> bool:
    return bool(_MARKER.search(text or ""))


def files_with_markers(root: str | Path, paths: list[str]) -> list[str]:
    """Which of ``paths`` (relative to ``root``) still contain a conflict marker."""
    left: list[str] = []
    for rel in paths:
        p = Path(root) / rel
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # deleted as part of the resolution — nothing left to mark
        if has_markers(text):
            left.append(rel)
    return left


def unresolvable(paths: list[str]) -> list[str]:
    """Conflicted files that must not be text-edited (lock files, binaries)."""
    return [p for p in paths if _UNRESOLVABLE.search(p.replace("\\", "/"))]


@dataclass
class ResolveContext:
    """What the resolver is told about the PR — the INTENT of both sides, which is what
    a conflict actually asks you to reconcile."""

    pr_id: int = 0
    title: str = ""
    description: str = ""
    work_item_id: int = 0
    work_item_title: str = ""
    source_branch: str = ""
    target_branch: str = ""


@dataclass
class ConflictResolution:
    """Outcome of one resolution attempt."""

    success: bool = False
    how: str = ""                      # BY_AGENT | BY_CLEAN_MERGE when success
    files: list[str] = field(default_factory=list)       # conflicted files
    error: str = ""                    # why it failed, in words for the PR comment
    merge_commit: str = ""
    checks: dict[str, str] = field(default_factory=dict)  # check → "ok" / reason
    tokens: int = 0
    duration_seconds: float = 0.0


def resolution_prompt(
    ctx: ResolveContext, files: list[str], ours_log: str, theirs_log: str,
) -> str:
    """The instruction for settling the conflicted hunks — scoped, and honest about
    the way out. ``ours_log`` = commits on the PR branch, ``theirs_log`` = commits the
    target gained since the branch forked."""
    listed = "\n".join(f"- {f}" for f in files)
    wi = (f"Work item #{ctx.work_item_id}: {ctx.work_item_title}\n"
          if ctx.work_item_id else "")
    desc = (ctx.description or "").strip()
    desc = (desc[:1500] + "…") if len(desc) > 1500 else desc
    return f"""\
You are resolving a MERGE CONFLICT. `git merge origin/{ctx.target_branch}` has already
been run on branch `{ctx.source_branch}` (PR !{ctx.pr_id}) and stopped on conflicts.
The merge is in progress in the current working tree.

PR: {ctx.title}
{wi}{('PR description:' + chr(10) + desc + chr(10)) if desc else ''}
Commits on this PR branch (OURS — the change under review):
{ours_log or '(none listed)'}

Commits the target `{ctx.target_branch}` gained meanwhile (THEIRS — already merged work):
{theirs_log or '(none listed)'}

Conflicted files — edit ONLY these:
{listed}

How to resolve:
1. For each file, read BOTH sides of every `<<<<<<<` / `=======` / `>>>>>>>` block and
   what each side's commits were trying to do (`git log -p --follow -- <file>` on each
   side if needed). The result must keep BOTH intents: the PR's change applied on top of
   what the target already has. Never drop the target's change just to keep the PR's.
2. Remove every conflict marker. Keep the file compiling / valid.
3. Do NOT edit any other file, do NOT run `git add`, `git commit`, `git merge --abort`
   or push — the caller verifies and commits.
4. If a hunk genuinely cannot be reconciled without a product decision (both sides
   change the same behaviour in incompatible ways), do not guess: leave that file's
   markers in place and explain why.

Finish with one line exactly like one of:
RESOLUTION: done — <one sentence on what you reconciled>
RESOLUTION: needs-human — <which file/hunk and the decision it needs>
"""


def parse_verdict(text: str) -> tuple[str, str]:
    """``("done"|"needs-human"|"", reason)`` from the agent's last RESOLUTION line."""
    last = ("", "")
    for line in (text or "").splitlines():
        m = re.match(r"\s*RESOLUTION:\s*(done|needs-human)\s*[—\-:]?\s*(.*)", line, re.I)
        if m:
            last = (m.group(1).lower(), m.group(2).strip())
    return last


def files_html(files: list[str], limit: int = 20) -> str:
    """The conflicted files as an HTML list for a PR comment."""
    if not files:
        return "<i>(ADO did not list the files)</i>"
    items = "".join(f"<li><code>{_esc(f)}</code></li>" for f in files[:limit])
    more = f"<li>… và {len(files) - limit} file khác</li>" if len(files) > limit else ""
    return f"<ul>{items}{more}</ul>"


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
