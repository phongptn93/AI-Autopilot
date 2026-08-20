"""Process-health metrics: is the WAY the team works still holding up?

The delivery report answers "what is the autopilot shipping". This answers a different
question, the one a Head of Product asks at sprint/month/quarter boundaries: how much
capacity went to unplanned work, how many defects escaped the QC gate, which items have
been stuck, and whether the tag taxonomy has started to rot. Those reviews are written
into the team's process doc as recurring duties, and a duty nobody has time to compute
is a duty that quietly stops happening.

Deliberately a **leaf module** (no package imports beyond the work-item model) and
**pure**: every function takes the already-fetched items and returns numbers, so the
thresholds can be tested without an ADO connection — the same reasoning as ``outcomes``
and ``flows``.

What it can measure is bounded by what the ADO process actually defines. A process doc
may call for a ``Rework Count`` or a ``Blocked Owner`` field that was never created;
metrics here therefore lean on what always exists — tags, states, Area Path, dates —
and treat richer fields (``Blocked``, ``Found In Environment``, ``Severity``) as
optional, reporting them as "unknown" rather than pretending a zero.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# Tag taxonomy from the team's process doc. Group 1 ("classification") says what a
# ticket IS — exactly one per ticket. Group 2 ("routing") says who must look at it and
# is meant to be REMOVED once they have; a routing tag left on a closed item is the
# rot this module surfaces.
DEFAULT_CLASSIFICATION_TAGS = (
    "BUG", "Change Request", "New Request", "Pre-Sales", "Support", "Spike", "Ops",
)
# Work that was not in the sprint plan. Support/Spike/Ops are the timeboxed ad-hoc
# lanes; Pre-Sales eats committed capacity without belonging to any contract.
DEFAULT_AD_HOC_TAGS = ("Support", "Spike", "Ops", "Pre-Sales")
DEFAULT_ROUTING_TAGS = ("PM - Need Review", "Product - Review")
# Types that are TICKETS — the unit an external request becomes, and the only level a
# classification tag is expected on. Task/Bug are technical children of a ticket: asking
# for a tag there would report a "violation" the process never asked for, and 500 false
# findings is how a report gets ignored.
DEFAULT_TICKET_TYPES = ("Requirement", "User Story", "Product Backlog Item")
# Environments that mean the defect got past internal QC — the escaped-defect signal.
ESCAPED_ENVIRONMENTS = ("uat", "production", "prod")


@dataclass(frozen=True)
class HealthItem:
    """One work item, reduced to the fields these metrics read.

    A plain snapshot rather than the full ``WorkItemInfo`` so the metrics stay testable
    from literals and independent of how the items were fetched.
    """

    id: int
    work_item_type: str = ""
    state: str = ""
    title: str = ""
    area_path: str = ""
    tags: tuple[str, ...] = ()
    changed: datetime | None = None
    created: datetime | None = None
    blocked: bool | None = None          # None = the project has no Blocked field
    found_in_environment: str = ""       # "" = not set / field absent
    assigned_to: str = ""

    def has_tag(self, tag: str) -> bool:
        return tag.strip().lower() in {t.strip().lower() for t in self.tags}

    @property
    def module(self) -> str:
        """Last segment of the Area Path — the module the doc reports by."""
        return (self.area_path or "").replace("/", "\\").split("\\")[-1].strip()


def _lower(values) -> set[str]:
    return {str(v).strip().lower() for v in values if str(v).strip()}


@dataclass
class AdHocRatio:
    ad_hoc: int = 0
    planned: int = 0
    by_tag: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.ad_hoc + self.planned

    @property
    def percent(self) -> float:
        return round(100 * self.ad_hoc / self.total, 1) if self.total else 0.0

    def over(self, threshold_pct: float) -> bool:
        """Only meaningful once there is enough work to divide by — a single ad-hoc
        ticket in a quiet week is 100% and says nothing."""
        return self.total >= 5 and self.percent > threshold_pct


def is_ticket(item: HealthItem, ticket_types=DEFAULT_TICKET_TYPES) -> bool:
    return item.work_item_type.strip().lower() in _lower(ticket_types)


def ad_hoc_ratio(
    items: list[HealthItem],
    ad_hoc_tags=DEFAULT_AD_HOC_TAGS,
    classification_tags=DEFAULT_CLASSIFICATION_TAGS,
) -> AdHocRatio:
    """Share of classified tickets that were unplanned work.

    Only tickets carrying a classification tag count, in either direction: an untagged
    item says nothing about planning, and counting it as "planned" would quietly dilute
    the ratio the more the team forgets to tag.
    """
    out = AdHocRatio()
    known, ad_hoc = _lower(classification_tags), _lower(ad_hoc_tags)
    for item in items:
        tags = _lower(item.tags)
        if not (tags & known):
            continue
        hit = tags & ad_hoc
        if hit:
            out.ad_hoc += 1
            for tag in ad_hoc_tags:
                if tag.strip().lower() in hit:
                    out.by_tag[tag] = out.by_tag.get(tag, 0) + 1
        else:
            out.planned += 1
    return out


def escaped_defects(items: list[HealthItem]) -> dict[str, int]:
    """Defects that reached UAT or production, counted per module.

    Two shapes count, because the process routes a defect differently depending on when
    it was found: a ``Bug`` whose *Found In Environment* is UAT/Production, and a
    ``Requirement`` tagged ``BUG`` — which is how a defect found *after* the original
    requirement was accepted is raised, precisely because it escaped.
    """
    out: Counter[str] = Counter()
    for item in items:
        env = (item.found_in_environment or "").strip().lower()
        escaped = any(env.startswith(e) for e in ESCAPED_ENVIRONMENTS)
        if not escaped and item.work_item_type.strip().lower() == "requirement":
            escaped = item.has_tag("BUG")
        if escaped:
            out[item.module or "(no module)"] += 1
    return dict(out.most_common())


def stale_blocked(items: list[HealthItem], days: int = 3, now: datetime | None = None):
    """Items flagged Blocked and untouched for ``days`` — the PM's follow-up list.

    Measured from the last change, not from when the flag went on: ADO does not record
    the latter, and an item somebody is actively chasing gets touched.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    return [
        item for item in items
        if item.blocked and item.changed is not None and item.changed < cutoff
    ]


