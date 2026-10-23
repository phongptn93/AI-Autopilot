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
* **Customer version** never carries a work-item id, and is cut PER TENANT: each tenant
  is a different customer, and a handover listing another customer's items is a leak.
* **Waiting** (``release_note_waiting_states``, e.g. ``Ready for UAT``) is a SNAPSHOT, not
  a window: every item sitting in one of those states now, oldest first, with how long it
  has waited. For a team whose release is "Ready for UAT → In UAT", this is the queue for
  the next production deploy — and the one number a PM needs on a morning when nothing
  shipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ai_autopilot import delivery

COMMON = "Chung"
#: A customer ticket reference at the start of a title: ``TLCL.18``, ``VPM.204``.
_TICKET = re.compile(r"^\s*([A-Z]{2,5}\.\d+)\s*(?:[-–—:|.]\s*)?")
#: How many items one Teams group lists before "… +N more" — a card is read on a phone.
TEAMS_LINES_PER_GROUP = 15
#: How many waiting items the card lists (the oldest) — the rest are on the page.
TEAMS_WAITING_LINES = 10


def item_url(link_base: str, wid: int) -> str:
    """The work item's browser URL. ``link_base`` is ``…/_workitems/edit`` with or without
    a trailing slash — gluing it on bare once produced ``…/edit8357``, a link to nothing."""
    return f"{link_base.rstrip('/')}/{wid}" if link_base else ""


@dataclass(frozen=True)
class Entry:
    id: int
    ticket: str
    title: str
    work_item_type: str
    project: str
    delivered_at: datetime


@dataclass(frozen=True)
class Waiting:
    """An item in a waiting state now. ``since`` is when it entered that state — from
    the recorded history, else ADO's last-changed date (``approx``: at least this old)."""

    id: int
    ticket: str
    title: str
    state: str
    tenants: tuple[str, ...]
    since: datetime | None = None
    approx: bool = False
    assignee: str = ""

    def age_days(self, now: datetime) -> int | None:
        return None if self.since is None else max(0, (_utc(now) - _utc(self.since)).days)


@dataclass
class Lane:
    """One role's slice of the board NOW — what Dev / QC / BA each have to act on.

    The note was written for whoever ships; the morning brief is read by every role,
    and a tester looking for "what is waiting for me" found nothing in it."""

    role: str
    states: tuple[str, ...]
    items: list[Waiting] = field(default_factory=list)    # oldest first

    def by_state(self, now: datetime) -> list[tuple[str, int, int | None]]:
        """``[(state, count, oldest age)]`` in the configured state order, zeros kept —
        "In Testing: 0" is information too."""
        out = []
        for state in self.states:
            ages = [w.age_days(now) for w in self.items if w.state.lower() == state.lower()]
            known = [a for a in ages if a is not None]
            out.append((state, len(ages), max(known) if known else None))
        return out


@dataclass(frozen=True)
class WipColumn:
    """A board column with a WIP limit, as Azure DevOps counts it."""

    project: str
    column: str
    count: int
    limit: int

    @property
    def over(self) -> bool:
        return self.count > self.limit


