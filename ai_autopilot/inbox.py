"""The decision inbox: every place a human decision is waiting, on one page.

Each of these already had its own page — the held queue, spec drift, PR conflicts,
the fleet, the knowledge pool, security — and each page was right about its own
corner. What none of them could answer is the question a lead actually opens the
dashboard with: *what is waiting for ME, and what first?* Answering it meant visiting
six pages in a fixed order and doing the triage in one's head, which in practice meant
the pages nobody visited that day accumulated decisions nobody knew were pending.

So this module reads the same repositories those pages read — nothing new is stored —
and turns every pending decision into one :class:`InboxEntry` on a common scale.

Tiering — one rule for every source, so a red entry means the same thing wherever it
came from:

* 🔴 **red, "Cần chốt hôm nay"** — something is BLOCKED until a person acts, or the
  decision belongs to the customer/BA (the agent is not allowed to make it), or a
  machine is gone. Concretely: an item held in ``NEEDS_HUMAN``; a spec-drift point that
  needs a business decision (``needs_decision`` or an ``spec_unclear``/``assumption``
  kind); a PR conflict the resolver ESCALATED; a fleet worker that stopped reporting; a
  CRITICAL open security finding (a release gate fails on it).
* 🟡 **yellow, "Trong tuần"** — nothing is blocked today, but quality degrades the
  longer it is left: any other spec-drift point (spec and code drift further apart), a
  conflict sitting in an interactive session, a paused worker or one low on disk, a
  lesson that keeps recurring (should become a rule), a fleet lesson corroborated by
  two or more machines, a HIGH security finding.
* 🟢 **green, "Để biết"** — informational: a fleet lesson only one machine reported,
  and the "and N more" overflow lines.

Every source is read independently and fault-tolerantly: one broken table, a missing
repository on an older container, or a workspace directory that vanished must cost
the page that one source — reported as "không đọc được nguồn X" — never the whole
inbox. A blank inbox that is blank because of an exception is the worst possible
failure for this page, because it reads as "nothing waiting".

The module knows nothing about HTTP; actions are described as data (a link, or a form
posting to an EXISTING endpoint) and the route renders them. That keeps it testable
with a bare container and keeps the inbox from growing endpoints of its own that
would drift from the pages that own each decision.
"""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from ai_autopilot.logging_config import describe_exc, get_logger

_log = get_logger(__name__)

RED, YELLOW, GREEN = "red", "yellow", "green"
TIER_ORDER = (RED, YELLOW, GREEN)
TIERS: dict[str, tuple[str, str]] = {
    RED: ("🔴", "Cần chốt hôm nay"),
    YELLOW: ("🟡", "Trong tuần"),
    GREEN: ("🟢", "Để biết"),
}

#: source key → (icon, label). Order is the order sources are read and chips shown.
SOURCES: dict[str, tuple[str, str]] = {
    "held": ("🙋", "Item chờ người"),
    "spec": ("📐", "Lệch spec"),
    "conflict": ("⚔️", "Xung đột PR"),
    "fleet": ("🛰", "Fleet"),
    "knowledge": ("🧠", "Tri thức"),
    "security": ("🔐", "Bảo mật"),
}

#: Per source, beyond this many entries the rest collapse into one "còn N mục" line.
#: A page of 400 drift rows is not an inbox any more, and the owning page already
#: knows how to show all of them.
PER_SOURCE_CAP = 60

_DRIFT_ASKS = ("spec_unclear", "assumption")


@dataclass
class InboxAction:
    """One button or link on an entry.

    ``method == "post"`` renders a form posting ``fields`` to ``href`` — always an
    endpoint the owning page already has, so the decision is recorded (audit log,
    ADO comment, flash) exactly as if it had been made there.
    """

    label: str
    href: str
    method: str = "get"
    fields: dict[str, Any] = field(default_factory=dict)
    confirm: str = ""
    title: str = ""
    external: bool = False


