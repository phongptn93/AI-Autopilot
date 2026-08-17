"""Delivery analytics — the project view, as opposed to the autopilot's own view.

``analytics.py`` answers "how is the ROBOT doing?" (runs, tokens, success rate). This
module answers "how is the PROJECT doing?" — throughput, lead time, work in progress,
who is loaded, what is stuck, and how much of the delivery the autopilot actually did.

Three deliberate differences from ``analytics.py``, each of them the reason this is a
separate module rather than more fields on ``AnalyticsReport``:

**The unit is a work item, not a run.** Three runs on one item is a retry — a bad
sign — but counted as runs it looks like three times the output. Everything here
counts distinct work items.

**Age is a first-class value.** A card that has sat in review for three days and one
that arrived twenty minutes ago are indistinguishable on a board. Sorting the action
list by age is the entire point of it.

**Nothing is invented when the data is not there.** Flow and lead time need recorded
state transitions (``StateHistoryRepository``), which only exist from the day
recording was switched on. Rather than back-filling from ``System.ChangedDate`` — which
any edit bumps, so it reads *newer* than the truth and would quietly hide the stuck
items this page exists to surface — the report carries ``history_since`` and lets the
page say how far back it can actually see.

Pure functions: records in, report out. The dashboard route does the gathering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# ADO state categories, which is what the process template guarantees — state NAMES are
# free text an admin can change, categories are not.
CAT_PROPOSED = "Proposed"
CAT_IN_PROGRESS = "InProgress"
CAT_RESOLVED = "Resolved"
CAT_COMPLETED = "Completed"
CAT_REMOVED = "Removed"
# What counts as delivered. Resolved is included on purpose: many boards treat it as
# "code is done, awaiting release", and excluding it would report a team that ships
# steadily as delivering nothing.
DONE_CATEGORIES = frozenset({CAT_RESOLVED, CAT_COMPLETED})
# Flow chart bands, left to right, with the label the page shows.
FLOW_BANDS: tuple[tuple[str, str], ...] = (
    (CAT_PROPOSED, "Chưa làm"),
    (CAT_IN_PROGRESS, "Đang làm"),
    (CAT_RESOLVED, "Chờ phát hành"),
    (CAT_COMPLETED, "Xong"),
)

# Action kinds, most urgent first. The order here IS the display order.
KIND_BLOCKED_PR = "blocked_pr"
KIND_MERGE_READY = "merge_ready"
KIND_REVIEW_WAITING = "review_waiting"
KIND_NEEDS_HUMAN = "needs_human"
KIND_STALE = "stale"
KIND_FAILED = "failed"

_KIND_ORDER = {
    KIND_BLOCKED_PR: 0,
    KIND_MERGE_READY: 1,
    KIND_REVIEW_WAITING: 2,
    KIND_NEEDS_HUMAN: 3,
    KIND_STALE: 4,
    KIND_FAILED: 5,
}


@dataclass(frozen=True)
class Thresholds:
    """When a wait becomes a problem worth a PM's attention.

    Defaults suit a team on 1–2 week sprints. They are deliberately tight: the cost of
    a false alarm is a glance, the cost of a missed one is a week of silent delay."""

    merge_hours: int = 24      # approved, nothing blocking, still not merged
    review_hours: int = 24     # PR open with nobody having voted
    stale_days: int = 3        # in progress, no state change


@dataclass(frozen=True)
class PrView:
    """A pull request, normalised by the caller from the ADO payload + reviewer state.

    Kept as a plain value so this module never touches the API: the counts (approved /
    blocked / pending) are already reduced, and ``approved_at`` is the timestamp of the
    LAST approving vote — the correct clock for "how long has this been ready to
    merge?", which the PR's own creation date would badly overstate."""

    id: int
    repo: str = ""
    title: str = ""
    author: str = ""
    url: str = ""
    work_item_id: int = 0
    project: str = ""
    created_at: datetime | None = None
    approved_at: datetime | None = None
    approved: int = 0
    blocked: int = 0
    pending: int = 0
    pending_reviewers: tuple[str, ...] = ()

    @property
    def is_ready_to_merge(self) -> bool:
        """At least one approval, nothing rejecting, nobody still to vote."""
        return self.approved >= 1 and self.blocked == 0 and self.pending == 0

    @property
    def ready_since(self) -> datetime | None:
        """When it became merge-able — the last approval, or failing that, its creation."""
        return self.approved_at or self.created_at