@dataclass(frozen=True)
class Analysis:
    """Claude's reading of the note: a headline, a health call, insights and actions
    addressed to roles. Built ONLY from the facts in :func:`analysis_facts`."""

    headline: str
    health: str                                   # good | watch | risk
    insights: tuple[str, ...] = ()
    actions: tuple[tuple[str, str], ...] = ()     # (role, what to do)


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
    #: Items waiting to be released NOW, oldest first (see the module doc).
    waiting: list[Waiting] = field(default_factory=list)
    waiting_states: list[str] = field(default_factory=list)
    #: A waiting item at least this many days old is flagged.
    stale_days: int = 7
    #: Per-role snapshots (``release_note_roles``) and the board's WIP columns.
    lanes: list[Lane] = field(default_factory=list)
    wip: list[WipColumn] = field(default_factory=list)
    #: Released in the window just before this one — None when history does not reach.
    previous_total: int | None = None
    analysis: Analysis | None = None

    def over_wip(self) -> list[WipColumn]:
        return [c for c in self.wip if c.over]

    @property
    def has_news(self) -> bool:
        """Anything worth posting: shipped, waiting, or on a role's plate."""
        return bool(self.total or self.waiting or any(lane.items for lane in self.lanes))

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

    def stale(self) -> list[Waiting]:
        """Waiting items at least ``stale_days`` old."""
        return [w for w in self.waiting if (w.age_days(self.end) or 0) >= self.stale_days]

    def oldest_wait(self) -> int | None:
        ages = [a for w in self.waiting if (a := w.age_days(self.end)) is not None]
        return max(ages) if ages else None

    def waiting_by_tenant(self) -> list[tuple[str, int]]:
        """``[(tenant, count)]`` for the waiting queue, in first-seen order."""
        counts: dict[str, int] = {}
        for w in self.waiting:
            for t in w.tenants:
                counts[t] = counts.get(t, 0) + 1
        return list(counts.items())


def handover_markdown(note: ReleaseNote, group: Group) -> str:
    """One tenant's handover table — what that customer receives. No internal ids."""
    head = (f"**Danh sách hạng mục đã triển khai production — {group.tenant}** "
            f"({_local_day(note.start)} – {_local_day(note.end)})")
    rows = [f"| {n} | {e.ticket} | {e.title} |" for n, e in enumerate(group.entries, 1)]
    return "\n".join([head, "", "| STT | Mã | Hạng Mục |", "|---|---|---|", *rows])


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


def build_waiting(
    items: list, entered: dict[int, datetime], *, tenant_tags: list[str],
) -> list[Waiting]:
    """The waiting queue from items currently in a waiting state. Pure.

    ``entered``: when each item entered its current state, from the recorded history.
    Missing → ADO's last-changed date, marked approximate (a later edit moves it, so the
    real wait is at least that long). Oldest first; unknown age last."""
    out: list[Waiting] = []
    for item in items:
        ticket, title = split_ticket(item.title)
        since = entered.get(item.id)
        approx = since is None and item.changed_date is not None
        out.append(Waiting(
            id=item.id, ticket=ticket, title=title, state=item.state or "",
            tenants=tuple(tenants_of(list(item.tags or []), item.project or "", tenant_tags)),
            since=since if since is not None else item.changed_date, approx=approx,
            assignee=(item.assigned_to or "").strip(),
        ))
    return sorted(out, key=lambda w: (w.since is None, _utc(w.since) if w.since else NEVER, w.id))


NEVER = datetime.max.replace(tzinfo=UTC)


def parse_roles(mapping: dict[str, str] | None) -> list[tuple[str, list[str]]]:
    """``{"🛠️ Dev": "Rework Required", "🧪 QC": "Ready for Testing, In Testing"}`` →
    ``[(role, [states])]`` in the configured order; a role naming no state is dropped."""
    out = []
    for role, raw in (mapping or {}).items():
        states = [s.strip() for s in re.split(r"[,;]", raw or "") if s.strip()]
        if role.strip() and states:
            out.append((role.strip(), states))
    return out


def build_lanes(roles: list[tuple[str, list[str]]], queue: list[Waiting]) -> list[Lane]:
    """Split the snapshot ``queue`` (every role state, oldest first) into one lane per
    role. A state named by two roles shows in both — each of them has to act on it."""
    lanes = []
    for role, states in roles:
        wanted = {s.lower() for s in states}
        lanes.append(Lane(role=role, states=tuple(states),
                          items=[w for w in queue if w.state.lower() in wanted]))
    return lanes


# ── Claude's analysis ────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """Bạn là trưởng nhóm delivery giàu kinh nghiệm, viết phần NHẬN ĐỊNH \
cho bản tin {kind} gửi CẢ TEAM (Dev, QC, BA/PO, PM) trên Microsoft Teams.

Dữ liệu thật (chỉ dùng đúng những gì có ở đây — TUYỆT ĐỐI không bịa số, mã hay tên):
{facts}

