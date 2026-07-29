"""Exec / ROI analytics — pure aggregation over execution history.

Kept as a pure function (records in → report out) so it is trivially unit-testable,
exactly like ``pr_scorer.score_run``. The dashboard route gathers the records and
this module only computes the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ai_autopilot.data.entities import ExecutionRecord, ExecutionStatus


@dataclass
class CategoryStat:
    category: str
    runs: int
    success: int

    @property
    def success_rate(self) -> int:
        return round(100 * self.success / self.runs) if self.runs else 0


@dataclass
class AnalyticsReport:
    window_days: int
    total_runs: int = 0
    success: int = 0
    failed: int = 0
    with_pr: int = 0
    distinct_items: int = 0
    total_tokens: int = 0
    avg_duration_seconds: float = 0.0
    by_category: list[CategoryStat] = field(default_factory=list)
    per_day: list[tuple[str, int]] = field(default_factory=list)  # (YYYY-MM-DD, runs)

    @property
    def success_rate(self) -> int:
        done = self.success + self.failed
        return round(100 * self.success / done) if done else 0

    @property
    def pr_rate(self) -> int:
        return round(100 * self.with_pr / self.total_runs) if self.total_runs else 0

    @property
    def runs_per_item(self) -> float:
        return round(self.total_runs / self.distinct_items, 2) if self.distinct_items else 0.0

    @property
    def tokens_per_pr(self) -> int | None:
        return round(self.total_tokens / self.with_pr) if self.with_pr else None

    @property
    def avg_tokens_per_run(self) -> int:
        return round(self.total_tokens / self.total_runs) if self.total_runs else 0

    @property
    def peak_day(self) -> int:
        return max((n for _, n in self.per_day), default=0)


def compute_analytics(
    records: list[ExecutionRecord], *, days: int, now: datetime
) -> AnalyticsReport:
    """Aggregate execution records into an ROI report over the last ``days`` days."""
    rep = AnalyticsReport(window_days=days)
    cats: dict[str, list[int]] = {}  # category -> [runs, success]
    items: set[int] = set()
    day_counts: dict[str, int] = {}

    for r in records:
        rep.total_runs += 1
        items.add(r.work_item_id)
        rep.total_tokens += r.cost_tokens or 0
        if r.status == ExecutionStatus.SUCCESS:
            rep.success += 1
        elif r.status == ExecutionStatus.FAILED:
            rep.failed += 1
        if r.pr_url:
            rep.with_pr += 1
        cat = r.category or "(uncategorised)"
        slot = cats.setdefault(cat, [0, 0])
        slot[0] += 1
        if r.status == ExecutionStatus.SUCCESS:
            slot[1] += 1
        if r.started_at:
            key = r.started_at.date().isoformat()
            day_counts[key] = day_counts.get(key, 0) + 1

    durations = [r.duration_seconds for r in records if r.duration_seconds]
    rep.avg_duration_seconds = round(sum(durations) / len(durations), 1) if durations else 0.0
    rep.distinct_items = len(items)
    rep.by_category = sorted(
        (CategoryStat(c, v[0], v[1]) for c, v in cats.items()),
        key=lambda cs: cs.runs, reverse=True,
    )
    # Dense per-day series (zero-filled) for the last `days` days, oldest → newest.
    start = now.date() - timedelta(days=days - 1)
    rep.per_day = [
        ((start + timedelta(days=i)).isoformat(),
         day_counts.get((start + timedelta(days=i)).isoformat(), 0))
        for i in range(days)
    ]
    return rep
