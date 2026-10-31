"""BA/PM metrics — are the specs good enough to loosen the process, or must it tighten?

north_star answers "is the autopilot taking work off people's hands". The numbers here
answer the question a BA or PM asks one level up: how good are the specs we hand to the
autopilot, how quickly are spec-drift points decided, and how often does the customer's
confirmation go stale because the spec moved afterwards.

Pure on purpose (plain rows in, report out; the dashboard gathers the rows and passes
``now``), like ``north_star`` and ``process_health``.

Honesty rules, copied from north_star because this wall is read by people who will not
check the method:

* No data -> ``None`` -> the page shows "—", never 0 % (a zero is a claim).
* Rates over a different population than their neighbours say so in :attr:`Kpi.hint`
  (e.g. ``update_spec_rate`` counts only decided drift points).
* Percentages that combine many specs are built from sums, not a mean of per-spec
  ratios, so a spec with 1 criterion does not weigh as much as one with 30.
* Bad rows are skipped or treated as 0; a malformed row must never break the dashboard.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Drift kinds that mean "the spec itself was unclear or incomplete", as opposed to
# "the code diverged from a clear spec".
SPEC_FAULT_KINDS = frozenset({"spec_unclear", "spec_gap", "assumption"})

_WORST_LIMIT = 5

# Fixed tone thresholds. Only applied when there is data.
_SCORE_GOOD, _SCORE_WARN = 80.0, 60.0
_READY_GOOD, _READY_WARN = 70.0, 40.0
_VERIFIABLE_GOOD, _VERIFIABLE_WARN = 90.0, 70.0
_DECIDE_GOOD_DAYS, _DECIDE_WARN_DAYS = 2.0, 5.0
_SPEC_FAULT_GOOD, _SPEC_FAULT_WARN = 0.25, 0.5
_STALE_BAD = 0.25


@dataclass
class SpecQuality:
    count: int = 0
    avg_score: float | None = None
    ready_pct: float | None = None
    verifiable_pct: float | None = None
    worst: list[dict[str, object]] = field(default_factory=list)


@dataclass
class DriftMetrics:
    total: int = 0
    open: int = 0
    overdue: int = 0
    decided: int = 0
    median_days_to_decide: float | None = None
    by_kind: dict[str, int] = field(default_factory=dict)
    spec_fault_rate: float | None = None
    update_spec_rate: float | None = None


@dataclass
class ConfirmationMetrics:
    total: int = 0
    spec_confirmations: int = 0
    acceptances: int = 0
    stale: int = 0
    stale_rate: float | None = None


@dataclass
class Kpi:
    key: str
    label: str
    icon: str
    value: str = "—"  # formatted for display; "—" when there is no data
    sub: str = ""
    tone: str = ""  # good | warn | bad | "" — colour only when there is data
    hint: str = ""  # tooltip: what this number really counts

    @property
    def has_data(self) -> bool:
        return self.value != "—"


# ── helpers ───────────────────────────────────────────────────────────────────


def _int(value: object) -> int:
    """Coerce a loosely-typed cell to int; anything unusable counts as 0."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value else 0  # NaN guard
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def _str(value: object) -> str:
    return "" if value is None else str(value)


def _aware(at: object) -> datetime | None:
    """A datetime in UTC; naive values are taken as UTC. Non-datetimes give None."""
    if not isinstance(at, datetime):
        return None
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def _round1(x: float) -> float:
    return round(x, 1)


# ── spec quality ──────────────────────────────────────────────────────────────


@dataclass
class _Spec:
    rel: str
    title: str
    score: int
    verdict: str
    ac: int
    ac_ok: int


def spec_quality(rows: Sequence[Mapping[str, object]]) -> SpecQuality:
    """Score distribution of the specs: average, share ready, share of verifiable AC."""
    specs = [
        _Spec(
            rel=_str(r.get("rel")),
            title=_str(r.get("title")),
            score=_int(r.get("score")),
            verdict=_str(r.get("verdict")),
            ac=max(0, _int(r.get("ac"))),
            ac_ok=max(0, _int(r.get("ac_verifiable"))),
        )
        for r in rows
    ]
    n = len(specs)
    if n == 0:
        return SpecQuality()
    ac_total = sum(s.ac for s in specs)
    ac_ok = sum(min(s.ac_ok, s.ac) for s in specs)
    ready = sum(1 for s in specs if s.verdict == "ready")
    # Stable sort: ties keep input order, so the list is deterministic.
    lowest = sorted(specs, key=lambda s: s.score)[:_WORST_LIMIT]
    return SpecQuality(
        count=n,
        avg_score=_round1(sum(s.score for s in specs) / n),
        ready_pct=_round1(100 * ready / n),
        verifiable_pct=_round1(100 * ac_ok / ac_total) if ac_total else None,
        worst=[
            {"rel": s.rel, "title": s.title, "score": s.score, "verdict": s.verdict}
            for s in lowest
        ],
    )


# ── drift ─────────────────────────────────────────────────────────────────────


def drift_metrics(
    rows: Sequence[Mapping[str, object]], *, sla_days: int, now: datetime
) -> DriftMetrics:
    """Backlog and decision speed of spec-drift points. Rows without created_at are skipped."""
    now_utc = _aware(now) or datetime.now(UTC)
    total = open_ = overdue = decided = update_spec = spec_fault = 0
    spans: list[float] = []
    kinds: Counter[str] = Counter()
    for r in rows:
        created = _aware(r.get("created_at"))
        if created is None:
            continue
        total += 1
        kind = _str(r.get("kind"))
        kinds[kind] += 1
        if kind in SPEC_FAULT_KINDS:
            spec_fault += 1
        resolved = _aware(r.get("resolved_at"))
        if resolved is None:
            open_ += 1
            if sla_days > 0 and (now_utc - created).days >= sla_days:
                overdue += 1
            continue
        decided += 1
        spans.append(max(0.0, (resolved - created).total_seconds() / 86400))
        if _str(r.get("decision")) == "update_spec":
            update_spec += 1
    return DriftMetrics(
        total=total,
        open=open_,
        overdue=overdue,
        decided=decided,
        median_days_to_decide=_round1(statistics.median(spans)) if spans else None,
        by_kind=dict(sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))),
        spec_fault_rate=spec_fault / total if total else None,
        update_spec_rate=update_spec / decided if decided else None,
    )


