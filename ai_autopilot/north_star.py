"""North-star KPIs — is the autopilot actually taking work off people's hands?

The Analytics page already answers "how much did it run" (runs, success rate, tokens).
Those are activity numbers: an autopilot that opens fifty PRs which each need three
rounds of human fixes scores beautifully on all of them. The numbers here ask the
outcome question instead — how much finished *without* a person stepping in, how fast
the first reviewable PR appears, how often it gives up and asks, and what reviewers
think of the result.

Pure on purpose (rows in → report out), like ``analytics.compute_analytics``.

Honesty rules, because a KPI wall is read by people who will not check the method:

* No data → ``None`` → the page shows "—", never 0 % (a zero is a claim).
* Each approximation is named in :attr:`Kpi.hint` so the tooltip says what the number
  really counts.
* What cannot be measured from stored data (human minutes per item) is returned as a
  KPI with no value and a hint saying what is missing — it is not invented.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ai_autopilot.run_timeline import HUMAN_FEEDBACK

# Kinds that mean "a person had to send it back" (QualityKind.REWORK). Repeated rather
# than imported from the entity module so the vocabulary this KPI depends on is visible
# in one place, next to the definition that uses it.
_REWORK_KINDS = frozenset({"execution_retry", "pr_revision", "sdlc_iteration", "reopened"})


@dataclass
class Kpi:
    key: str
    label: str
    icon: str
    value: str = "—"          # formatted for display; "—" when there is no data
    sub: str = ""             # the numerator/denominator or sample size
    tone: str = ""            # good | warn | bad | "" — colour only when there is data
    hint: str = ""            # tooltip: what this number really counts

    @property
    def has_data(self) -> bool:
        return self.value != "—"


@dataclass
class NorthStar:
    autonomous_items: int = 0
    finished_items: int = 0
    first_pr_seconds: list[float] = field(default_factory=list)
    needs_human_runs: int = 0
    finished_runs: int = 0
    votes_up: int = 0
    votes_total: int = 0
    merge_tracked: bool = False

    @property
    def autonomy_rate(self) -> int | None:
        if not self.finished_items:
            return None
        return round(100 * self.autonomous_items / self.finished_items)

    @property
    def median_first_pr_seconds(self) -> float | None:
        return statistics.median(self.first_pr_seconds) if self.first_pr_seconds else None

    @property
    def needs_human_rate(self) -> int | None:
        if not self.finished_runs:
            return None
        return round(100 * self.needs_human_runs / self.finished_runs)

    @property
    def approval_rate(self) -> int | None:
        return round(100 * self.votes_up / self.votes_total) if self.votes_total else None

    def kpis(self) -> list[Kpi]:
        return _kpis(self)


def _naive(at: datetime | None) -> datetime | None:
    if at is None:
        return None
    return at.astimezone(UTC).replace(tzinfo=None) if at.tzinfo else at


def _status(run) -> str:
    st = getattr(run, "status", "")
    return str(getattr(st, "value", st) or "")


def _pr_id(url: str | None) -> int:
    from ai_autopilot.services.pr_feedback import parse_pr_url

    parsed = parse_pr_url(url or "")
    return parsed[1] if parsed else 0


def compute_north_star(
    executions: Iterable,
    quality: Iterable = (),
    *,
    needs_human_items: Iterable[int] = (),
    merged_pr_ids: set[int] | None = None,
) -> NorthStar:
    """Aggregate one period's runs and quality events into the north-star numbers.

    ``needs_human_items``: items whose pipeline state is currently *Needs human*. The
    run row does not record an escalation (it is stored as a failure, or as a success
    the score gate then held), so the item's held state stands in for it: the LATEST
    finished run of a held item is counted as the one that asked for a person.

    ``merged_pr_ids``: PR ids known to have merged, when merge tracking is on
    (``auto_transition_enabled`` records them). ``None`` = not tracked → an opened PR is
    taken as the delivery, and the hint says so.
    """
    runs = list(executions)
    held = set(needs_human_items)
    rep = NorthStar(merge_tracked=merged_pr_ids is not None)

    by_item: dict[int, list] = {}
    for r in runs:
        by_item.setdefault(int(getattr(r, "work_item_id", 0) or 0), []).append(r)

    rework_items: set[int] = set()
    down_items: set[int] = set()
    for ev in quality:
        kind = getattr(ev, "kind", "") or ""
        wid = int(getattr(ev, "work_item_id", 0) or 0)
        if kind in _REWORK_KINDS:
            rework_items.add(wid)
        elif kind == HUMAN_FEEDBACK:
            rep.votes_total += 1
            if (getattr(ev, "value", 0) or 0) > 0:
                rep.votes_up += 1
            else:
                down_items.add(wid)

    for wid, item_runs in by_item.items():
        finished = [r for r in item_runs if getattr(r, "completed_at", None) is not None]
        rep.finished_runs += len(finished)
        if not finished:
            continue
        rep.finished_items += 1
        latest = max(finished, key=lambda r: _naive(r.completed_at))
        if wid in held:
            rep.needs_human_runs += 1

        # Time to first PR: the item's first start → the first finish that had a PR.
        starts = [_naive(r.started_at) for r in item_runs if getattr(r, "started_at", None)]
        with_pr = [_naive(r.completed_at) for r in finished if getattr(r, "pr_url", None)]
        if starts and with_pr:
            gap = (min(with_pr) - min(starts)).total_seconds()
            if gap >= 0:
                rep.first_pr_seconds.append(gap)

        delivered = [r for r in finished if _status(r) == "Success" and getattr(r, "pr_url", None)]
        if merged_pr_ids is not None:
            delivered = [r for r in delivered if _pr_id(r.pr_url) in merged_pr_ids]
        clean = (
            bool(delivered)
            and all(_status(r) == "Success" for r in finished)
            and not any(int(getattr(r, "retry_count", 0) or 0) for r in item_runs)
            and wid not in rework_items
            and wid not in down_items
            and wid not in held
            and _status(latest) == "Success"
        )
        if clean:
            rep.autonomous_items += 1
    return rep


def _fmt_span(seconds: float) -> str:
    s = int(round(seconds))
    if s < 3600:
        return f"{max(1, s // 60)} phút"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} giờ {m:02d}" if m else f"{h} giờ"
    d, rem = divmod(s, 86400)
    return f"{d} ngày {rem // 3600} giờ"


def _tone(value: float, good: float, warn: float, *, higher_is_better: bool = True) -> str:
    if higher_is_better:
        return "good" if value >= good else ("warn" if value >= warn else "bad")
    return "good" if value <= good else ("warn" if value <= warn else "bad")


def _kpis(ns: NorthStar) -> list[Kpi]:
    delivered = "PR đã merge" if ns.merge_tracked else "đã mở PR (chưa theo dõi merge)"
    auto = Kpi(
        "autonomy", "Hoàn thành tự hành", "🤖",
        hint=(
            f"Item có run thành công và {delivered}, không retry, không bị yêu cầu sửa PR "
            "(/ai revise), không quay vòng SDLC, không bị kéo lại, không 👎 và không bị "
            "giữ chờ người. Xấp xỉ: chưa đo được việc người sửa code trực tiếp trên nhánh."
        ),
    )
    if ns.autonomy_rate is not None:
        auto.value = f"{ns.autonomy_rate}%"
        auto.sub = f"{ns.autonomous_items}/{ns.finished_items} item"
        auto.tone = _tone(ns.autonomy_rate, 60, 30)

    ttfpr = Kpi(
        "first_pr", "Thời gian tới PR đầu tiên", "⏱️",
        hint="Trung vị, theo item: từ lúc run đầu tiên bắt đầu tới khi run đầu tiên có PR "
             "hoàn tất. Gồm cả thời gian chờ giữa các lần chạy lại.",
    )
    if ns.median_first_pr_seconds is not None:
        ttfpr.value = _fmt_span(ns.median_first_pr_seconds)
        ttfpr.sub = f"trung vị của {len(ns.first_pr_seconds)} item"
        ttfpr.tone = _tone(ns.median_first_pr_seconds, 3600, 4 * 3600, higher_is_better=False)

    human = Kpi(
        "needs_human", "Tỉ lệ cần người", "🙋",
        hint="Run gần nhất của item đang ở trạng thái Needs human / số run đã kết thúc. "
             "Xấp xỉ: run không lưu cờ needs_human, nên chỉ đếm được item hiện vẫn đang "
             "bị giữ — item đã được người gỡ giữ thì không còn tính.",
    )
    if ns.needs_human_rate is not None:
        human.value = f"{ns.needs_human_rate}%"
        human.sub = f"{ns.needs_human_runs}/{ns.finished_runs} run"
        human.tone = _tone(ns.needs_human_rate, 10, 25, higher_is_better=False)

    votes = Kpi(
        "feedback", "Phản hồi người review", "👍",
        hint="Tỉ lệ 👍 trong các đánh giá 1 chạm (nút trên thông báo hoàn tất / tab "
             "Diễn biến). Mỗi run một phiếu, phiếu sau thay phiếu trước.",
    )
    if ns.approval_rate is not None:
        votes.value = f"{ns.approval_rate}%"
        votes.sub = f"{ns.votes_up} 👍 / {ns.votes_total} đánh giá"
        votes.tone = _tone(ns.approval_rate, 80, 50)

    minutes = Kpi(
        "human_minutes", "Phút công người / item", "⏳",
        hint="Chưa đo được: hệ thống không ghi thời gian người review, sửa PR hay trả "
             "lời câu hỏi của agent. Cần thời gian review trên PR (ADO) hoặc log thao "
             "tác của người — không ước đoán thay.",
    )
    return [auto, ttfpr, human, votes, minutes]