@dataclass
class InboxEntry:
    source: str
    tier: str
    icon: str
    title: str
    context: str = ""
    at: datetime | None = None
    actions: list[InboxAction] = field(default_factory=list)
    href: str = ""          # where the title links to, if anywhere

    @property
    def age_seconds(self) -> float | None:
        if self.at is None:
            return None
        return max(0.0, (datetime.now(UTC) - _aware(self.at)).total_seconds())


@dataclass
class Inbox:
    entries: list[InboxEntry] = field(default_factory=list)
    #: Labels of sources that raised — shown as a muted line, never hidden.
    failed: list[str] = field(default_factory=list)

    def count(self, tier: str) -> int:
        return sum(1 for e in self.entries if e.tier == tier)


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes for values written as UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _cap(source: str, entries: list[InboxEntry], href: str) -> list[InboxEntry]:
    if len(entries) <= PER_SOURCE_CAP:
        return entries
    rest = len(entries) - PER_SOURCE_CAP
    icon, label = SOURCES[source]
    return entries[:PER_SOURCE_CAP] + [InboxEntry(
        source=source, tier=GREEN, icon=icon, title=f"Còn {rest} mục {label.lower()} nữa",
        context="Mở trang gốc để xem đủ.", href=href,
        actions=[InboxAction("Xem tất cả →", href)],
    )]


# ── sources ─────────────────────────────────────────────────────────────────
# Each takes the container and returns its entries. Looked up BY NAME at call time
# (see ``gather``) so a test can replace one with a function that raises.


def _hold_reason(detail: str) -> tuple[str, str, str]:
    """Why an item is held, in words: ``(kind, icon, sentence)``.

    The poller writes a short machine reason into the state row; a person reading the
    inbox needs to know which decision is being asked of them — approve a plan, review
    a risky change, raise a budget — not the log line.
    """
    text = (detail or "").strip()
    low = text.lower()
    if low.startswith("plan posted"):
        return "plan", "📋", "Kế hoạch triển khai đã đăng lên work item — chờ duyệt rồi mới code."
    if low.startswith("risk gate:"):
        why = text.split(":", 1)[1].strip()
        return "risk", "🧨", "Thay đổi rủi ro cao, cần người duyệt: " + why
    if low.startswith("token budget exceeded"):
        return "budget", "💸", ("Vượt ngân sách token của item ("
                                + text.split(":", 1)[1].strip() + ") — không tự chạy lại.")
    return "held", "🙋", text or "Autopilot đã chuyển item này cho người xử lý."


async def _src_held(c) -> list[InboxEntry]:
    """Items the autopilot escalated: nothing moves on them until someone resumes."""
    from ai_autopilot.data.entities import PipelineState

    held = [s for s in await c.state_repo.all() if s.state == PipelineState.NEEDS_HUMAN]
    earliest = datetime.min.replace(tzinfo=UTC)
    held.sort(key=lambda s: _aware(s.updated_at) if s.updated_at else earliest)
    out = []
    for s in held:
        reason, icon, context = _hold_reason(s.detail or "")
        actions = []
        if reason == "plan":
            # The approved-plan tag is the whole protocol: the poller sees it on its
            # next cycle, clears the hold and builds what the plan says.
            actions.append(InboxAction(
                "✅ Duyệt kế hoạch", "/dashboard/inbox/approve-plan", "post",
                {"item_id": s.work_item_id},
                confirm=f"Duyệt kế hoạch của #{s.work_item_id} và cho autopilot triển khai?",
                title="Gắn tag duyệt kế hoạch — autopilot code theo kế hoạch ở vòng kế tiếp"))
        actions += [
            InboxAction("▶ Tiếp tục", "/dashboard/queue/resume", "post",
                        {"ids": s.work_item_id},
                        confirm=f"Trả #{s.work_item_id} lại cho autopilot?",
                        title="Gỡ trạng thái giữ và trả item lại cho autopilot"),
            InboxAction("Phòng task", f"/dashboard/task/{s.work_item_id}"),
        ]
        if s.pr_url:
            actions.append(InboxAction("PR ↗", s.pr_url, external=True))
        out.append(InboxEntry(
            source="held", tier=RED, icon=icon,
            title=f"#{s.work_item_id} — {s.title or 'không có tiêu đề'}",
            context=_short(context),
            at=s.updated_at, actions=actions, href=f"/dashboard/task/{s.work_item_id}",
        ))
    return _cap("held", out, "/dashboard/queue")


