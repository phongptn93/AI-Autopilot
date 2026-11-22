"""The four DORA delivery metrics, from the deploy pipeline's own build history.

Lead time was the only one of the four the Delivery page had. The other three need to
know every deploy — the failed ones especially — and the state-sync only ever kept a
watermark of the newest *successful* build. So they are read where the truth already
lives: the deploy pipeline's completed builds on ADO, every result. That also means the
numbers reach back as far as the pipeline does, instead of starting the day a table was
added.

Definitions (stated on the page, because each has rival definitions):

* **Deploy frequency** — successful deploy builds per week in the window.
* **Change failure rate** — deploy builds that did not succeed (``failed`` or
  ``partiallySucceeded``) ÷ all completed deploy builds. Canceled builds are neither:
  someone stopped them, nothing was shipped or broken.
* **Time to restore** — from a deploy failing to the next successful deploy on the same
  pipeline+branch. p50 over the failures that have been restored; one still broken is
  reported separately as "đang hỏng", never folded into the median.

Pure functions; ``status="unknown"`` with a reason when the builds could not be read —
an unreachable pipeline is not "zero deploys".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

OK_RESULTS = {"succeeded"}
FAILED_RESULTS = {"failed", "partiallysucceeded"}


@dataclass(frozen=True)
class Deploy:
    build_id: int
    scope: str            # pipeline|branch — failures restore within their own scope
    result: str           # lowercased ADO result
    finished: datetime

    @property
    def ok(self) -> bool:
        return self.result in OK_RESULTS

    @property
    def failed(self) -> bool:
        return self.result in FAILED_RESULTS


@dataclass
class DoraReport:
    days: int
    status: str = "ok"                    # "ok" | "unknown"
    reason: str = ""
    deploys_ok: int = 0
    deploys_failed: int = 0
    per_week: float = 0.0
    change_failure_rate: float | None = None   # 0..1; None when nothing deployed
    restore_hours: list[float] = field(default_factory=list)
    broken_since: datetime | None = None       # oldest failure not yet restored
    lead_time_days: float | None = None        # p50, taken from the delivery report
    last_deploy: datetime | None = None

    @property
    def restore_p50_hours(self) -> float | None:
        return _median(self.restore_hours) if self.restore_hours else None

    @property
    def levels(self) -> dict[str, str]:
        """DORA performance band per metric — elite / high / medium / low (or "")."""
        out = {"frequency": "", "cfr": "", "restore": "", "lead": ""}
        if self.status != "ok":
            return out
        if self.deploys_ok or self.deploys_failed:
            f = self.per_week
            out["frequency"] = ("elite" if f >= 7 else "high" if f >= 1
                                else "medium" if f >= 0.25 else "low")
        if self.change_failure_rate is not None:
            r = self.change_failure_rate
            out["cfr"] = ("elite" if r <= 0.05 else "high" if r <= 0.10
                          else "medium" if r <= 0.15 else "low")
        if self.restore_p50_hours is not None:
            h = self.restore_p50_hours
            out["restore"] = ("elite" if h < 1 else "high" if h < 24
                              else "medium" if h < 24 * 7 else "low")
        if self.lead_time_days is not None:
            d = self.lead_time_days
            out["lead"] = ("elite" if d < 1 else "high" if d <= 7
                           else "medium" if d <= 30 else "low")
        return out


def _median(values: list[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def deploys_from_builds(builds: list[dict], scope: str) -> list[Deploy]:
    """ADO ``build/builds`` rows → deploys. Unfinished and canceled builds dropped."""
    out: list[Deploy] = []
    for b in builds:
        result = str(b.get("result") or "").lower()
        finished = _parse(b.get("finishTime"))
        bid = b.get("id")
        if not isinstance(bid, int) or finished is None:
            continue
        if result not in OK_RESULTS and result not in FAILED_RESULTS:
            continue
        out.append(Deploy(bid, scope, result, finished))
    return out


def compute(deploys: list[Deploy], *, days: int, now: datetime | None = None,
            lead_time_days: float | None = None) -> DoraReport:
    now = now or datetime.now(UTC)
    start = now - timedelta(days=days)
    report = DoraReport(days=days, lead_time_days=lead_time_days)
    unique = {(d.scope, d.build_id): d for d in deploys}
    ordered = sorted(unique.values(), key=lambda d: d.finished)
    in_window = [d for d in ordered if d.finished >= start]
    report.deploys_ok = sum(1 for d in in_window if d.ok)
    report.deploys_failed = sum(1 for d in in_window if d.failed)
    total = report.deploys_ok + report.deploys_failed
    report.per_week = round(report.deploys_ok / (days / 7), 1) if days else 0.0
    report.change_failure_rate = (report.deploys_failed / total) if total else None
    oks = [d for d in in_window if d.ok]
    report.last_deploy = oks[-1].finished if oks else None

    # Restore: walk each scope in time order; a failure opens an incident, the next
    # success on the same scope closes it. Consecutive failures belong to ONE incident.
    open_since: dict[str, datetime] = {}
    for d in ordered:
        if d.failed:
            open_since.setdefault(d.scope, d.finished)
        elif d.ok and d.scope in open_since:
            began = open_since.pop(d.scope)
            if began >= start:
                report.restore_hours.append(round((d.finished - began).total_seconds() / 3600, 2))
    if open_since:
        report.broken_since = min(open_since.values())
    return report


def unknown(days: int, reason: str, lead_time_days: float | None = None) -> DoraReport:
    return DoraReport(days=days, status="unknown", reason=reason, lead_time_days=lead_time_days)
