"""Compliance evidence pack: one document per period that an auditor can read cold.

ISO 27001 / SOC 2 / customer security reviews ask the same things for every release
window: what changed and through which reviewed PR, did the tests run, was security
scanned and did the gate pass, which known risks were accepted (by whom, why, until
when), what is the software bill of materials, and who changed the system's own
configuration. All of it already lives here — scattered over five pages. This gathers
it into one Markdown file, read-only, generated on demand.

Nothing is inferred. A section with no data says so in words ("không có lần quét nào
trong kỳ"), because in an audit an empty table and a missing control read the same.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


def _utc(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    return v if v.tzinfo else v.replace(tzinfo=UTC)


def _md(text: object) -> str:
    """One table cell: no pipes, no newlines."""
    return str(text or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _d(v: datetime | None) -> str:
    v = _utc(v)
    return v.strftime("%Y-%m-%d %H:%M") if v else "—"


@dataclass
class EvidenceInput:
    start: datetime
    end: datetime
    version: str = ""
    runs: list = field(default_factory=list)          # ExecutionRecord
    scans: list = field(default_factory=list)         # SecurityScan
    open_findings: list = field(default_factory=list)  # SecurityFinding, status open
    suppressed: list = field(default_factory=list)    # SecurityFinding, status suppressed
    audit: list = field(default_factory=list)         # AuditEvent
    flaky: list = field(default_factory=list)         # flaky_tests.FlakyTest
    sbom_files: list[Path] = field(default_factory=list)


def _in(window: EvidenceInput, when: datetime | None) -> bool:
    when = _utc(when)
    return when is not None and _utc(window.start) <= when <= _utc(window.end)  # type: ignore[operator]


def sbom_files(workspace: str) -> list[Path]:
    root = Path(workspace) / ".autopilot" / "sbom" if workspace else None
    if root is None or not root.is_dir():
        return []
    return sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]


def render(ev: EvidenceInput) -> str:
    out: list[str] = []
    w = out.append
    w(f"# Bộ bằng chứng tuân thủ — {_d(ev.start)[:10]} → {_d(ev.end)[:10]}")
    w("")
    w(f"- Sinh lúc: {_d(datetime.now(UTC))} UTC · AI-Autopilot {ev.version or '?'}")
    w("- Nguồn: lịch sử chạy, kết quả quét bảo mật, nhật ký thao tác và sổ test chập chờn "
      "lưu trên máy này. Chỉ đọc, không suy diễn.")
    w("")

    # 1. Changes
    runs = [r for r in ev.runs if _in(ev, r.completed_at or r.started_at)]
    with_pr = [r for r in runs if r.pr_url]
    w("## 1. Thay đổi đã giao")
    w("")
    w(f"{len(runs)} lần chạy trong kỳ, {len(with_pr)} lần mở PR.")
    w("")
    if with_pr:
        w("| Work item | Tiêu đề | Kết quả | PR | Test (đạt / tổng) | Xong lúc |")
        w("|---|---|---|---|---|---|")
        for r in sorted(with_pr, key=lambda r: _utc(r.completed_at or r.started_at) or ev.start):
            total = int(getattr(r, "tests_total", 0) or 0)
            failed = int(getattr(r, "tests_failed", 0) or 0)
            tests = f"{total - failed} / {total}" if total else "chưa chạy case QC"
            status = getattr(getattr(r, "status", ""), "value", getattr(r, "status", ""))
            w(f"| #{r.work_item_id} | {_md(r.title)} | {_md(status)} | {_md(r.pr_url)} | "
              f"{tests} | {_d(r.completed_at)} |")
    else:
        w("Không có PR nào được mở trong kỳ.")
    w("")

    # 2. Tests
    w("## 2. Kiểm thử")
    w("")
    qc = [r for r in runs if int(getattr(r, "tests_total", 0) or 0)]
    total = sum(int(r.tests_total or 0) for r in qc)
    failed = sum(int(r.tests_failed or 0) for r in qc)
    blocked = sum(int(getattr(r, "tests_blocked", 0) or 0) for r in qc)
    if qc:
        w(f"{len(qc)} lần chạy có thực thi test case: {total} case, {failed} không đạt, "
          f"{blocked} chưa chạy được.")
    else:
        w("Không có lần chạy nào thực thi test case trong kỳ.")
    flaky = [f for f in ev.flaky if _in(ev, f.last_seen)]
    w("")
    if flaky:
        w(f"Test chập chờn ghi nhận trong kỳ ({len(flaky)}):")
        w("")
        w("| Repo | Test | Số lần | Gần nhất |")
        w("|---|---|---|---|")
        for f in flaky[:30]:
            w(f"| {_md(f.repo)} | {_md(f.signature)} | {f.count} | {_d(f.last_seen)} |")
    else:
        w("Không ghi nhận test chập chờn nào trong kỳ.")
    w("")

    # 3. Security
    scans = [s for s in ev.scans if _in(ev, s.started_at)]
    w("## 3. Quét bảo mật")
    w("")
    if scans:
        passed = sum(1 for s in scans if s.gate_passed)
        w(f"{len(scans)} lần quét, cổng chặn đạt {passed} / {len(scans)}.")
        w("")
        w("| Lúc | Repo | Phạm vi | Công cụ | Critical | High | Medium | Cổng |")
        w("|---|---|---|---|---|---|---|---|")
        for s in sorted(scans, key=lambda s: _utc(s.started_at) or ev.start, reverse=True)[:40]:
            try:
                tools = ", ".join(sorted(json.loads(s.tools_json or "{}")))
            except (TypeError, ValueError):
                tools = ""
            w(f"| {_d(s.started_at)} | {_md(Path(s.repo).name or s.repo)} | {_md(s.scope)} | "
              f"{_md(tools)} | {s.critical_count} | {s.high_count} | {s.medium_count} | "
              f"{'✅ đạt' if s.gate_passed else '❌ chặn'} (ngưỡng {_md(s.fail_on)}) |")
    else:
        w("**Không có lần quét bảo mật nào trong kỳ.**")
    w("")
    counts = Counter(str(f.severity or "info").lower() for f in ev.open_findings)
    w("Lỗ hổng đang mở tại thời điểm sinh báo cáo: "
      + (", ".join(f"{s} {counts[s]}" for s in SEVERITY_ORDER if counts[s]) or "không có")
      + ".")
    w("")

    # 4. Accepted risk
    w("## 4. Rủi ro đã chấp nhận (suppression)")
    w("")
    if ev.suppressed:
        w("| Mức | Phát hiện | File | Lý do | Người duyệt | Hết hạn |")
        w("|---|---|---|---|---|---|")
        for f in ev.suppressed[:60]:
            w(f"| {_md(f.severity)} | {_md(f.title or f.rule_id)} | {_md(f.file)} | "
              f"{_md(f.suppress_reason) or '**(không ghi lý do)**'} | "
              f"{_md(f.suppressed_by) or '—'} | {_d(f.suppress_until)} |")
    else:
        w("Không có phát hiện nào đang được chấp nhận rủi ro.")
    w("")

    # 5. SBOM
    w("## 5. Danh mục thành phần phần mềm (SBOM)")
    w("")
    if ev.sbom_files:
        for p in ev.sbom_files:
            stamp = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
            w(f"- `{p.name}` — {_d(stamp)}")
    else:
        w("Chưa có SBOM nào trong `<workspace>/.autopilot/sbom/`. Tạo bằng "
          "`ai-autopilot scan --sbom -`.")
    w("")

    # 6. Audit
    audit = [a for a in ev.audit if _in(ev, a.at)]
    w("## 6. Thao tác trên hệ thống")
    w("")
    if audit:
        by_action = Counter(a.action for a in audit)
        w(f"{len(audit)} thao tác được ghi: "
          + ", ".join(f"{_md(k)} ×{n}" for k, n in by_action.most_common(12)) + ".")
        w("")
        config = [a for a in audit if str(a.action or "").startswith(("config", "settings"))]
        if config:
            w("Thay đổi cấu hình:")
            w("")
            w("| Lúc | Người | Hành động | Đối tượng |")
            w("|---|---|---|---|")
            for a in config[:50]:
                w(f"| {_d(a.at)} | {_md(a.actor)} | {_md(a.action)} | {_md(a.target)} |")
    else:
        w("Không có thao tác nào được ghi trong kỳ.")
    w("")
    return "\n".join(out)