@dataclass
class ActionItem:
    """One row of "needs your attention", already carrying its reason and its age."""

    kind: str
    title: str
    age_hours: float
    owner: str = ""
    project: str = ""
    detail: str = ""
    work_item_id: int = 0
    pr_id: int = 0
    repo: str = ""
    url: str = ""

    @property
    def age_label(self) -> str:
        """Age as a human reads it — "5 giờ", "3 ngày"."""
        if self.age_hours < 1:
            return f"{int(self.age_hours * 60)} phút"
        if self.age_hours < 48:
            return f"{self.age_hours:.0f} giờ"
        return f"{self.age_hours / 24:.0f} ngày"


@dataclass
class PersonStat:
    """One person's load and output over the window."""

    name: str
    wip: int = 0             # currently in an InProgress state
    waiting_merge: int = 0   # their PR is ready/open, work is done but not landed
    delivered: int = 0       # reached a done state within the window
    ai_assisted: int = 0     # of those delivered, how many the autopilot worked on
    todo: int = 0            # assigned but not started
    oldest_wait_hours: float = 0.0

    @property
    def ai_share(self) -> int:
        return round(100 * self.ai_assisted / self.delivered) if self.delivered else 0

    @property
    def load(self) -> int:
        """Everything on this person's plate right now (the over-load signal)."""
        return self.wip + self.waiting_merge


@dataclass
class ProjectStat:
    project: str
    delivered: int = 0
    wip: int = 0
    waiting_merge: int = 0
    todo: int = 0
    ai_assisted: int = 0
    actions: int = 0
    worst_age_hours: float = 0.0

    @property
    def ai_share(self) -> int:
        return round(100 * self.ai_assisted / self.delivered) if self.delivered else 0


@dataclass
class Kpi:
    """A headline number with its previous-window comparison.

    A number on its own is not a decision input — "14 items" is only good or bad next
    to last week's. ``previous`` is None when the comparison cannot be made honestly
    (not enough history), and the page then omits the arrow rather than implying zero."""

    label: str
    value: float
    previous: float | None = None
    unit: str = ""
    hint: str = ""
    # True when a RISE is the good direction (throughput), False when a fall is
    # (lead time, WIP). Without it the page cannot colour the arrow correctly.
    higher_is_better: bool = True

    @property
    def delta(self) -> float | None:
        return None if self.previous is None else self.value - self.previous

    @property
    def delta_pct(self) -> int | None:
        if not self.previous:
            return None
        return round(100 * (self.value - self.previous) / self.previous)

    @property
    def direction(self) -> str:
        """``up`` / ``down`` / ``flat`` — the arrow, independent of whether it is good."""
        d = self.delta
        if d is None or abs(d) < 1e-9:
            return "flat"
        return "up" if d > 0 else "down"

    @property
    def tone(self) -> str:
        """``good`` / ``bad`` / ``flat`` — the colour."""
        direction = self.direction
        if direction == "flat":
            return "flat"
        rising = direction == "up"
        return "good" if rising == self.higher_is_better else "bad"


@dataclass
class DeliveryReport:
    window_days: int
    generated_at: datetime
    thresholds: Thresholds
    kpis: list[Kpi] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)
    people: list[PersonStat] = field(default_factory=list)
    projects: list[ProjectStat] = field(default_factory=list)
    # (YYYY-MM-DD, {category: count}) per day, oldest → newest.
    flow: list[tuple[str, dict[str, int]]] = field(default_factory=list)
    # When state recording began. None = never recorded; anything after the window
    # start means the flow chart's left edge is incomplete and the page must say so.
    history_since: datetime | None = None
    total_items: int = 0
    delivered_items: list[int] = field(default_factory=list)

    @property
    def flow_is_partial(self) -> bool:
        """True when history does not reach back to the start of the window."""
        if self.history_since is None:
            return True
        start = self.generated_at - timedelta(days=self.window_days)
        return _as_utc(self.history_since) > start

    @property
    def flow_peak(self) -> int:
        """Tallest day in the flow chart — the scale every bar is drawn against."""
        return max((sum(counts.values()) for _, counts in self.flow), default=0)

    @property
    def action_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            out[a.kind] = out.get(a.kind, 0) + 1
        return out