async def _src_spec(c) -> list[InboxEntry]:
    """Open spec-drift points, one entry per point so each can be decided in place."""
    from ai_autopilot import spec_drift

    rows = await c.spec_drift_repo.open_drifts()
    rows = sorted(rows, key=lambda r: _aware(r.created_at) if r.created_at else datetime.now(UTC))
    out = []
    for r in rows:
        ask = bool(getattr(r, "needs_decision", False)) or r.kind in _DRIFT_ASKS
        # No ``back`` field: the decide endpoint reads it as a kind filter for the
        # specs page, so the redirect lands on /dashboard/specs — where the rest of
        # that item's points are, which is the right next place anyway.
        fields = {"row_id": r.id}
        actions = [
            InboxAction("📝 Cập nhật spec", "/dashboard/specs/decide", "post",
                        {**fields, "decision": "update_spec"},
                        title=spec_drift.DECISIONS.get("update_spec", "")),
            InboxAction("🔧 Sửa code", "/dashboard/specs/decide", "post",
                        {**fields, "decision": "fix_code"},
                        title=spec_drift.DECISIONS.get("fix_code", "")),
            InboxAction("✔ Giữ nguyên", "/dashboard/specs/decide", "post",
                        {**fields, "decision": "accept"},
                        title=spec_drift.DECISIONS.get("accept", "")),
        ]
        where = f"{r.where} · " if r.where else ""
        out.append(InboxEntry(
            source="spec", tier=RED if ask else YELLOW,
            icon="❗" if ask else spec_drift.icon_for(r.kind),
            title=f"#{r.work_item_id} — {where}{_short(r.code_does or r.summary, 120)}",
            context=(("Cần khách / BA chốt · " if ask else "")
                     + spec_drift.label_for(r.kind)
                     + (f" · spec: “{_short(r.spec_says, 90)}”" if r.spec_says else "")),
            at=r.created_at, actions=actions,
            href=f"/dashboard/task/{r.work_item_id}?tab=drift",
        ))
    return _cap("spec", out, "/dashboard/specs")


async def _src_conflict(c) -> list[InboxEntry]:
    """PR conflicts that are waiting on a person: escalated, or in an open session."""
    from ai_autopilot import pr_conflicts as pc

    out = []
    for status in (pc.ESCALATED, pc.IN_SESSION):
        for r in await c.pr_conflict_repo.recent(limit=200, status=status):
            escalated = status == pc.ESCALATED
            actions = []
            if escalated:
                actions.append(InboxAction(
                    "▶ Thử giải lại", f"/dashboard/conflicts/{r.id}/resolve", "post",
                    title="Chạy lại phiên tự giải conflict cho PR này"))
            else:
                actions.append(InboxAction(
                    "✕ Đóng phiên", f"/dashboard/conflicts/{r.id}/cancel", "post",
                    confirm="Đóng phiên? Nhánh giữ nguyên như hiện tại."))
            if r.url:
                actions.append(InboxAction("PR ↗", r.url, external=True))
            actions.append(InboxAction("Xung đột PR", "/dashboard/conflicts"))
            why = _short(r.last_error, 120) if escalated and r.last_error else ""
            out.append(InboxEntry(
                source="conflict", tier=RED if escalated else YELLOW, icon="⚔️",
                title=f"PR !{r.pr_id} — {r.title or r.source_branch or 'không có tiêu đề'}",
                context=("Bot đã thử và chuyển cho người" if escalated
                         else "Đang có phiên tương tác — chờ người vào xử lý")
                        + f" · {r.repo_name or r.repo_id} → {r.target_branch or '?'}"
                        + (f" · {why}" if why else ""),
                at=r.first_seen, actions=actions, href=r.url or "/dashboard/conflicts",
            ))
    return _cap("conflict", out, "/dashboard/conflicts")


