"""Release note: the work items delivered in a window, grouped by tenant.

The ``release-note`` skill answers the same question by hand — a WIQL query, a table per
tenant, a version for the customer without ids — and it costs a model run each time. The
facts it needs are already on this machine: the delivery tracker records every state a
work item enters, so "what shipped in the last seven days" is a local query plus one
batched read of the items' titles and tags. This module is the pure half: given those
two inputs it builds the note. No I/O, so every rule below is testable without ADO.

Rules, kept identical to the skill so the two never disagree about the same week:

* **Delivered** = the FIRST time an item entered a done state inside ``[start, end)``.
  An item already done before the window is not this window's output; one resolved,
  reopened and resolved again counts once. By default "done" is the Resolved/Completed
  state CATEGORY — the same rule as the Delivery page. Naming states
  (``release_note_states``, e.g. ``Pre-Release``) switches to those state names, for a
  team whose release is a state rather than a category.
* **Tenant** = a tag from ``release_note_tenant_tags`` (TLCL, TLLA, VPM…). An item that
  carries two tenant tags appears under both — it shipped to both. No tenant tags
  configured → grouped by project. An item matching none → :data:`COMMON`.
* **Ticket code** = the customer's reference at the start of the title (``TLCL.18``),
  stripped from the displayed title. Never invented: no code, blank cell.
* **Customer version** never carries a work-item id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ai_autopilot import delivery

COMMON = "Chung"
#: A customer ticket reference at the start of a title: ``TLCL.18``, ``VPM.204``.
_TICKET = re.compile(r"^\s*([A-Z]{2,5}\.\d+)\s*(?:[-–—:|.]\s*)?")
#: How many items one Teams group lists before "… +N more" — a card is read on a phone.
TEAMS_LINES_PER_GROUP = 15


@dataclass(frozen=True)
class Entry:
    id: int
    ticket: str
    title: str
    work_item_type: str
    project: str
    delivered_at: datetime


@dataclass
class Group:
    tenant: str
    entries: list[Entry] = field(default_factory=list)


@dataclass
class ReleaseNote:
    start: datetime
    end: datetime
    days: int
    groups: list[Group] = field(default_factory=list)
    #: When the tracker started recording. A window that begins before it is partial,
    #: and the page says so instead of presenting a short list as the whole week.
    history_since: datetime | None = None
    error: str = ""

    @property
    def total(self) -> int:
        """Distinct items — one shipped to two tenants is still one item."""
        return len({e.id for g in self.groups for e in g.entries})

    @property
    def partial(self) -> bool:
        return self.history_since is None or _utc(self.history_since) > self.start

    def customer_rows(self) -> list[tuple[str, str]]:
        """``(ticket, title)`` for the merged customer version: no ids, no repeats."""
        seen: set[int] = set()
        rows: list[tuple[str, str]] = []
        for group in self.groups:
            for e in group.entries:
                if e.id not in seen:
                    seen.add(e.id)
                    rows.append((e.ticket, e.title))
        return rows


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def split_ticket(title: str) -> tuple[str, str]:
    """``("TLCL.18", "rest of the title")``, or ``("", title)`` when there is no code."""
    title = (title or "").strip()
    m = _TICKET.match(title)
    if not m:
        return "", title
    return m.group(1), title[m.end():].strip() or title


def released_between(
    changes: list, start: datetime, end: datetime, states: list[str] | None = None,
) -> dict[int, datetime]:
    """``{work_item_id: first time it was released}`` within ``[start, end)``.

    No ``states`` → the delivery rule (done category). With ``states`` → entering one
    of those state NAMES, case-insensitively, under the same first-entry rule."""
    wanted = {s.strip().lower() for s in (states or []) if s and s.strip()}
    if not wanted:
        return delivery.delivered_between(changes, start, end)
    timelines: dict[int, list] = {}
    for change in changes:
        timelines.setdefault(change.work_item_id, []).append(change)
    out: dict[int, datetime] = {}
    for wid, timeline in timelines.items():
        for change in sorted(timeline, key=lambda c: _utc(c.entered_at)):
            if (change.state or "").strip().lower() not in wanted:
                continue
            at = _utc(change.entered_at)
            if start <= at < end:
                out[wid] = at
            break        # the first release decides; an earlier one is not this window's
    return out


def tenants_of(tags: list[str], project: str, tenant_tags: list[str]) -> list[str]:
    """Which groups an item belongs to, in the configured tenant order."""
    configured = [t.strip() for t in tenant_tags if t and t.strip()]
    if not configured:
        return [project or COMMON]
    have = {t.strip().lower() for t in tags}
    hits = [t for t in configured if t.lower() in have]
    return hits or [COMMON]


def build(
    items: list, released: dict[int, datetime], *, tenant_tags: list[str],
    start: datetime, end: datetime, days: int, history_since: datetime | None = None,
) -> ReleaseNote:
    """Group the released ``items`` (WorkItemInfo-like) into a note. Pure.

    Group order: the configured tenant order (or projects alphabetically), with
    :data:`COMMON` last. Inside a group: by ticket code, then by id, so the same week
    always renders the same way and two notes can be compared by eye.
    """
    by_tenant: dict[str, list[Entry]] = {}
    for item in items:
        at = released.get(item.id)
        if at is None:
            continue
        ticket, title = split_ticket(item.title)
        entry = Entry(id=item.id, ticket=ticket, title=title,
                      work_item_type=item.work_item_type or "", project=item.project or "",
                      delivered_at=at)
        for tenant in tenants_of(list(item.tags or []), item.project or "", tenant_tags):
            by_tenant.setdefault(tenant, []).append(entry)
    configured = [t.strip() for t in tenant_tags if t and t.strip()]
    order = configured if configured else sorted(k for k in by_tenant if k != COMMON)
    names = [t for t in order if t in by_tenant] + ([COMMON] if COMMON in by_tenant else [])
    groups = [
        Group(tenant=name, entries=sorted(
            by_tenant[name], key=lambda e: (e.ticket == "", _ticket_key(e.ticket), e.id)))
        for name in names
    ]
    return ReleaseNote(start=start, end=end, days=days, groups=groups,
                       history_since=history_since)


def _ticket_key(ticket: str) -> tuple[str, int]:
    prefix, _, num = ticket.partition(".")
    return prefix, int(num) if num.isdigit() else 0


def render_teams(note: ReleaseNote, *, link_base: str = "", title: str = "") -> tuple[str, str]:
    """``(heading, text)`` for the Teams card. ``title`` replaces the default heading
    (the morning brief names the day rather than a span).

    Bullets, not a table: an Adaptive Card TextBlock does not render markdown tables. The
    full tables — and the customer version — are one click away on the dashboard page.
    """
    span = f"{_local_day(note.start)} – {_local_day(note.end)}"
    heading = title or f"📣 Release note {note.days} ngày ({span}) — {note.total} hạng mục"
    if not note.groups:
        return heading, "Không có hạng mục nào được giao trong khoảng này."
    lines: list[str] = []
    for group in note.groups:
        lines.append(f"**{group.tenant}** ({len(group.entries)})")
        for e in group.entries[:TEAMS_LINES_PER_GROUP]:
            ref = f"[#{e.id}]({link_base}{e.id})" if link_base else f"#{e.id}"
            code = f"{e.ticket} — " if e.ticket else ""
            lines.append(f"- {ref} {code}{e.title}")
        extra = len(group.entries) - TEAMS_LINES_PER_GROUP
        if extra > 0:
            lines.append(f"- … và {extra} hạng mục khác")
        lines.append("")
    if note.partial:
        lines.append("_Lưu ý: lịch sử trạng thái mới được ghi từ "
                     f"{_local_day(note.history_since) if note.history_since else 'gần đây'}"
                     " — danh sách có thể chưa đủ cả khoảng._")
    return heading, "\n".join(lines).strip()


def _local_day(value: datetime | None) -> str:
    return _utc(value).strftime("%d/%m/%Y") if value else "—"