def _as_utc(value: datetime | None) -> datetime | None:
    """Naive datetimes come out of SQLite; treat them as UTC (which is what we wrote)."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _hours(later: datetime, earlier: datetime | None) -> float:
    earlier = _as_utc(earlier)
    if earlier is None:
        return 0.0
    return max(0.0, (later - earlier).total_seconds() / 3600)


def percentile(values: list[float], pct: int) -> float:
    """Nearest-rank percentile — no interpolation.

    Interpolating invents a lead time no work item actually had. For a handful of
    items a week (the normal case) that fabricated midpoint is the difference between
    two real observations, so the honest answer is one of the observations."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(pct / 100 * len(ordered) + 0.5)))
    return ordered[rank - 1]


def _state_timelines(changes: list) -> dict[int, list]:
    """``{work_item_id: [changes oldest→newest]}``."""
    out: dict[int, list] = {}
    for change in changes:
        out.setdefault(change.work_item_id, []).append(change)
    for timeline in out.values():
        timeline.sort(key=lambda c: _as_utc(c.entered_at))
    return out


def delivered_between(
    changes: list, start: datetime, end: datetime
) -> dict[int, datetime]:
    """``{work_item_id: when it first entered a done state}`` within ``[start, end)``.

    "First" matters: an item can be resolved, reopened and resolved again, and counting
    both would report more delivery than happened."""
    out: dict[int, datetime] = {}
    for wid, timeline in _state_timelines(changes).items():
        for change in timeline:
            at = _as_utc(change.entered_at)
            if change.category in DONE_CATEGORIES and start <= at < end:
                out[wid] = at
                break
            if change.category in DONE_CATEGORIES:
                break  # already delivered before the window — not this window's output
    return out


def build_flow(
    changes: list, *, days: int, now: datetime
) -> list[tuple[str, dict[str, int]]]:
    """Cumulative flow: how many items sat in each category at the end of each day.

    Built by replaying the transitions rather than sampling ADO daily, which is what
    makes any past window re-derivable. An item contributes nothing to a day before its
    first recorded transition — the chart grows into correctness rather than starting
    with a fabricated backlog."""
    if days <= 0:
        return []
    ordered = sorted(changes, key=lambda c: _as_utc(c.entered_at))
    out: list[tuple[str, dict[str, int]]] = []
    current: dict[int, str] = {}
    cursor = 0
    start_day = (now - timedelta(days=days - 1)).date()
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        # End of this day (or "now" for today — a partial day must not look like a dip).
        boundary = min(
            datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=UTC), now
        )
        while cursor < len(ordered) and _as_utc(ordered[cursor].entered_at) <= boundary:
            current[ordered[cursor].work_item_id] = ordered[cursor].category
            cursor += 1
        counts = {cat: 0 for cat, _ in FLOW_BANDS}
        for category in current.values():
            if category in counts:
                counts[category] += 1
        out.append((day.isoformat(), counts))
    return out


def _pr_actions(
    prs: list[PrView], thresholds: Thresholds, now: datetime
) -> list[ActionItem]:
    """Merge-ready, review-waiting and rejected PRs — the buckets whose age is exact
    from day one, because a PR carries its own real timestamps."""
    actions: list[ActionItem] = []
    for pr in prs:
        common = dict(
            work_item_id=pr.work_item_id, pr_id=pr.id, repo=pr.repo,
            owner=pr.author, project=pr.project, url=pr.url,
            title=pr.title or f"PR !{pr.id}",
        )
        if pr.blocked:
            actions.append(ActionItem(
                kind=KIND_BLOCKED_PR,
                age_hours=_hours(now, pr.created_at),
                detail=f"{pr.blocked} người từ chối — tác giả cần sửa",
                **common,
            ))
            continue
        if pr.is_ready_to_merge:
            age = _hours(now, pr.ready_since)
            if age >= thresholds.merge_hours:
                actions.append(ActionItem(
                    kind=KIND_MERGE_READY, age_hours=age,
                    detail="Đã duyệt, không ai chặn — chỉ còn bấm merge",
                    **common,
                ))
            continue
        if pr.pending or pr.approved == 0:
            age = _hours(now, pr.created_at)
            if age >= thresholds.review_hours:
                who = ", ".join(pr.pending_reviewers) if pr.pending_reviewers else "chưa gán ai"
                actions.append(ActionItem(
                    kind=KIND_REVIEW_WAITING, age_hours=age,
                    detail=f"Chờ review: {who}", **common,
                ))
    return actions