Yêu cầu:
- Đánh giá thẳng thắn, chuyên nghiệp: điểm nghẽn ở đâu, rủi ro gì, so với kỳ trước ra sao.
- Ưu tiên điều đáng hành động: hàng chờ lâu, cột vượt WIP, rework nhiều, QC ùn.
- Mỗi việc cần làm gán cho MỘT vai trò (Dev, QC, BA/PO, PM hoặc Team), cụ thể, \
nêu mã #id khi có trong dữ liệu.
- Tiếng Việt, câu ngắn, không chào hỏi, không lặp lại nguyên bảng số liệu.

Trả về DUY NHẤT một JSON, không kèm chữ nào khác:
{{"headline": "1 câu tổng quan ≤ 25 từ",
 "health": "good | watch | risk",
 "insights": ["tối đa 4 nhận định, mỗi câu ≤ 30 từ"],
 "actions": [{{"role": "Dev|QC|BA/PO|PM|Team", "text": "việc cụ thể ≤ 25 từ"}}]}}
Tối đa 5 actions."""


def analysis_facts(note: ReleaseNote) -> str:
    """The note as compact text for Claude — every number it may cite, and nothing it
    could mistake for an instruction (titles are data, quoted)."""
    now = note.end
    lines = [f"Kỳ: {_local_day(note.start)} – {_local_day(note.end)} ({note.days} ngày)",
             f"Lên production trong kỳ: {note.total}"]
    if note.previous_total is not None:
        lines.append(f"Kỳ trước cùng độ dài: {note.previous_total}")
    for g in note.groups:
        lines.append(f"- {g.tenant}: {len(g.entries)} — "
                     + "; ".join(f"#{e.id} \"{e.title[:80]}\"" for e in g.entries[:8]))
    if note.waiting_states:
        lines.append(f"Chờ deploy ({', '.join(note.waiting_states)}): {len(note.waiting)}, "
                     f"lâu nhất {note.oldest_wait()} ngày, quá {note.stale_days} ngày: "
                     f"{len(note.stale())}")
        for w in note.waiting[:8]:
            lines.append(f"  #{w.id} chờ {w.age_days(now)} ngày \"{w.title[:80]}\"")
    for lane in note.lanes:
        parts = [f"{s}: {n} (lâu nhất {a} ngày)" if a is not None else f"{s}: {n}"
                 for s, n, a in lane.by_state(now)]
        lines.append(f"Vai trò {lane.role}: " + "; ".join(parts))
        for w in lane.items[:5]:
            lines.append(f"  #{w.id} [{w.state}] {w.age_days(now)} ngày, "
                         f"{w.assignee or 'chưa giao'} \"{w.title[:80]}\"")
    for c in note.wip:
        flag = " — VƯỢT" if c.over else ""
        lines.append(f"WIP {c.project}/{c.column}: {c.count}/{c.limit}{flag}")
    return "\n".join(lines)


def known_ids(note: ReleaseNote) -> set[int]:
    ids = {e.id for g in note.groups for e in g.entries} | {w.id for w in note.waiting}
    return ids | {w.id for lane in note.lanes for w in lane.items}


def parse_analysis(text: str, ids: set[int]) -> Analysis | None:
    """Claude's JSON → :class:`Analysis`, or None when it is not usable.

    A sentence citing a work item that is NOT in the facts is dropped: an invented id in
    a team-wide post sends someone to chase a ticket that does not exist."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    def grounded(s: object) -> str:
        s = str(s or "").strip()
        cited = {int(x) for x in re.findall(r"#(\d+)", s)}
        return s if s and cited <= ids else ""

    headline = grounded(data.get("headline"))
    if not headline:
        return None
    health = str(data.get("health") or "").strip().lower()
    insights = tuple(x for x in (grounded(i) for i in (data.get("insights") or [])[:4]) if x)
    actions = []
    for a in (data.get("actions") or [])[:5]:
        if isinstance(a, dict) and (text_ := grounded(a.get("text"))):
            actions.append((str(a.get("role") or "Team").strip()[:12], text_))
    return Analysis(headline=headline, health=health if health in HEALTH else "watch",
                    insights=insights, actions=tuple(actions))


