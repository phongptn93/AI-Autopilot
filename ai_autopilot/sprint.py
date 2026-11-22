"""The current sprint, as the team's board sees it — committed vs done, and on track?

Everything else on the dashboard is a rolling window ("last 14 days"). A team plans in
sprints, and the stand-up / review questions are sprint questions: how much of what we
committed is done, are we ahead of or behind the calendar, what was added after the
sprint started, who still has hours left, and how good were our estimates.

Read from the default team's current iteration and the items in it. Fields beyond the
basics are optional in ADO processes (Story Points on Agile, Effort on Scrum, hours on
tasks only when the team logs them) — each metric that needs one is ``None`` when the
process does not have it, never 0.

"Added after start" is read from the item's creation date: ADO does not expose when an
item was moved INTO an iteration without the revision history, so an old item pulled in
mid-sprint is not counted. The page says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

BACKLOG_TYPES = {"requirement", "user story", "product backlog item", "bug", "feature"}
TASK_TYPES = {"task"}
DONE = {"Resolved", "Completed"}
_POINTS = ("Microsoft.VSTS.Scheduling.StoryPoints", "Microsoft.VSTS.Scheduling.Effort",
           "Microsoft.VSTS.Scheduling.Size")
_REMAINING = "Microsoft.VSTS.Scheduling.RemainingWork"
_COMPLETED = "Microsoft.VSTS.Scheduling.CompletedWork"
_ORIGINAL = "Microsoft.VSTS.Scheduling.OriginalEstimate"
# Behind the calendar by more than this many percentage points = at risk.
BEHIND_MARGIN_PCT = 10


def _dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)) or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass
class SprintItem:
    id: int
    title: str
    type: str
    state: str
    category: str
    assigned_to: str
    points: float | None
    created: datetime | None
    by_ai: bool = False

    @property
    def done(self) -> bool:
        return self.category in DONE

    @property
    def started(self) -> bool:
        return self.category == "InProgress" or self.done


@dataclass
class PersonLoad:
    name: str
    items: int = 0
    done: int = 0
    remaining_hours: float = 0.0


@dataclass
class SprintReport:
    status: str = "ok"            # "ok" | "none" (no current sprint) | "unknown"
    reason: str = ""
    name: str = ""
    path: str = ""
    start: datetime | None = None
    finish: datetime | None = None
    committed: int = 0
    done: int = 0
    in_progress: int = 0
    not_started: int = 0
    points_committed: float | None = None
    points_done: float | None = None
    added_after_start: list[SprintItem] = field(default_factory=list)
    remaining_hours: float | None = None
    estimate_accuracy: float | None = None   # completed / original, over done tasks
    ai_done: int = 0
    people: list[PersonLoad] = field(default_factory=list)
    at_risk: list[SprintItem] = field(default_factory=list)
    elapsed_pct: float | None = None

    @property
    def done_pct(self) -> float | None:
        if self.points_committed:
            return round(100 * (self.points_done or 0) / self.points_committed, 1)
        return round(100 * self.done / self.committed, 1) if self.committed else None

    @property
    def days_left(self) -> int | None:
        if self.finish is None:
            return None
        return max(0, (self.finish - datetime.now(UTC)).days + 1)

    @property
    def verdict(self) -> str:
        """"ahead" | "on_track" | "behind" | "" (nothing to judge)."""
        if self.done_pct is None or self.elapsed_pct is None:
            return ""
        gap = self.done_pct - self.elapsed_pct
        if gap >= 0:
            return "ahead" if gap >= BEHIND_MARGIN_PCT else "on_track"
        return "behind" if -gap > BEHIND_MARGIN_PCT else "on_track"


def compute(iteration: dict | None, raw: list[dict] | None, *, categories: dict[str, str],
            ai_item_ids: set[int] | None = None, now: datetime | None = None) -> SprintReport:
    now = now or datetime.now(UTC)
    if iteration is None or raw is None:
        return SprintReport(status="unknown",
                            reason="Không đọc được sprint hiện tại từ ADO.")
    if not iteration:
        return SprintReport(status="none",
                            reason="Team mặc định không có sprint nào đang diễn ra hôm nay.")
    rep = SprintReport(name=str(iteration.get("name") or ""),
                       path=str(iteration.get("path") or ""),
                       start=_dt(iteration.get("start")), finish=_dt(iteration.get("finish")))
    if rep.start and rep.finish and rep.finish > rep.start:
        span = (rep.finish - rep.start).total_seconds()
        rep.elapsed_pct = round(100 * min(1.0, max(0.0, (now - rep.start).total_seconds() / span)),
                                1)
    ai_item_ids = ai_item_ids or set()
    people: dict[str, PersonLoad] = {}
    pts_c = pts_d = 0.0
    has_points = False
    remaining = 0.0
    has_hours = False
    orig_sum = done_sum = 0.0
    backlog: list[SprintItem] = []

    for f in raw:
        kind = str(f.get("System.WorkItemType") or "").strip().lower()
        state = str(f.get("System.State") or "")
        cat = categories.get(state, "")
        if cat == "Removed":
            continue
        who = f.get("System.AssignedTo")
        name = (who.get("displayName") if isinstance(who, dict) else str(who or "")) or ""
        person = people.setdefault(name or "(chưa gán)", PersonLoad(name or "(chưa gán)"))
        if kind in TASK_TYPES:
            rem = _num(f.get(_REMAINING))
            if rem is not None:
                has_hours = True
                if cat not in DONE:
                    remaining += rem
                    person.remaining_hours += rem
            orig, comp = _num(f.get(_ORIGINAL)), _num(f.get(_COMPLETED))
            if cat in DONE and orig and comp is not None:
                orig_sum += orig
                done_sum += comp
            continue
        if kind not in BACKLOG_TYPES:
            continue
        points = next((p for p in (_num(f.get(k)) for k in _POINTS) if p is not None), None)
        item = SprintItem(int(f.get("System.Id") or 0), str(f.get("System.Title") or ""),
                          str(f.get("System.WorkItemType") or ""), state, cat, name, points,
                          _dt(f.get("System.CreatedDate")),
                          by_ai=int(f.get("System.Id") or 0) in ai_item_ids)
        backlog.append(item)
        person.items += 1
        if points is not None:
            has_points = True
            pts_c += points
            if item.done:
                pts_d += points
        if item.done:
            rep.done += 1
            person.done += 1
            rep.ai_done += int(item.by_ai)
        elif item.started:
            rep.in_progress += 1
        else:
            rep.not_started += 1
        if rep.start and item.created and item.created > rep.start:
            rep.added_after_start.append(item)

    rep.committed = len(backlog)
    if has_points:
        rep.points_committed, rep.points_done = round(pts_c, 1), round(pts_d, 1)
    if has_hours:
        rep.remaining_hours = round(remaining, 1)
    if orig_sum:
        rep.estimate_accuracy = round(done_sum / orig_sum, 2)
    # Not started with under a third of the sprint left = will most likely slip.
    if rep.elapsed_pct is not None and rep.elapsed_pct >= 66:
        rep.at_risk = [i for i in backlog if not i.started]
    rep.people = sorted((p for p in people.values() if p.items or p.remaining_hours),
                        key=lambda p: (-p.remaining_hours, -(p.items - p.done), p.name))
    for p in rep.people:
        p.remaining_hours = round(p.remaining_hours, 1)
    return rep