def routing_tags_left(
    items: list[HealthItem], done_states, routing_tags=DEFAULT_ROUTING_TAGS
) -> list[tuple[HealthItem, list[str]]]:
    """Finished items still carrying a "somebody must look at this" tag.

    Routing tags are a request for attention; left on a closed item they make every
    "what is waiting on me" query lie, which is how people stop trusting the queries.
    """
    done = _lower(done_states)
    out: list[tuple[HealthItem, list[str]]] = []
    for item in items:
        if (item.state or "").strip().lower() not in done:
            continue
        left = [t for t in routing_tags if item.has_tag(t)]
        if left:
            out.append((item, left))
    return out


def tag_case_drift(items: list[HealthItem]) -> dict[str, list[str]]:
    """Tags that differ only by case/spacing, grouped by their normalised form.

    ADO tags are case-sensitive strings, so ``BUG`` / ``Bug`` / ``bug`` are three tags
    and every query written against one silently misses the others.
    """
    seen: dict[str, set[str]] = {}
    for item in items:
        for tag in item.tags:
            raw = tag.strip()
            if raw:
                seen.setdefault(" ".join(raw.lower().split()), set()).add(raw)
    return {k: sorted(v) for k, v in sorted(seen.items()) if len(v) > 1}


def untagged(
    items: list[HealthItem],
    classification_tags=DEFAULT_CLASSIFICATION_TAGS,
    ticket_types=DEFAULT_TICKET_TYPES,
):
    """TICKETS with no classification tag — the ones every other metric misses.

    Scoped to ticket types on purpose: the classification tag decides how a request is
    handled and whether it is billable, which is a property of the ticket, not of the
    tasks someone split it into."""
    known = _lower(classification_tags)
    return [
        i for i in items
        if is_ticket(i, ticket_types) and not (_lower(i.tags) & known)
    ]


def missing_area_path(items: list[HealthItem], root_only: str = "") -> list[HealthItem]:
    """Items left at the project root Area Path — invisible to every module report.

    ``root_only`` is the bare project name; an item whose Area Path is exactly that has
    not been assigned to a module.
    """
    root = (root_only or "").strip().lower()
    return [
        i for i in items
        if not (i.area_path or "").strip()
        or (root and (i.area_path or "").strip().lower() == root)
    ]


@dataclass
class HealthReport:
    """Everything one process-health run measured, ready to render."""

    project: str = ""
    window_days: int = 14
    items: int = 0
    ad_hoc: AdHocRatio = field(default_factory=AdHocRatio)
    ad_hoc_threshold: float = 30.0
    escaped: dict[str, int] = field(default_factory=dict)
    blocked: list[HealthItem] = field(default_factory=list)
    routing_left: list[tuple[HealthItem, list[str]]] = field(default_factory=list)
    tag_drift: dict[str, list[str]] = field(default_factory=dict)
    untagged: list[HealthItem] = field(default_factory=list)
    no_module: list[HealthItem] = field(default_factory=list)
    # True when this project simply does not use Area Path to split modules. Then
    # "everything is at the root" is its normal shape, not 700 violations.
    area_path_unused: bool = False

    @property
    def has_findings(self) -> bool:
        """Whether anything needs a human. A report with nothing in it is still worth
        computing (it is the evidence the process is holding) but not worth pinging."""
        return bool(
            self.ad_hoc.over(self.ad_hoc_threshold) or self.escaped or self.blocked
            or self.routing_left or self.tag_drift or self.untagged or self.no_module
        )