#: health → (label, Adaptive Card container style, colour)
HEALTH = {"good": ("🟢 Ổn định", "good", "Good"),
          "watch": ("🟡 Cần để ý", "warning", "Warning"),
          "risk": ("🔴 Rủi ro", "attention", "Attention")}


def _ticket_key(ticket: str) -> tuple[str, int]:
    prefix, _, num = ticket.partition(".")
    return prefix, int(num) if num.isdigit() else 0


def render_teams(note: ReleaseNote, *, link_base: str = "", title: str = "") -> tuple[str, str]:
    """``(heading, text)`` for the Teams card. ``title`` replaces the default heading
    (the morning brief names the day rather than a span).

    Bullets, not a table: an Adaptive Card TextBlock does not render markdown tables. The
    full tables — and the customer version — are one click away on the dashboard page.
    """
    heading = title or weekly_title(note)
    lines: list[str] = []
    if note.analysis:
        a = note.analysis
        lines += [f"**🧠 {HEALTH.get(a.health, HEALTH['watch'])[0]} — {a.headline}**",
                  *[f"- {i}" for i in a.insights],
                  *[f"- **{role}**: {text}" for role, text in a.actions], ""]
    if note.over_wip():
        lines += ["**🚦 Vượt WIP:** " + " · ".join(
            f"{c.column} {c.count}/{c.limit}" for c in note.over_wip()), ""]
    for lane in note.lanes:
        lines.append(f"**{lane.role}:** " + " · ".join(
            f"{s} {n}" for s, n, _ in lane.by_state(note.end)))
    if note.lanes:
        lines.append("")
    if not note.groups:
        lines += ["Không có hạng mục nào lên production trong khoảng này.", ""]
    else:
        lines += ["**🚀 Đã lên production**", ""]
    for group in note.groups:
        lines.append(f"**{group.tenant}** ({len(group.entries)})")
        for e in group.entries[:TEAMS_LINES_PER_GROUP]:
            lines.append(f"- {_ref(e.id, link_base)} {_coded(e.ticket, e.title)}")
        extra = len(group.entries) - TEAMS_LINES_PER_GROUP
        if extra > 0:
            lines.append(f"- … và {extra} hạng mục khác")
        lines.append("")
    if note.waiting:
        lines += [f"**⏳ Chờ deploy ({len(note.waiting)})** — cũ nhất trước", ""]
        for w in note.waiting[:TEAMS_WAITING_LINES]:
            lines.append(f"- {_ref(w.id, link_base)} {_coded(w.ticket, w.title)}"
                         f" · {_age_text(w, note.end)}")
        extra = len(note.waiting) - TEAMS_WAITING_LINES
        if extra > 0:
            lines.append(f"- … và {extra} hạng mục khác")
        lines.append("")
    if note.partial:
        lines.append("_Lưu ý: lịch sử trạng thái mới được ghi từ "
                     f"{_local_day(note.history_since) if note.history_since else 'gần đây'}"
                     " — danh sách có thể chưa đủ cả khoảng._")
    return heading, "\n".join(lines).strip()


def weekly_title(note: ReleaseNote) -> str:
    span = f"{_local_day(note.start)} – {_local_day(note.end)}"
    return f"📣 Release {note.days} ngày ({span}) — {counts_text(note)}"


def counts_text(note: ReleaseNote) -> str:
    out = f"{note.total} lên production"
    if note.waiting_states:
        out += f" · {len(note.waiting)} chờ deploy"
    return out


def _ref(wid: int, link_base: str) -> str:
    url = item_url(link_base, wid)
    return f"[#{wid}]({url})" if url else f"#{wid}"


def _coded(ticket: str, title: str) -> str:
    return f"{ticket} — {title}" if ticket else title


def _age_text(w: Waiting, now: datetime) -> str:
    age = w.age_days(now)
    if age is None:
        return "chưa rõ"
    return f"{'≥ ' if w.approx else ''}{age} ngày" if age else "hôm nay"