# ── confirmations ─────────────────────────────────────────────────────────────


def confirmation_metrics(rows: Sequence[Mapping[str, object]]) -> ConfirmationMetrics:
    """How often a customer's spec confirmation was overtaken by a later spec change."""
    spec = acc = stale = 0
    for r in rows:
        kind = _str(r.get("kind"))
        if kind == "spec_confirm":
            spec += 1
            if r.get("stale"):
                stale += 1
        elif kind == "acceptance":
            acc += 1
    return ConfirmationMetrics(
        total=spec + acc,
        spec_confirmations=spec,
        acceptances=acc,
        stale=stale,
        stale_rate=stale / spec if spec else None,
    )


# ── KPI tiles ─────────────────────────────────────────────────────────────────


def _tone(value: float, good: float, warn: float, *, higher_is_better: bool = True) -> str:
    if higher_is_better:
        return "good" if value >= good else ("warn" if value >= warn else "bad")
    return "good" if value <= good else ("warn" if value <= warn else "bad")


def _pct(value: float) -> str:
    return f"{round(value)}%"


def kpis(q: SpecQuality, d: DriftMetrics, c: ConfirmationMetrics) -> list[Kpi]:
    """Seven tiles. Value "—" and no tone whenever the number has no data behind it."""
    avg = Kpi(
        "spec_score", "Điểm sẵn sàng của spec", "📐",
        hint="Trung bình điểm readiness của các spec đã chấm (0–100). "
             "Mỗi spec một phiếu, không có trọng số theo độ dài.",
    )
    if q.avg_score is not None:
        avg.value = f"{q.avg_score:g}"
        avg.sub = f"{q.count} spec"
        avg.tone = _tone(q.avg_score, _SCORE_GOOD, _SCORE_WARN)

    ready = Kpi(
        "spec_ready", "Spec đạt chuẩn", "✅",
        hint="Số spec có verdict = ready / tổng số spec đã chấm.",
    )
    if q.ready_pct is not None:
        ready.value = _pct(q.ready_pct)
        ready.sub = f"{q.count} spec"
        ready.tone = _tone(q.ready_pct, _READY_GOOD, _READY_WARN)

    ver = Kpi(
        "ac_verifiable", "AC kiểm chứng được", "🧪",
        hint="Tổng AC kiểm chứng được / tổng AC của mọi spec (cộng gộp, không lấy trung "
             "bình tỉ lệ từng spec). Không có AC nào thì không có số.",
    )
    if q.verifiable_pct is not None:
        ver.value = _pct(q.verifiable_pct)
        ver.tone = _tone(q.verifiable_pct, _VERIFIABLE_GOOD, _VERIFIABLE_WARN)

    drift = Kpi(
        "drift_open", "Điểm lệch spec đang mở", "🧭",
        hint="Số điểm lệch spec chưa có quyết định (resolved_at trống). Quá hạn = đã mở "
             "từ số ngày SLA trở lên.",
    )
    if d.total:
        drift.value = str(d.open)
        drift.sub = f"{d.overdue} quá hạn / {d.total} điểm"
        drift.tone = "bad" if d.overdue > 0 else ("warn" if d.open > 0 else "good")

    med = Kpi(
        "drift_days", "Ngày chốt điểm lệch", "⏱️",
        hint="Trung vị số ngày từ lúc ghi nhận tới lúc có quyết định, chỉ tính các điểm "
             "lệch đã được quyết định.",
    )
    if d.median_days_to_decide is not None:
        med.value = f"{d.median_days_to_decide:g} ngày"
        med.sub = f"{d.decided} điểm đã chốt"
        med.tone = _tone(
            d.median_days_to_decide, _DECIDE_GOOD_DAYS, _DECIDE_WARN_DAYS, higher_is_better=False
        )

    fault = Kpi(
        "spec_fault", "Lệch do spec chưa rõ", "📝",
        hint="Tỉ lệ điểm lệch có loại spec_unclear / spec_gap / assumption (spec thiếu hoặc "
             "mơ hồ) trên tổng điểm lệch; phần còn lại là code đi lệch spec.",
    )
    if d.spec_fault_rate is not None:
        fault.value = _pct(100 * d.spec_fault_rate)
        fault.sub = f"{d.total} điểm lệch"
        fault.tone = _tone(
            d.spec_fault_rate, _SPEC_FAULT_GOOD, _SPEC_FAULT_WARN, higher_is_better=False
        )

    stale = Kpi(
        "stale_confirm", "Xác nhận khách bị cũ", "⌛",
        hint="Số lần khách xác nhận spec mà spec đã đổi sau đó / tổng lần xác nhận spec. "
             "Không tính các lần nghiệm thu (acceptance).",
    )
    if c.stale_rate is not None:
        stale.value = _pct(100 * c.stale_rate)
        stale.sub = f"{c.stale}/{c.spec_confirmations} xác nhận"
        stale.tone = "bad" if c.stale_rate > _STALE_BAD else ("warn" if c.stale > 0 else "good")

    return [avg, ready, ver, drift, med, fault, stale]