def compute_delivery(
    *,
    items: list,
    categories: dict[str, str],
    prs: list[PrView],
    changes: list,
    ai_item_ids: set[int],
    held: list | None = None,
    failed: list | None = None,
    history_since: datetime | None = None,
    thresholds: Thresholds | None = None,
    days: int = 14,
    now: datetime | None = None,
) -> DeliveryReport:
    """Build the whole Delivery report.

    ``items`` are the current work items (``WorkItemInfo``), ``categories`` maps state
    name → ADO state category, ``prs`` the normalised pull requests, ``changes`` the
    recorded state transitions, ``ai_item_ids`` the work items the autopilot has
    executed at least once, ``held`` the escalated items (``WorkItemState`` rows) and
    ``failed`` the failed execution records in the window.
    """
    now = now or datetime.now(UTC)
    thresholds = thresholds or Thresholds()
    start = now - timedelta(days=days)
    prev_start = start - timedelta(days=days)
    report = DeliveryReport(
        window_days=days, generated_at=now, thresholds=thresholds,
        history_since=history_since, total_items=len(items),
    )

    delivered = delivered_between(changes, start, now)
    delivered_prev = delivered_between(changes, prev_start, start)
    report.delivered_items = sorted(delivered)

    # ── Current position of every item, from the items themselves (not history, which
    #    may not reach back far enough to know where an old item sits).
    by_id = {it.id: it for it in items}
    timelines = _state_timelines(changes)
    baseline = _as_utc(history_since)

    def in_state_since(item) -> datetime | None:
        """When the item entered its current state, as well as can be known.

        The recorded transition is exact — except for the very first row of all, which
        is a baseline snapshot: it says "the item was already in this state when we
        started looking", not "it moved just now". For those, ``changed_date`` is used
        when it is OLDER, because that field can only ever be bumped FORWARD by an
        edit — so an old value is trustworthy even though a recent one is not."""
        timeline = timelines.get(item.id)
        recorded = _as_utc(timeline[-1].entered_at) if timeline else None
        changed = _as_utc(getattr(item, "changed_date", None))
        if recorded is None:
            return changed
        if baseline is not None and recorded <= baseline and changed and changed < recorded:
            return changed
        return recorded

    # PR by work item — "code done, waiting to land" is a state the board cannot show.
    pr_by_item = {pr.work_item_id: pr for pr in prs if pr.work_item_id}

    people: dict[str, PersonStat] = {}
    projects: dict[str, ProjectStat] = {}
    in_flight = 0
    stale_actions: list[ActionItem] = []

    for item in items:
        category = categories.get(item.state or "", "")
        owner = item.assigned_to or "(chưa gán)"
        person = people.setdefault(owner, PersonStat(name=owner))
        project_name = item.project or "(không rõ)"
        project = projects.setdefault(project_name, ProjectStat(project_name))
        # Work in flight = being worked on OR finished-but-not-landed. Both consume the
        # team's attention and both lengthen lead time, so the headline counts both;
        # the person/project tables still split them, because the ACTION differs
        # (help someone code vs. go press merge).
        if category == CAT_IN_PROGRESS or item.id in pr_by_item:
            in_flight += 1
        if item.id in pr_by_item:
            person.waiting_merge += 1
            project.waiting_merge += 1
        elif category == CAT_IN_PROGRESS:
            person.wip += 1
            project.wip += 1
            since = in_state_since(item)
            age = _hours(now, since)
            person.oldest_wait_hours = max(person.oldest_wait_hours, age)
            if since is not None and age >= thresholds.stale_days * 24:
                stale_actions.append(ActionItem(
                    kind=KIND_STALE, work_item_id=item.id, title=item.title,
                    owner=owner, project=item.project, age_hours=age,
                    detail=f"Không đổi trạng thái từ «{item.state}»",
                ))
        elif category == CAT_PROPOSED:
            person.todo += 1
            project.todo += 1

    for wid in delivered:
        item = by_id.get(wid)
        owner = (item.assigned_to if item else "") or _owner_from_history(timelines, wid)
        person = people.setdefault(owner, PersonStat(name=owner))
        person.delivered += 1
        project_name = (
            (item.project if item else "")
            or _project_from_history(timelines, wid)
            or "(không rõ)"
        )
        project = projects.setdefault(project_name, ProjectStat(project_name))
        project.delivered += 1
        if wid in ai_item_ids:
            person.ai_assisted += 1
            project.ai_assisted += 1

    # ── Actions, most urgent kind first and oldest first within a kind.
    actions = _pr_actions(prs, thresholds, now)
    actions += stale_actions
    for row in held or []:
        actions.append(ActionItem(
            kind=KIND_NEEDS_HUMAN, work_item_id=row.work_item_id,
            title=getattr(row, "title", "") or f"#{row.work_item_id}",
            age_hours=_hours(now, getattr(row, "updated_at", None)),
            detail=(getattr(row, "detail", "") or "Autopilot dừng, chờ người quyết")[:180],
        ))
    for record in failed or []:
        actions.append(ActionItem(
            kind=KIND_FAILED, work_item_id=record.work_item_id,
            title=getattr(record, "title", "") or f"#{record.work_item_id}",
            age_hours=_hours(
                now,
                getattr(record, "completed_at", None) or getattr(record, "started_at", None),
            ),
            detail=(getattr(record, "error", "") or "Run thất bại")[:180],
        ))
    actions.sort(key=lambda a: (_KIND_ORDER.get(a.kind, 99), -a.age_hours))
    report.actions = actions

    for action in actions:
        name = action.project or "(không rõ)"
        if name in projects:
            projects[name].actions += 1
            projects[name].worst_age_hours = max(projects[name].worst_age_hours, action.age_hours)

    # ── Lead time: created → first done. Needs the item still to be readable; an item
    #    delivered and then deleted simply drops out rather than counting as zero.
    lead_times = [
        _hours(at, by_id[wid].created_date)
        for wid, at in delivered.items()
        if wid in by_id and getattr(by_id[wid], "created_date", None)
    ]
    lead_prev = [
        _hours(at, by_id[wid].created_date)
        for wid, at in delivered_prev.items()
        if wid in by_id and getattr(by_id[wid], "created_date", None)
    ]

    ai_now = sum(1 for wid in delivered if wid in ai_item_ids)
    ai_prev = sum(1 for wid in delivered_prev if wid in ai_item_ids)
    # The previous window is only comparable when history actually covers it. Without
    # this guard the first fortnight of recording reports "▼ 100%" against a period
    # nobody was watching — a collapse in delivery that never happened, and the most
    # damaging kind of wrong number a PM page can show.
    comparable = baseline is not None and baseline <= prev_start
    report.kpis = [
        Kpi("Giao được", len(delivered), _kpi_prev(len(delivered_prev), comparable),
            unit="item", hint=f"Work item vào trạng thái hoàn thành trong {days} ngày"),
        Kpi("Lead time p50", round(percentile(lead_times, 50) / 24, 1),
            _kpi_prev(round(percentile(lead_prev, 50) / 24, 1), comparable and lead_prev),
            unit="ngày", hint="Từ lúc tạo tới lúc xong — nửa số item nhanh hơn mức này",
            higher_is_better=False),
        Kpi("Lead time p85", round(percentile(lead_times, 85) / 24, 1),
            _kpi_prev(round(percentile(lead_prev, 85) / 24, 1), comparable and lead_prev),
            unit="ngày", hint="Mức cam kết được: 85% item xong trong khoảng này",
            higher_is_better=False),
        Kpi("Đang mở (WIP)", in_flight, None, unit="item",
            hint="Đang làm + đã xong code chờ merge. Càng nhiều việc mở song song, "
                 "lead time càng dài",
            higher_is_better=False),
        Kpi("Có AI-Autopilot", round(100 * ai_now / len(delivered)) if delivered else 0,
            _kpi_prev(round(100 * ai_prev / len(delivered_prev)) if delivered_prev else 0,
                      comparable and delivered_prev),
            unit="%", hint="Phần việc giao được mà autopilot có tham gia"),
    ]

    report.people = sorted(
        (p for p in people.values() if p.load or p.delivered or p.todo),
        key=lambda p: (-p.load, -p.delivered, p.name),
    )
    report.projects = sorted(projects.values(), key=lambda p: (-p.delivered, -p.wip, p.project))
    report.flow = build_flow(changes, days=days, now=now)
    return report


def _kpi_prev(value, evidence) -> float | None:
    """The previous-window value, or ``None`` when there is nothing honest to compare
    against — ``evidence`` is falsy when history does not cover that window."""
    return value if evidence else None


def _owner_from_history(timelines: dict[int, list], wid: int) -> str:
    timeline = timelines.get(wid) or []
    for change in reversed(timeline):
        if change.assigned_to:
            return change.assigned_to
    return "(chưa gán)"


def _project_from_history(timelines: dict[int, list], wid: int) -> str:
    timeline = timelines.get(wid) or []
    for change in reversed(timeline):
        if change.project:
            return change.project
    return ""
