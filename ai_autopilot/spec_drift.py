"""Spec drift: telling a human when the code and the written item stopped agreeing.

An autonomous agent is told to decide rather than to ask — that is what keeps a run
from stalling on a clarifying question nobody answers. The cost is that every decision
it makes on the team's behalf is invisible: the item still says one thing, the merged
code now does another, and the person who has to keep the specification true never
finds out. Weeks later that gap surfaces as a defect in UAT, or as a spec somebody
rewrites from scratch because it can no longer be trusted.

So the agent reports what it had to decide, and this module turns those reports into
things a person can act on:

* a work-item comment carrying a **fixed prefix**, so the whole backlog can be queried
  for it and so a human reading the item can tell this notice from ordinary bot chatter;
* a **tag** the board and the dashboard filter on;
* rows a BA can tick off once the specification has been brought back in line.

The prefix is deliberately load-bearing and deliberately boring: it is a contract with
future queries (ADO search, WIQL ``Contains``, a dashboard scan), so it must stay
stable across releases even if the wording around it changes.

Pure and dependency-free on purpose — every decision here is testable from literals,
and the I/O (ADO calls, database writes) belongs to the caller.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from ai_autopilot.execution.result_contract import Deviation

# The marker every spec-drift notice carries. Stable across releases: queries, board
# filters and the "already reported" check below all match on it.
DRIFT_PREFIX = "⚠️ SPEC-DRIFT"
# Its counterpart, written when a human confirms the specification is back in line.
RESOLVED_PREFIX = "✅ SPEC-UPDATED"

# Vietnamese labels — this is read by the BA/PM who owns the specification, not by the
# engineer who ran the agent.
KIND_LABELS: dict[str, str] = {
    "spec_unclear": "Mô tả chưa rõ — agent phải tự chọn",
    "logic_differs": "Code khác mô tả",
    "spec_gap": "Mô tả chưa nói tới trường hợp này",
    "out_of_scope": "Phát hiện ngoài phạm vi item",
    "assumption": "Giả định cần xác nhận",
}
KIND_ICONS: dict[str, str] = {
    "spec_unclear": "❓",
    "logic_differs": "🔀",
    "spec_gap": "🕳️",
    "out_of_scope": "📦",
    "assumption": "💭",
}


def label_for(kind: str) -> str:
    return KIND_LABELS.get(kind, KIND_LABELS["assumption"])


def icon_for(kind: str) -> str:
    return KIND_ICONS.get(kind, KIND_ICONS["assumption"])


@dataclass(frozen=True)
class DriftNotice:
    """A rendered notice plus the facts the caller needs to file it."""

    html: str
    count: int
    kinds: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return self.count == 0


def _esc(text: str, limit: int = 1200) -> str:
    """Escape and bound one field.

    Agent text lands verbatim in an ADO comment (HTML) — unescaped it can break the
    comment's markup, and an agent that pastes a whole diff into ``detail`` would push
    a comment past what anyone will read.
    """
    clean = " ".join((text or "").split())
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return html.escape(clean)


#: What a BA can decide about one point. Stable keys — stored per row.
DECISIONS: dict[str, str] = {
    "update_spec": "📝 Cập nhật spec theo code",
    "fix_code": "🔧 Sửa code theo spec",
    "accept": "✅ Giữ nguyên — không cần đổi",
}
#: Kinds that are a question for a human by nature, whatever the agent flagged.
_ASKS = ("spec_unclear", "assumption")


def decision_label(key: str) -> str:
    return DECISIONS.get(key, "—")


def needs_decision(dev: Deviation) -> bool:
    return bool(getattr(dev, "needs_decision", False)) or dev.kind in _ASKS


def render_comment(
    deviations: list[Deviation], *, pr_url: str = "", tag: str = "", dashboard_url: str = ""
) -> DriftNotice:
    """The work-item comment, in the four parts a BA works through.

    1. what was decided and by whom; 2. a table per affected spec point — *Mục ·
    Hiện tại · Điều chỉnh* — quoting the spec so the line to replace can be found;
    3. what landed outside the item's scope (QC cannot read a diff, so unlisted means
    untested); 4. what needs a business decision rather than a wording fix. The same
    shape the team's own spec-drift rule asks humans to write.
    """
    items = [d for d in deviations if not d.is_empty]
    if not items:
        return DriftNotice(html="", count=0, kinds=())

    in_scope = [d for d in items if d.kind != "out_of_scope"]
    outside = [d for d in items if d.kind == "out_of_scope"]
    asks = [d for d in items if needs_decision(d)]

    def row(dev: Deviation) -> str:
        where = _esc(dev.where, 200) if dev.where else "—"
        now = _esc(dev.spec_says) if getattr(dev, "spec_says", "") else (
            "<i>(agent không trích — xem mô tả item)</i>")
        then = _esc(getattr(dev, "code_does", "") or dev.summary)
        why = _esc(dev.detail) if dev.detail else ""
        return (
            f"<tr><td>{icon_for(dev.kind)} <b>{where}</b><br/>"
            f"<small>{html.escape(label_for(dev.kind))}</small></td>"
            f"<td>{now}</td>"
            f"<td>{then}{f'<br/><small>Lý do: {why}</small>' if why else ''}</td></tr>"
        )

    parts = [
        f"<div><b>{DRIFT_PREFIX}</b> — {len(items)} điểm code không khớp mô tả",
        "<h3>1. Quyết định</h3>"
        "<p>Agent được yêu cầu tự quyết thay vì hỏi; các điểm dưới đây là quyết định nó "
        "đưa ra thay cho đội. <b>Spec hiện KHÔNG phản ánh đúng code.</b></p>",
    ]
    if in_scope:
        parts.append(
            "<h3>2. Mục spec bị ảnh hưởng</h3>"
            "<table border='1' cellpadding='4' style='border-collapse:collapse'>"
            "<tr><th>Mục</th><th>Hiện tại (spec)</th><th>Điều chỉnh (code)</th></tr>"
            + "".join(row(d) for d in in_scope) + "</table>"
        )
    if outside:
        parts.append(
            "<h3>3. Phát sinh ngoài phạm vi</h3><ul>"
            + "".join(
                f"<li>{_esc(d.summary)}"
                f"{f' — <code>{_esc(d.where, 200)}</code>' if d.where else ''}</li>"
                for d in outside
            )
            + "</ul><p><i>QC không đọc được diff — mục nào không nằm ở đây sẽ không ai "
              "test.</i></p>"
        )
    if asks:
        parts.append(
            "<h3>4. Cần chốt lại</h3><ul>"
            + "".join(
                f"<li>{_esc(d.where, 200) + ': ' if d.where else ''}{_esc(d.summary)}</li>"
                for d in asks
            )
            + "</ul><p><i>Đừng để agent quyết thay khách — BA/khách chốt từng điểm.</i></p>"
        )
    if pr_url:
        parts.append(f'PR: <a href="{html.escape(pr_url)}">{html.escape(pr_url)}</a><br/>')
    parts.append(
        "<b>Cần làm:</b> với từng điểm, BA chọn cập nhật mô tả/AC cho khớp code, "
        "yêu cầu sửa code cho khớp mô tả, hoặc giữ nguyên."
    )
    if tag:
        parts.append(
            f" Item được gắn tag <code>{html.escape(tag)}</code> cho tới khi mọi điểm được quyết."
        )
    if dashboard_url:
        parts.append(
            f'<br/>Quyết định trên dashboard: <a href="{html.escape(dashboard_url)}">'
            f'{html.escape(dashboard_url)}</a>'
        )
    parts.append("</div>")
    return DriftNotice(
        html="".join(parts), count=len(items), kinds=tuple(d.kind for d in items)
    )


def render_decisions_comment(rows: list, by: str = "") -> str:
    """The closing note once every point on an item is decided — one comment, not one
    per click, listing what was decided so QC knows which AC changed and which code
    still has to change."""
    lines = []
    for r in rows:
        where = f"<b>{_esc(r.where, 200)}</b>: " if getattr(r, "where", "") else ""
        note = f" — <i>{_esc(r.decision_note, 400)}</i>" if getattr(r, "decision_note", "") else ""
        lines.append(f"<li>{decision_label(r.decision)} — {where}{_esc(r.summary, 300)}{note}</li>")
    fix = sum(1 for r in rows if r.decision == "fix_code")
    tail = (f"<p><b>{fix} điểm cần sửa code</b> — tạo task / rework trước khi nghiệm thu.</p>"
            if fix else "")
    who = f" bởi {html.escape(by)}" if by else ""
    return (f"<div><b>{RESOLVED_PREFIX}</b> — đã quyết {len(rows)} điểm lệch spec{who}."
            f"<ul>{''.join(lines)}</ul>{tail}</div>")


def render_pr_comment(deviations: list[Deviation]) -> str:
    """The same facts as a PR thread comment — markdown, and shorter.

    The reviewer is the last person who can catch a wrong decision before it merges, so
    the notice has to reach the PR too. It stays terse: the work-item comment is the
    record, this is the nudge.
    """
    items = [d for d in deviations if not d.is_empty]
    if not items:
        return ""
    lines = [
        f"**{DRIFT_PREFIX}** — {len(items)} điểm code không khớp mô tả work item.",
        "",
        "Agent tự quyết ở những chỗ này (mô tả chưa nói rõ hoặc code cố ý khác):",
        "",
    ]
    for dev in items:
        where = f" (`{dev.where}`)" if dev.where else ""
        lines.append(f"- {icon_for(dev.kind)} **{label_for(dev.kind)}**{where}: {dev.summary}")
        if dev.detail:
            lines.append(f"  - {dev.detail}")
    lines += ["", "Reviewer: xác nhận quyết định này đúng ý — nếu không, comment để agent sửa."]
    return "\n".join(lines)


def render_resolved_comment(count: int, by: str = "") -> str:
    """Written back to the item when a human marks the specification updated."""
    who = f" bởi <b>{html.escape(by)}</b>" if by else ""
    return (
        f"<div><b>{RESOLVED_PREFIX}</b> — {count} điểm chênh lệch đã được xử lý{who}. "
        "Mô tả/AC của item đã khớp với code.</div>"
    )


_PREFIX_RE = re.compile(re.escape(DRIFT_PREFIX), re.IGNORECASE)


def already_reported(comments: list[str]) -> bool:
    """True when a drift notice is already on the item.

    A rework re-runs the agent, which re-reports the same decisions it made the first
    time; without this the item collects one identical notice per revision and the BA
    stops reading them.
    """
    return any(_PREFIX_RE.search(c or "") for c in comments)


def summarise(deviations: list[Deviation], limit: int = 3) -> str:
    """One-line summary for a log line, a notification, or a dashboard cell."""
    items = [d for d in deviations if not d.is_empty]
    if not items:
        return ""
    shown = "; ".join(d.summary for d in items[:limit])
    more = f" (+{len(items) - limit})" if len(items) > limit else ""
    return f"{shown}{more}"
