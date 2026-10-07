"""Earned autonomy: how much a scope of work may do on its own, from its own record.

One ``autonomy_level`` for a whole machine is wrong in both directions at once. A
project/category whose PRs have merged untouched for weeks still waits on drafts, while
one whose runs keep bouncing gets exactly the same freedom. The ladder gives each SCOPE
its own level and lets it move by evidence:

    0  report      — plan as a comment, no code
    1  assisted    — draft PR
    2  assisted    — ready-for-review (non-draft) PR, still waits for a person
    3  unattended  — normal PR, the item is resolved without a review hold

**Scope = (project, work-item category).** Of what an execution row stores — project,
category, skill_used, trigger_tag, profile — these two are the ones that describe the
WORK rather than the machinery: ``skill_used`` is "agent" or "interactive:<session>"
for every modern run, ``trigger_tag`` names which machine picked it up, and a run's
quality has nothing to do with either. Project separates codebases and teams; category
(bug / feature / requirement…) separates kinds of change whose difficulty differs.

**Stateless by design.** The level is not stored anywhere: it is replayed from the
scope's history every time, oldest run first, applying the same promotion/demotion
rules. A stored level drifts from the record it claims to summarise (a restart, a
second machine, a deleted row); a replay cannot, and two machines with the same
history always agree. The cost is one bounded query per dispatch.

The machine's own ``autonomy_level`` becomes the CEILING and ``trust_max_level`` caps
the ladder below it — the ladder can only ever take autonomy away from what the
operator configured, or give back up to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Quality-event kind under which the level a run was given is recorded. A string, like
# every QualityKind, so the append-only log never needs a migration for it.
TRUST_LEVEL_KIND = "trust_level"

GOOD, BAD, PENDING = "good", "bad", "pending"

# What each rung means to the executor and to the poller's outcome handling:
# (autonomy passed to the executor, open the PR as a draft, hold the item for review).
_RUNGS: dict[int, tuple[str, bool, bool]] = {
    0: ("report", True, True),
    1: ("assisted", True, True),
    2: ("assisted", False, True),
    3: ("unattended", False, False),
}

LEVEL_LABELS = {
    0: "báo cáo (chỉ kế hoạch)",
    1: "PR nháp",
    2: "PR sẵn sàng review",
    3: "tự động hoàn toàn",
}


@dataclass(frozen=True)
class Rung:
    level: int
    autonomy: str
    draft_pr: bool
    review_first: bool


def rung(level: int) -> Rung:
    lv = max(0, min(3, int(level)))
    autonomy, draft, review = _RUNGS[lv]
    return Rung(lv, autonomy, draft, review)


def ceiling_for(autonomy_level: str) -> int:
    """The highest rung the machine's configured autonomy allows.

    "assisted" reaches 2: a ready-for-review PR still waits for a person, which is
    what assisted promises. Only "unattended" reaches 3. Anything unknown is treated
    as a draft-only machine rather than guessed upwards.
    """
    return {"report": 0, "assisted": 2, "unattended": 3}.get(
        (autonomy_level or "").strip().lower(), 1
    )


@dataclass(frozen=True)
class TrustRun:
    """One finished run of the scope, reduced to what the ladder judges."""

    work_item_id: int
    succeeded: bool
    had_pr: bool
    merged: bool
    # A person sent it back after the run started: a /revise round on its PR, a reopen,
    # or a rejecting review vote.
    setback: bool


def judge(run: TrustRun) -> str:
    """good / bad / pending for one run.

    A PR that is still open is PENDING — neither proof nor disproof — and is left out
    of the rate rather than counted against a scope whose reviewers are merely slow.
    A successful run with no PR (a plan, a QC pass) is good: nothing was expected to
    merge, and without this a scope demoted to report could never climb back.
    """
    if not run.succeeded or run.setback:
        return BAD
    if run.had_pr and not run.merged:
        return PENDING
    return GOOD


def compute_level(
    runs: list[TrustRun], *, ceiling: int, max_level: int = 2, min_runs: int = 10,
    promote_rate: float = 0.8, start: int = 1,
) -> tuple[int, str]:
    """Replay ``runs`` (OLDEST first) and return ``(level, reason)``.

    - Start at ``start`` (draft PRs): earned autonomy is earned, so a new scope does not
      begin at the top — but not at 0 either, where nothing could ever merge to prove it.
    - Two decided-bad runs in a row → one rung down. Two, not one: a single failure is
      as often the environment as the work.
    - At least ``min_runs`` decided runs since the last change, with a good share of at
      least ``promote_rate`` → one rung up, and the window starts again.
    - Never above ``min(ceiling, max_level)``.
    """
    top = max(0, min(int(ceiling), int(max_level), 3))
    level = max(0, min(int(start), top))
    need = max(1, int(min_runs))
    reason = f"khởi điểm mức {level} — chưa đủ {need} lần chạy để xét lên mức"
    window: list[str] = []
    streak = 0
    for run in runs:
        verdict = judge(run)
        if verdict == PENDING:
            continue
        window.append(verdict)
        streak = streak + 1 if verdict == BAD else 0
        if streak >= 2 and level > 0:
            level -= 1
            reason = f"hạ xuống mức {level}: 2 lần chạy liên tiếp bị lỗi / trả lại / phải sửa"
            window, streak = [], 0
            continue
        if level < top and len(window) >= need:
            rate = window.count(GOOD) / len(window)
            if rate >= promote_rate:
                level += 1
                reason = (f"lên mức {level}: {window.count(GOOD)}/{len(window)} lần chạy "
                          f"merge không phải sửa (≥ {promote_rate:.0%})")
                window, streak = [], 0
    if level >= top and top < 3:
        reason += f" — đang ở trần (mức {top})"
    return level, reason


def _naive_utc(at: Any) -> datetime | None:
    """Rows come back naive from SQLite and aware from a fresh object; compare as naive UTC."""
    if not isinstance(at, datetime):
        return None
    return at.astimezone(UTC).replace(tzinfo=None) if at.tzinfo else at


def build_runs(
    rows: list[Any], merged_ids: set[int], setbacks: list[tuple[int, Any]],
) -> list[TrustRun]:
    """Execution rows (oldest first) + what happened to their items afterwards → runs.

    A setback is attributed to every run of that item that STARTED before it: the work
    a reviewer sent back is the work that existed when they looked.
    """
    by_item: dict[int, list[datetime]] = {}
    for item_id, at in setbacks:
        when = _naive_utc(at)
        if when is not None:
            by_item.setdefault(int(item_id), []).append(when)
    out: list[TrustRun] = []
    for row in rows:
        status = getattr(getattr(row, "status", None), "value", getattr(row, "status", ""))
        started = _naive_utc(getattr(row, "started_at", None))
        item_id = int(getattr(row, "work_item_id", 0) or 0)
        later = by_item.get(item_id, [])
        out.append(TrustRun(
            work_item_id=item_id,
            succeeded=str(status).lower() == "success",
            had_pr=bool(getattr(row, "pr_url", None)),
            merged=item_id in merged_ids,
            setback=bool(started and any(t >= started for t in later)),
        ))
    return out


def scope_of(item: Any) -> tuple[str, str]:
    return ((getattr(item, "project", "") or "").strip(), str(getattr(item, "category", "") or ""))


async def level_for(c: Any, item: Any) -> tuple[int, str]:
    """The rung this item's scope has earned, and why — read from the repositories.

    Raises on a missing/broken repository: the caller owns the fallback (the machine's
    configured autonomy), because only it can log the fallback in context.
    """
    cfg = c.config
    project, category = scope_of(item)
    rows = await c.execution_repo.scope_history(project, category, limit=200)
    ids = {int(r.work_item_id) for r in rows}
    merged = await c.sync_repo.merged_work_item_ids(ids) if ids else set()
    events_repo = getattr(c, "quality_events", None) or c.quality_repo
    setbacks = await events_repo.setbacks_for_items(ids) if ids else []
    return compute_level(
        build_runs(rows, merged, setbacks),
        ceiling=ceiling_for(cfg.autonomy_level),
        max_level=cfg.trust_max_level,
        min_runs=cfg.trust_min_runs,
        promote_rate=cfg.trust_promote_rate,
    )