# ── Adaptive Card ────────────────────────────────────────────────────────────


def render_card(note: ReleaseNote, *, link_base: str = "") -> list[dict]:
    """The card body under the heading, as native Adaptive Card elements.

    The markdown text above is the fallback for channels without cards (email, Zalo). In
    Teams a whole note in ONE TextBlock rendered as cramped nested bullets with the
    counts buried in the heading. Here: a KPI row a PM reads in a second, then one
    section per stage, each row an id column beside a wrapping title column.
    """
    body: list[dict] = [_kpis(note)]
    if note.analysis:
        body.append(_analysis_block(note.analysis))
    if note.over_wip():
        wip = [_section_title("🚦 VƯỢT GIỚI HẠN WIP", "Attention")]
        multi = len({c.project for c in note.wip}) > 1
        wip.append(_text(" · ".join(
            f"{c.project + '/' if multi else ''}{c.column} **{c.count}**/{c.limit}"
            for c in note.over_wip()), size="Small"))
        body.append(_container(wip))
    for lane in note.lanes:
        body.append(_lane_block(lane, note, link_base))
    deployed: list[dict] = [_section_title("🚀 ĐÃ LÊN PRODUCTION", "Good")]
    if not note.groups:
        deployed.append(_text("Không có hạng mục nào lên production trong khoảng này.",
                              subtle=True))
    for group in note.groups:
        deployed.append(_text(f"**{group.tenant}** · {len(group.entries)}", spacing="Medium"))
        for e in group.entries[:TEAMS_LINES_PER_GROUP]:
            deployed.append(_row(e.id, link_base, _coded(e.ticket, e.title)))
        extra = len(group.entries) - TEAMS_LINES_PER_GROUP
        if extra > 0:
            deployed.append(_text(f"… và {extra} hạng mục khác", subtle=True))
    body.append(_container(deployed))
    if note.waiting_states:
        states = ", ".join(note.waiting_states)
        waiting = [_section_title(f"⏳ CHỜ DEPLOY ({states}) — cũ nhất trước", "Warning")]
        if not note.waiting:
            waiting.append(_text("Không có hạng mục nào đang chờ.", subtle=True))
        else:
            split = " · ".join(f"{t} {n}" for t, n in note.waiting_by_tenant())
            waiting.append(_text(split, subtle=True, size="Small"))
        for w in note.waiting[:TEAMS_WAITING_LINES]:
            stale = (w.age_days(note.end) or 0) >= note.stale_days
            waiting.append(_row(w.id, link_base, _coded(w.ticket, w.title),
                                age=_age_text(w, note.end), alert=stale))
        extra = len(note.waiting) - TEAMS_WAITING_LINES
        if extra > 0:
            waiting.append(_text(f"… và {extra} hạng mục khác", subtle=True))
        body.append(_container(waiting))
    if note.partial:
        since = _local_day(note.history_since) if note.history_since else "gần đây"
        body.append(_text(f"Lịch sử trạng thái mới được ghi từ {since} — danh sách có thể "
                          "chưa đủ cả khoảng.", subtle=True, size="Small", spacing="Medium"))
    return body


def _analysis_block(a: Analysis) -> dict:
    label, style, color = HEALTH.get(a.health, HEALTH["watch"])
    items: list[dict] = [
        {"type": "ColumnSet", "columns": [
            {"type": "Column", "width": "stretch", "items": [
                _section_title("🧠 NHẬN ĐỊNH (Claude)", "Accent")]},
            {"type": "Column", "width": "auto", "items": [
                {"type": "TextBlock", "text": label, "size": "Small", "weight": "Bolder",
                 "color": color}]},
        ]},
        {"type": "TextBlock", "text": a.headline, "weight": "Bolder", "wrap": True,
         "spacing": "Small"},
    ]
    items += [_text(f"• {i}", size="Small") for i in a.insights]
    if a.actions:
        items.append(_text("**Việc cần làm**", size="Small", spacing="Medium"))
        items.append({"type": "FactSet", "spacing": "Small",
                      "facts": [{"title": role, "value": text} for role, text in a.actions]})
    return {"type": "Container", "style": style, "spacing": "Medium", "items": items}