# Above this share of items at the project root, the project is not using Area Path at
# all. Reporting every item then buries the findings that ARE actionable.
_AREA_PATH_UNUSED_SHARE = 0.9


def build_report(
    items: list[HealthItem],
    *,
    project: str = "",
    window_days: int = 14,
    done_states=(),
    ad_hoc_threshold: float = 30.0,
    blocked_days: int = 3,
    classification_tags=DEFAULT_CLASSIFICATION_TAGS,
    ad_hoc_tags=DEFAULT_AD_HOC_TAGS,
    routing_tags=DEFAULT_ROUTING_TAGS,
    ticket_types=DEFAULT_TICKET_TYPES,
    now: datetime | None = None,
) -> HealthReport:
    no_module = missing_area_path(items, project)
    unused = bool(items) and len(no_module) >= _AREA_PATH_UNUSED_SHARE * len(items)
    return HealthReport(
        project=project,
        window_days=window_days,
        items=len(items),
        ad_hoc=ad_hoc_ratio(items, ad_hoc_tags, classification_tags),
        ad_hoc_threshold=ad_hoc_threshold,
        escaped=escaped_defects(items),
        blocked=stale_blocked(items, blocked_days, now),
        routing_left=routing_tags_left(items, done_states, routing_tags),
        tag_drift=tag_case_drift(items),
        untagged=untagged(items, classification_tags, ticket_types),
        no_module=[] if unused else no_module,
        area_path_unused=unused,
    )


def _bullets(lines: list[str], limit: int = 10) -> str:
    shown = lines[:limit]
    if len(lines) > limit:
        shown.append(f"…và {len(lines) - limit} mục nữa")
    return "\n".join(f"  • {line}" for line in shown)


def render_text(report: HealthReport) -> str:
    """Plain-text digest — readable in Teams, Zalo, email body and a log line alike."""
    ratio = report.ad_hoc
    flag = "⚠️" if ratio.over(report.ad_hoc_threshold) else "✅"
    out = [
        f"📋 Sức khoẻ quy trình — {report.project or 'toàn bộ'} "
        f"({report.window_days} ngày, {report.items} work item)",
        "",
        f"{flag} Việc ngoài kế hoạch: {ratio.percent}% ({ratio.ad_hoc}/{ratio.total}) "
        f"— ngưỡng {report.ad_hoc_threshold}%",
    ]
    if ratio.by_tag:
        out.append("  " + " · ".join(f"{k}: {v}" for k, v in sorted(ratio.by_tag.items())))
    if report.escaped:
        total = sum(report.escaped.values())
        out += ["", f"🐞 Lỗi lọt qua QC (UAT/Production): {total}",
                _bullets([f"{mod}: {n}" for mod, n in report.escaped.items()])]
    if report.blocked:
        out += ["", f"⛔ Bị chặn quá lâu: {len(report.blocked)}",
                _bullets([f"#{i.id} {i.title[:60]}" for i in report.blocked])]
    if report.routing_left:
        out += ["", f"🏷️ Item đã xong nhưng còn tag định tuyến: {len(report.routing_left)}",
                _bullets([f"#{i.id} — {', '.join(tags)}" for i, tags in report.routing_left])]
    if report.tag_drift:
        out += ["", f"🔤 Tag lệch hoa/thường (query sẽ sót): {len(report.tag_drift)}",
                _bullets([" / ".join(v) for v in report.tag_drift.values()])]
    if report.untagged:
        out += ["", f"❓ Chưa gắn tag phân loại: {len(report.untagged)}",
                _bullets([f"#{i.id} {i.title[:60]}" for i in report.untagged])]
    if report.no_module:
        out += ["", f"📁 Chưa chọn module (Area Path): {len(report.no_module)}",
                _bullets([f"#{i.id} {i.title[:60]}" for i in report.no_module])]
    elif report.area_path_unused:
        out += ["", "📁 Project này chưa dùng Area Path để tách module "
                    "(mọi item ở gốc) — bỏ qua kiểm tra module."]
    if not report.has_findings:
        out += ["", "Không có gì bất thường."]
    return "\n".join(out)