async def _src_fleet(c) -> list[InboxEntry]:
    """Central only: machines that went quiet, were paused, or are running out of disk.

    The same thresholds the Fleet page uses (``fleet_offline_after_minutes``,
    ``fleet_disk_warn_gb``), so the two pages never disagree about a machine.
    """
    from ai_autopilot import fleet as fleet_mod

    cfg = c.config
    if (getattr(cfg, "fleet_role", "") or "") != fleet_mod.ROLE_CENTRAL:
        return []
    now = datetime.now(UTC)
    offline_after = max(1, int(getattr(cfg, "fleet_offline_after_minutes", 30) or 30)) * 60
    disk_warn = float(getattr(cfg, "fleet_disk_warn_gb", 0) or 0)
    out = []
    for row in await c.fleet_repo.list_all():
        seen = _aware(row.last_seen)
        try:
            health = json.loads(getattr(row, "health", "") or "{}")
        except ValueError:
            health = {}
        if not isinstance(health, dict):
            health = {}
        name = row.name
        if (now - seen).total_seconds() > offline_after:
            out.append(InboxEntry(
                source="fleet", tier=RED, icon="🔌", title=f"Máy {name} mất liên lạc",
                context=f"{row.hostname or name} · v{row.version or '?'} · không báo về quá "
                        f"{offline_after // 60} phút",
                at=row.last_seen, href="/dashboard/fleet",
                actions=[
                    InboxAction("Mở Fleet", "/dashboard/fleet"),
                    InboxAction("Quên máy", "/dashboard/fleet/forget", "post", {"name": name},
                                confirm=f"Bỏ máy {name} khỏi danh sách? Chỉ làm khi máy đã "
                                        "ngừng dùng hẳn."),
                ],
            ))
            # A machine that is gone has stale health figures; do not also flag them.
            continue
        if health.get("poller") == fleet_mod.POLLER_PAUSED:
            out.append(InboxEntry(
                source="fleet", tier=YELLOW, icon="⏸", title=f"Máy {name} đang tạm dừng",
                context=_short(health.get("paused_reason") or "Không nhận việc mới cho tới khi "
                                                              "được tiếp tục."),
                at=row.last_seen, href="/dashboard/fleet",
                actions=[
                    InboxAction("▶ Tiếp tục", "/dashboard/fleet/command", "post",
                                {"name": name, "kind": fleet_mod.CMD_RESUME}),
                    InboxAction("Mở Fleet", "/dashboard/fleet"),
                ],
            ))
        free = health.get("disk_free_gb")
        try:
            low = free is not None and disk_warn > 0 and float(free) < disk_warn
        except (TypeError, ValueError):
            low = False
        if low:
            out.append(InboxEntry(
                source="fleet", tier=YELLOW, icon="💾", title=f"Máy {name} sắp hết ổ đĩa",
                context=f"Còn {float(free):.1f} GB (ngưỡng cảnh báo {disk_warn:g} GB)",
                at=row.last_seen, href="/dashboard/fleet",
                actions=[InboxAction("Mở Fleet", "/dashboard/fleet")],
            ))
    return _cap("fleet", out, "/dashboard/fleet")


