"""Team pulse: one line per role, from the role pages' own numbers.

Each role has its desk now — Sprint, the BA desk, code health, defects, CI/CD,
environments, DORA — and nobody opens seven pages before the stand-up. The pulse is the
first glance: per role, a tone (good / warn / bad / unknown), the two or three numbers
that decide it, the next action, and the page that has the detail.

Built from the SAME report objects those pages render, so the morning brief, the team
page and the role pages can never disagree. A source that could not be read is
``unknown`` with the reason — never a green tile.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GOOD, WARN, BAD, UNKNOWN = "good", "warn", "bad", "unknown"
_RANK = {BAD: 0, WARN: 1, UNKNOWN: 2, GOOD: 3}


@dataclass
class RoleSignal:
    role: str
    icon: str
    href: str
    tone: str = UNKNOWN
    facts: list[tuple[str, str]] = field(default_factory=list)   # (label, value)
    action: str = ""
    reason: str = ""          # why it is unknown

    @property
    def summary(self) -> str:
        if self.tone == UNKNOWN:
            return f"chưa đo được — {self.reason}" if self.reason else "chưa đo được"
        text = " · ".join(f"{label} {value}" for label, value in self.facts)
        return f"{text}. ➡️ {self.action}" if self.action else text


def _worst(*tones: str) -> str:
    known = [t for t in tones if t != UNKNOWN]
    return min(known, key=lambda t: _RANK[t]) if known else UNKNOWN


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{round(v * 100)}%"


def ba_signal(cr_report) -> RoleSignal:
    s = RoleSignal("BA", "🗒️", "/dashboard/ba?tab=changes")
    if cr_report is None:
        s.reason = "không đọc được change request"
        return s
    silent, open_cr = len(cr_report.silent), len(cr_report.open)
    s.facts = [("spec đổi sau khi khách xác nhận, chưa có CR:", str(silent)),
               ("CR đang mở:", str(open_cr))]
    s.tone = BAD if silent else (WARN if open_cr >= 5 else GOOD)
    if silent:
        s.action = "tạo CR hoặc xin khách xác nhận lại các spec đó"
    elif open_cr:
        s.action = "chốt các CR đang mở"
    return s


def pm_signal(sprint) -> RoleSignal:
    s = RoleSignal("PM", "🏃", "/dashboard/sprint")
    if sprint is None or sprint.status == "unknown":
        s.reason = getattr(sprint, "reason", "") or "không đọc được sprint"
        return s
    if sprint.status == "none":
        s.tone, s.facts = WARN, [("sprint:", "không có sprint nào đang diễn ra")]
        s.action = "đặt ngày cho sprint hiện tại trên ADO"
        return s
    verdict = {"ahead": "nhanh hơn lịch", "on_track": "đúng tiến độ",
               "behind": "chậm hơn lịch"}.get(sprint.verdict, "")
    done = "—" if sprint.done_pct is None else f"{sprint.done_pct:g}"
    elapsed = "—" if sprint.elapsed_pct is None else f"{sprint.elapsed_pct:g}"
    s.facts = [(f"{sprint.name}:", f"xong {done}% / thời gian {elapsed}%"
                + (f" — {verdict}" if verdict else ""))]
    if sprint.at_risk:
        s.facts.append(("chưa bắt đầu:", str(len(sprint.at_risk))))
    s.tone = BAD if sprint.verdict == "behind" else (WARN if sprint.at_risk else GOOD)
    if sprint.verdict == "behind" or sprint.at_risk:
        s.action = "rà các việc chưa bắt đầu, cân nhắc bỏ bớt khỏi sprint"
    return s


def dev_signal(ci, flaky_count: int | None) -> RoleSignal:
    s = RoleSignal("Dev", "🧑‍💻", "/dashboard/ci")
    if ci is None or ci.status != "ok":
        s.reason = getattr(ci, "reason", "") or "không đọc được build"
        return s
    code = [f for f in ci.failed if f.category in ("build", "test") and not f.flaky]
    s.facts = [("build đỏ do code:", str(len(code)))]
    if flaky_count is not None:
        s.facts.append(("test chập chờn:", str(flaky_count)))
    s.tone = BAD if len(code) >= 3 else (WARN if code or (flaky_count or 0) >= 5 else GOOD)
    if code:
        s.action = f"sửa build #{code[0].number or code[0].build_id} ({code[0].pipeline})"
    return s


def qc_signal(defects) -> RoleSignal:
    s = RoleSignal("QC", "🐞", "/dashboard/defects")
    if defects is None:
        s.reason = "không đọc được lỗi"
        return s
    old = [o for o in defects.oldest_open if o.age_days >= 30]
    s.facts = [("lỗi mở:", str(defects.open_count)), ("lọt UAT/prod:", _pct(defects.escape_rate)),
               ("mở lại:", _pct(defects.reopen_rate))]
    rate = defects.escape_rate or 0
    s.tone = BAD if rate >= 0.3 else (WARN if rate >= 0.15 or old else GOOD)
    if old:
        s.action = f"{len(old)} lỗi mở quá 30 ngày cần quyết"
    return s


def ops_signal(dora, incidents) -> RoleSignal:
    s = RoleSignal("DevSecOps", "🚀", "/dashboard/environments")
    tones: list[str] = []
    if dora is not None and dora.status == "ok":
        s.facts.append(("deploy:", f"{dora.per_week:g}/tuần"))
        s.facts.append(("deploy lỗi:", _pct(dora.change_failure_rate)))
        if dora.broken_since is not None:
            s.facts.append(("pipeline:", "đang hỏng"))
            tones.append(BAD)
            s.action = "khôi phục pipeline deploy"
        else:
            cfr = dora.change_failure_rate or 0
            tones.append(WARN if cfr > 0.15 else GOOD)
    if incidents is not None:
        s.facts.append(("sự cố prod mở:", str(len(incidents.open))))
        tones.append(BAD if incidents.open else GOOD)
        if incidents.open and not s.action:
            s.action = f"xử lý sự cố #{incidents.open[0].id}"
    s.tone = _worst(*tones)
    if s.tone == UNKNOWN:
        s.reason = "không đọc được deploy và sự cố"
    return s


def sort_for_attention(signals: list[RoleSignal]) -> list[RoleSignal]:
    """Worst first — the brief is read top-down and may be read only half way."""
    return sorted(signals, key=lambda s: _RANK[s.tone])
