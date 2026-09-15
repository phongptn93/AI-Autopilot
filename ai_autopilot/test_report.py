"""The QC result comment: what was executed, and what came out of it.

A QC run's outcomes reached the work item only when the agent's session happened to
have an MCP tool that could write a comment — so the same twenty-case pass took place
twice and was visible once (#8965). The same argument that moved test-case FILING into
the control plane moves REPORTING there: whether QC's verdict is visible must not
depend on which tools a session was handed.

Kept as pure functions, next to :mod:`spec_drift`, so the rendering can be tested
without a poller, an agent, or an ADO account.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from ai_autopilot.execution.result_contract import CaseOutcome

# Prefix used to recognise the comment again — mirrors DRIFT_PREFIX.
REPORT_PREFIX = "🧪 QC — Kết quả thực thi"

_ICONS = {"pass": "✅", "fail": "❌", "blocked": "⚠️"}
_LABELS = {"pass": "Đạt", "fail": "Không đạt", "blocked": "Chưa chạy được"}
# Failures first: a reader who stops after three rows must have seen the three rows
# that decide whether this item can move.
_ORDER = {"fail": 0, "blocked": 1, "pass": 2}
# A run with hundreds of cases would otherwise push a comment past what anyone reads.
_MAX_ROWS = 60


@dataclass(frozen=True)
class TestReport:
    html: str
    total: int
    passed: int
    failed: int
    blocked: int

    @property
    def is_empty(self) -> bool:
        return self.total == 0

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and self.failed == 0 and self.blocked == 0


def _esc(text: str, limit: int = 600) -> str:
    """Escape and bound one field — agent text lands verbatim in HTML."""
    clean = " ".join((text or "").split())
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return html.escape(clean)


def render_comment(results: list[CaseOutcome], *, dashboard_url: str = "") -> TestReport:
    """The work-item comment for a run that EXECUTED test cases.

    Written for whoever opens the item next and has to decide if it can move: the
    verdict first, then every case that is not a pass, then the passes.
    """
    items = [r for r in results if not r.is_empty]
    if not items:
        return TestReport(html="", total=0, passed=0, failed=0, blocked=0)

    passed = sum(1 for r in items if r.outcome == "pass")
    failed = sum(1 for r in items if r.outcome == "fail")
    blocked = len(items) - passed - failed

    # The headline is the number a reader wants before any table: did this pass.
    if failed:
        headline = f"❌ <b>{failed}/{len(items)} không đạt</b>"
    elif blocked:
        headline = f"⚠️ <b>{blocked}/{len(items)} chưa chạy được</b>"
    else:
        headline = f"✅ <b>{len(items)}/{len(items)} đạt</b>"

    shown = sorted(items, key=lambda r: (_ORDER.get(r.outcome, 3), r.title.lower()))
    hidden = max(0, len(shown) - _MAX_ROWS)
    shown = shown[:_MAX_ROWS]

    rows = []
    for res in shown:
        icon = _ICONS.get(res.outcome, "⚠️")
        label = _LABELS.get(res.outcome, res.outcome)
        # The note is what makes a failure actionable, so it is a column rather than a
        # tooltip — and blank for a pass, where there is nothing to act on.
        note = _esc(res.note) if res.note else ""
        rows.append(
            "<tr>"
            f"<td>{icon} {html.escape(label)}</td>"
            f"<td>{_esc(res.title, 300)}</td>"
            f"<td>{note}</td>"
            "</tr>"
        )

    parts = [
        f"<div><b>{REPORT_PREFIX}</b> — {headline}",
        f" <span>({passed} đạt · {failed} không đạt · {blocked} chưa chạy được)</span>",
        "<table><tr><th>Kết quả</th><th>Test case</th><th>Ghi chú</th></tr>",
        "".join(rows),
        "</table>",
    ]
    if hidden:
        parts.append(f"<div><i>… và {hidden} case nữa (xem tab Tests).</i></div>")
    if failed:
        parts.append(
            "<div><i>Case không đạt cần được xử lý trước khi item này đi tiếp — "
            "sửa code hoặc sửa lại case nếu kỳ vọng đã đổi.</i></div>"
        )
    if dashboard_url:
        parts.append(
            f'<div>Theo dõi: <a href="{html.escape(dashboard_url)}">'
            f"{html.escape(dashboard_url)}</a></div>"
        )
    parts.append("</div>")

    return TestReport(
        html="".join(parts), total=len(items),
        passed=passed, failed=failed, blocked=blocked,
    )