async def _src_knowledge(c) -> list[InboxEntry]:
    """Fleet lessons awaiting approval (central) and local lessons ripe to be rules."""
    from ai_autopilot import fleet as fleet_mod
    from ai_autopilot import lessons as lessons_mod

    cfg = c.config
    out = []
    store = getattr(c, "fleet_knowledge_repo", None)
    if (getattr(cfg, "fleet_role", "") or "") == fleet_mod.ROLE_CENTRAL and store is not None:
        for row in await store.list_all(status="draft"):
            try:
                origins = json.loads(row.origins or "[]")
            except ValueError:
                origins = []
            n = len(origins) if isinstance(origins, list) else 0
            out.append(InboxEntry(
                source="knowledge", tier=YELLOW if n >= 2 else GREEN, icon="🧠",
                title=f"Tri thức chờ duyệt: {_short(row.text, 120)}",
                context=f"{row.repo or 'mọi repo'} · {n} máy · ×{row.occurrences}",
                at=row.last_seen, href="/dashboard/learning",
                actions=[
                    InboxAction("Duyệt", "/dashboard/learning/pool/approved", "post",
                                {"key": row.key}),
                    InboxAction("Bỏ", "/dashboard/learning/pool/rejected", "post",
                                {"key": row.key}),
                ],
            ))
    workspace = getattr(cfg, "workspace_directory", "") or ""
    if workspace:
        for repo in lessons_mod.list_repos(workspace):
            for le in lessons_mod.entries(workspace, repo):
                if not le.recurring:
                    continue
                at = None
                with contextlib.suppress(TypeError, ValueError):
                    at = datetime.combine(date.fromisoformat(le.date), datetime.min.time(),
                                          tzinfo=UTC)
                shown_repo = "mọi repo" if repo == lessons_mod.SHARED_BUCKET else repo
                out.append(InboxEntry(
                    source="knowledge", tier=YELLOW, icon="📌",
                    title=f"Bài học lặp lại {le.count} lần: {_short(le.text, 120)}",
                    context=f"{shown_repo} · nên nâng thành quy tắc mà mọi run đều nạp",
                    at=at, href="/dashboard/learning",
                    actions=[InboxAction("📌 Nâng thành quy tắc", "/dashboard/learning/promote",
                                         "post", {"repo": repo, "text": le.text})],
                ))
    return _cap("knowledge", out, "/dashboard/learning")


async def _src_security(c) -> list[InboxEntry]:
    """Open CRITICAL (red) and HIGH (yellow) findings across every scanned repo."""
    from pathlib import PurePath

    cfg = c.config
    can_file = bool(getattr(cfg, "ado_pat", "")) and getattr(cfg, "autonomy_level", "") != "report"
    out = []
    for sev in ("critical", "high"):
        rows = await c.security_repo.list_findings(status="open", severity=sev, limit=200)
        for r in rows:
            actions = [InboxAction("Chi tiết", f"/dashboard/security/f/{r.id}")]
            if can_file and not r.ado_bug_id:
                actions.append(InboxAction("🐞 Tạo Bug", f"/dashboard/security/{r.id}/file",
                                           "post"))
            loc = f"{r.file}:{r.line}" if r.line else (r.file or "")
            out.append(InboxEntry(
                source="security", tier=RED if sev == "critical" else YELLOW,
                icon="🔐", title=f"[{sev.upper()}] {_short(r.title, 120)}",
                context=" · ".join(x for x in (
                    PurePath(r.repo).name if r.repo else "", _short(loc, 80),
                    r.cve or "", "KEV" if r.kev else "",
                    f"Bug #{r.ado_bug_id}" if r.ado_bug_id else "",
                ) if x),
                at=r.first_seen, actions=actions, href=f"/dashboard/security/f/{r.id}",
            ))
    return _cap("security", out, "/dashboard/security")


# ── aggregation ─────────────────────────────────────────────────────────────


async def gather(container) -> Inbox:
    """Read every source; a source that raises is recorded in ``failed``, not fatal."""
    module = sys.modules[__name__]
    inbox = Inbox()
    for key in SOURCES:
        fn = getattr(module, f"_src_{key}")
        try:
            inbox.entries.extend(await fn(container))
        except Exception as exc:  # noqa: BLE001 — one source must not blank the page
            _log.warning("inbox source failed", source=key, error=describe_exc(exc))
            inbox.failed.append(SOURCES[key][1])
    # Most urgent tier first; inside a tier, whatever has waited LONGEST first — the
    # oldest red is the one most likely already costing something.
    far_future = datetime.max.replace(tzinfo=UTC)
    inbox.entries.sort(key=lambda e: (
        TIER_ORDER.index(e.tier), _aware(e.at) if e.at else far_future,
    ))
    return inbox


async def collect(container) -> list[InboxEntry]:
    """Every pending decision, sorted by tier then age. Failed sources are dropped."""
    return (await gather(container)).entries