#: How many items a role block lists (its oldest) — the counts carry the rest.
TEAMS_LANE_LINES = 3


def _lane_block(lane: Lane, note: ReleaseNote, link_base: str) -> dict:
    counts = " · ".join(
        f"{state} **{n}**" + (f" (lâu nhất {age}d)" if age else "")
        for state, n, age in lane.by_state(note.end))
    items: list[dict] = [_section_title(f"{lane.role.upper()} — {len(lane.items)} hạng mục",
                                        "Accent"),
                         _text(counts, size="Small")]
    for w in lane.items[:TEAMS_LANE_LINES]:
        stale = (w.age_days(note.end) or 0) >= note.stale_days
        who = f" · _{w.assignee}_" if w.assignee else ""
        items.append(_row(w.id, link_base, f"{_coded(w.ticket, w.title)}{who}",
                          age=_age_text(w, note.end), alert=stale))
    return _container(items)


def _kpis(note: ReleaseNote) -> dict:
    shipped = "Lên production"
    if note.previous_total is not None:
        delta = note.total - note.previous_total
        shipped += f" ({'+' if delta > 0 else ''}{delta} so với kỳ trước)" if delta else \
            " (bằng kỳ trước)"
    tiles = [(str(note.total), shipped, "Good")]
    if note.waiting_states:
        oldest = note.oldest_wait()
        stale = len(note.stale())
        tiles += [
            (str(len(note.waiting)), "Chờ deploy", "Warning" if note.waiting else "Default"),
            ("—" if oldest is None else f"{oldest}d", "Chờ lâu nhất",
             "Attention" if oldest is not None and oldest >= note.stale_days else "Default"),
            (str(stale), f"Chờ ≥ {note.stale_days} ngày", "Attention" if stale else "Default"),
        ]
    return {
        "type": "Container", "style": "emphasis", "bleed": True, "spacing": "Small",
        "items": [{"type": "ColumnSet", "columns": [
            {"type": "Column", "width": "stretch", "items": [
                {"type": "TextBlock", "text": value, "size": "ExtraLarge", "weight": "Bolder",
                 "color": color, "horizontalAlignment": "Center"},
                {"type": "TextBlock", "text": label, "size": "Small", "isSubtle": True,
                 "horizontalAlignment": "Center", "spacing": "None", "wrap": True},
            ]} for value, label, color in tiles
        ]}],
    }


def _container(items: list[dict]) -> dict:
    return {"type": "Container", "separator": True, "spacing": "Medium", "items": items}


def _section_title(text: str, color: str) -> dict:
    return {"type": "TextBlock", "text": text, "weight": "Bolder", "size": "Small",
            "color": color, "wrap": True}


def _text(text: str, *, subtle: bool = False, size: str = "", spacing: str = "Small") -> dict:
    block: dict = {"type": "TextBlock", "text": text, "wrap": True, "spacing": spacing}
    if subtle:
        block["isSubtle"] = True
    if size:
        block["size"] = size
    return block


def _row(wid: int, link_base: str, title: str, *, age: str = "", alert: bool = False) -> dict:
    columns = [
        {"type": "Column", "width": "60px", "items": [
            {"type": "TextBlock", "text": _ref(wid, link_base), "size": "Small"}]},
        {"type": "Column", "width": "stretch", "items": [
            {"type": "TextBlock", "text": title, "size": "Small", "wrap": True}]},
    ]
    if age:
        columns.append({"type": "Column", "width": "auto", "items": [
            {"type": "TextBlock", "text": ("⚠️ " if alert else "") + age, "size": "Small",
             "color": "Attention" if alert else "Default", "isSubtle": not alert}]})
    return {"type": "ColumnSet", "spacing": "Small", "columns": columns}


def _local_day(value: datetime | None) -> str:
    return _utc(value).strftime("%d/%m/%Y") if value else "—"
