"""Merged without a person touching it — the strictest reading of "the PR was good".

``north_star`` already says whether an item finished with no retry, no revise round and
no 👎. What it cannot see is a reviewer who simply fixed the branch by hand and merged:
no event is written for that, so the item scores as autonomous when it was not.

ADO records every push to a PR source branch as an *iteration*. The first iteration is
the PR as opened; each later one is a push after it. Pushes the autopilot made itself
(``/ai revise`` rounds, logged as ``pr_revision`` quality events) are subtracted, and
any push left over is taken to be a person's.

Honest limits, named in the KPI tooltip rather than hidden:

* A merge-conflict resolution the autopilot pushed is also an iteration and writes no
  quality event, so it counts here as a person's edit — the number errs towards
  *under*-claiming autonomy, never over.
* Only PRs already known to have merged are judged; an open PR can still be edited.
* A PR whose iterations could not be read is "unknown", never counted as clean.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from ai_autopilot.data.entities import QualityKind

#: Merged PRs never change, so an answer once obtained is kept for the process lifetime.
_CACHE: dict[tuple[str, int], int] = {}


@dataclass
class PrAssessment:
    pr_id: int
    work_item_id: int
    pushes_after_open: int
    autopilot_pushes: int

    @property
    def human_pushes(self) -> int:
        return max(0, self.pushes_after_open - self.autopilot_pushes)

    @property
    def clean(self) -> bool:
        return self.human_pushes == 0


@dataclass
class Cleanliness:
    assessed: list[PrAssessment] = field(default_factory=list)
    unknown: int = 0     # merged PRs whose iterations could not be read

    @property
    def clean(self) -> int:
        return sum(1 for a in self.assessed if a.clean)

    @property
    def rate(self) -> int | None:
        if not self.assessed:
            return None
        return round(100 * self.clean / len(self.assessed))


def assess(
    prs: Iterable[tuple[int, int]],
    iteration_counts: dict[int, int | None],
    revisions_by_item: dict[int, int],
) -> Cleanliness:
    """``prs``: ``(pr_id, work_item_id)`` of merged PRs. ``iteration_counts``: pr_id →
    number of iterations, or ``None`` when it could not be read."""
    out = Cleanliness()
    for pr_id, wid in prs:
        n = iteration_counts.get(pr_id)
        if n is None:
            out.unknown += 1
            continue
        out.assessed.append(
            PrAssessment(pr_id, wid, max(0, n - 1), revisions_by_item.get(wid, 0))
        )
    return out


async def gather(client, runs: Iterable, quality: Iterable, merged_ids: set[int]) -> Cleanliness:
    """Read iteration counts for the merged PRs among ``runs`` and assess them.

    Best-effort by construction: a read that fails marks that PR *unknown*; it never
    raises, because this feeds a dashboard card and must not take the page down.
    """
    from ai_autopilot.services.pr_feedback import parse_pr_url

    revisions: dict[int, int] = {}
    for ev in quality:
        if getattr(ev, "kind", "") == QualityKind.PR_REVISION:
            wid = int(getattr(ev, "work_item_id", 0) or 0)
            revisions[wid] = revisions.get(wid, 0) + 1

    seen: dict[int, tuple[str, str, int]] = {}      # pr_id → (repo, project, work item)
    for r in runs:
        parsed = parse_pr_url(getattr(r, "pr_url", None))
        if parsed and parsed[1] in merged_ids and parsed[1] not in seen:
            seen[parsed[1]] = (
                parsed[0], str(getattr(r, "project", "") or ""),
                int(getattr(r, "work_item_id", 0) or 0),
            )

    counts: dict[int, int | None] = {}
    for pr_id, (repo, project, _wid) in seen.items():
        key = (repo, pr_id)
        if key in _CACHE:
            counts[pr_id] = _CACHE[key]
            continue
        counts[pr_id] = None
        with contextlib.suppress(Exception):
            its = await client.get_pull_request_iterations(repo, pr_id, project)
            if its is not None:
                _CACHE[key] = counts[pr_id] = len(its)
    return assess(((p, v[2]) for p, v in seen.items()), counts, revisions)
