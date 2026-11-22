"""Defect metrics for QC — how many bugs, how old, how many escaped, how many came back.

``process_health`` already counts escaped defects per module for the Head of Product.
QC asks the next questions, the ones a test lead reports on every sprint:

* **Escape rate** — of the defects raised in the window, how many were found in UAT or
  production rather than by internal QC (``process_health.ESCAPED_ENVIRONMENTS``).
* **Reopen rate** — of the defects closed, how many came back. Read from the recorded
  state transitions: a bug that reached a Resolved/Completed state and later went back
  to Proposed/InProgress. Needs state history; without it the rate is ``None``, never 0.
* **Open defect age** — p50 and the oldest, so a bug parked for two months is seen.
* **Time to close** — p50 from created to closed, for defects closed in the window.
* **By module** — raised, escaped, still open, per Area Path leaf.

Pure: items + transitions + category map in, report out. A defect is a ``Bug``, or a
``Requirement`` tagged ``BUG`` (how this process raises a defect found after a
requirement was accepted — see ``process_health.escaped_defects``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ai_autopilot.process_health import ESCAPED_ENVIRONMENTS, HealthItem

DONE_CATEGORIES = {"Resolved", "Completed"}
ACTIVE_CATEGORIES = {"Proposed", "InProgress"}
REMOVED_CATEGORY = "Removed"


def is_defect(item: HealthItem) -> bool:
    kind = item.work_item_type.strip().lower()
    return kind == "bug" or (kind == "requirement" and item.has_tag("BUG"))


def is_escaped(item: HealthItem) -> bool:
    env = (item.found_in_environment or "").strip().lower()
    if any(env.startswith(e) for e in ESCAPED_ENVIRONMENTS):
        return True
    return item.work_item_type.strip().lower() == "requirement" and item.has_tag("BUG")


@dataclass
class ModuleDefects:
    module: str
    raised: int = 0
    escaped: int = 0
    open: int = 0


@dataclass
class OpenDefect:
    id: int
    title: str
    state: str
    module: str
    severity: str
    assigned_to: str
    age_days: float
    escaped: bool


@dataclass
class DefectReport:
    days: int
    raised: int = 0
    escaped: int = 0
    closed: int = 0
    reopened: int = 0
    open_count: int = 0
    reopen_measured: bool = False       # False = no state history reached the window
    open_age_p50_days: float | None = None
    close_p50_days: float | None = None
    severity_known: bool = False
    by_severity: dict[str, int] = field(default_factory=dict)   # open defects
    modules: list[ModuleDefects] = field(default_factory=list)
    oldest_open: list[OpenDefect] = field(default_factory=list)

    @property
    def escape_rate(self) -> float | None:
        return self.escaped / self.raised if self.raised else None

    @property
    def reopen_rate(self) -> float | None:
        if not self.reopen_measured or not self.closed:
            return None
        return self.reopened / self.closed


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def reopened_ids(changes: list, categories: dict[str, str], since: datetime) -> set[int]:
    """Items that went done → active again, with the reopen inside the window."""
    by_item: dict[int, list] = defaultdict(list)
    for ch in changes:
        by_item[ch.work_item_id].append(ch)
    out: set[int] = set()
    for wid, rows in by_item.items():
        rows.sort(key=lambda r: _utc(r.entered_at) or datetime.min.replace(tzinfo=UTC))
        was_done = False
        for r in rows:
            cat = getattr(r, "category", "") or categories.get(r.state, "")
            if cat in DONE_CATEGORIES:
                was_done = True
            elif cat in ACTIVE_CATEGORIES and was_done:
                if (_utc(r.entered_at) or since) >= since:
                    out.add(wid)
                was_done = False
    return out


def compute(items: list[HealthItem], *, categories: dict[str, str], days: int,
            changes: list | None = None, history_since: datetime | None = None,
            now: datetime | None = None, oldest_limit: int = 15) -> DefectReport:
    now = now or datetime.now(UTC)
    start = now - timedelta(days=days)
    report = DefectReport(days=days)
    defects = [i for i in items if is_defect(i)]
    modules: dict[str, ModuleDefects] = {}
    open_ages: list[float] = []
    close_days: list[float] = []
    closed_ids: set[int] = set()
    open_list: list[OpenDefect] = []

    for d in defects:
        cat = categories.get(d.state, "")
        if cat == REMOVED_CATEGORY:
            continue
        module = d.module or "(chưa có module)"
        mod = modules.setdefault(module, ModuleDefects(module))
        created = _utc(d.created)
        escaped = is_escaped(d)
        if created and created >= start:
            report.raised += 1
            mod.raised += 1
            if escaped:
                report.escaped += 1
                mod.escaped += 1
        if cat in DONE_CATEGORIES:
            closed = _utc(d.closed) or _utc(d.changed)
            if closed and closed >= start:
                report.closed += 1
                closed_ids.add(d.id)
                if created:
                    close_days.append((closed - created).total_seconds() / 86400)
        else:
            report.open_count += 1
            mod.open += 1
            age = (now - created).total_seconds() / 86400 if created else 0.0
            if created:
                open_ages.append(age)
            if d.severity:
                report.severity_known = True
                report.by_severity[d.severity] = report.by_severity.get(d.severity, 0) + 1
            open_list.append(OpenDefect(d.id, d.title, d.state, module, d.severity,
                                        d.assigned_to, round(age, 1), escaped))

    report.open_age_p50_days = _round(_median(open_ages))
    report.close_p50_days = _round(_median(close_days))
    # Reopens can only be counted where transitions were being recorded.
    since = _utc(history_since)
    if changes is not None and since is not None and since <= start:
        report.reopen_measured = True
        reopened = reopened_ids(changes, categories, start)
        defect_ids = {d.id for d in defects}
        report.reopened = len(reopened & defect_ids)
        # A bug reopened this window and still open was closed at some point too.
        report.closed = max(report.closed, len(closed_ids | (reopened & defect_ids)))
    open_list.sort(key=lambda o: (-o.age_days, o.id))
    report.oldest_open = open_list[:oldest_limit]
    report.modules = sorted(
        (m for m in modules.values() if m.raised or m.open),
        key=lambda m: (-m.escaped, -m.raised, -m.open, m.module))
    return report


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
