"""Periodic process-health digest: the recurring reviews, computed instead of chased.

A process doc typically assigns standing duties — check the ad-hoc ratio each sprint,
read escaped defects per module each month, prune the tag catalogue each quarter. They
are exactly the duties that lapse first, because each one is a manual query nobody owns
on a busy week. This service runs them on a timer and pushes the findings to wherever
the team already reads notifications.

It only ever READS. Everything it surfaces (a stuck item, a routing tag left on a closed
ticket, a tag that drifted case) is a judgement call about how people are working, and
the doc assigns those calls to a person. Silently "fixing" them would also erase the
evidence that the process needs attention.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger
from ai_autopilot.process_health import (
    HealthItem,
    HealthReport,
    build_report,
    render_text,
)

# ADO reference names for the optional fields. Absent from a project's process → the
# metric that reads them simply reports nothing, rather than a misleading zero.
_F_BLOCKED = "Microsoft.VSTS.CMMI.Blocked"
_F_FOUND_IN_ENV = "Microsoft.VSTS.CMMI.FoundInEnvironment"


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _as_bool(value) -> bool | None:
    """ADO returns Blocked as the string "Yes"/"No" on CMMI, bool elsewhere, and omits
    it entirely when the process has no such field — which must stay distinguishable
    from "not blocked"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip().lower() in ("yes", "true", "1")
    return None


def to_health_item(fields: dict) -> HealthItem | None:
    """One raw ADO ``fields`` dict → the reduced snapshot the metrics read."""
    ident = fields.get("System.Id")
    if not isinstance(ident, int):
        return None
    assigned = fields.get("System.AssignedTo")
    tags = [t.strip() for t in str(fields.get("System.Tags") or "").split(";") if t.strip()]
    return HealthItem(
        id=ident,
        work_item_type=str(fields.get("System.WorkItemType") or ""),
        state=str(fields.get("System.State") or ""),
        title=str(fields.get("System.Title") or ""),
        area_path=str(fields.get("System.AreaPath") or ""),
        tags=tuple(tags),
        changed=_parse_dt(fields.get("System.ChangedDate")),
        created=_parse_dt(fields.get("System.CreatedDate")),
        blocked=_as_bool(fields.get(_F_BLOCKED)),
        found_in_environment=str(fields.get(_F_FOUND_IN_ENV) or ""),
        assigned_to=(assigned or {}).get("displayName", "") if isinstance(assigned, dict) else "",
    )


class ProcessHealthService:
    """Computes a health report per polled project and broadcasts the findings."""

    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.process_health")
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._config.process_health_enabled:
            return
        if self._config.process_health_interval_hours <= 0:
            self._log.info("process health: interval is 0 — report only on demand")
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        cfg = self._config
        self._log.info(
            "process-health digest started",
            every_hours=cfg.process_health_interval_hours,
            window_days=cfg.process_health_window_days,
        )
        while True:
            try:
                # Sleep FIRST: a restart must not fire a digest, or a day of restarts
                # becomes a day of duplicate messages.
                await asyncio.sleep(cfg.process_health_interval_hours * 3600)
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a digest must never kill the app
                self._log.error("process-health cycle failed", error=str(exc))

    async def reports(self) -> list[HealthReport]:
        """One report per polled project (also used by the dashboard / on demand)."""
        cfg = self._config
        out: list[HealthReport] = []
        for project in cfg.effective_ado_projects:
            scoped = cfg.scoped_for_project(project)
            try:
                raw = await self._c.ado.get_raw_work_items(
                    project, cfg.process_health_window_days
                )
            except Exception as exc:  # noqa: BLE001
                self._log.warning("process-health fetch failed", project=project, error=str(exc))
                continue
            items = [i for i in (to_health_item(f) for f in raw) if i is not None]
            out.append(build_report(
                items,
                project=project,
                window_days=cfg.process_health_window_days,
                done_states=scoped.done_states,
                ad_hoc_threshold=cfg.process_health_adhoc_threshold_pct,
                blocked_days=cfg.process_health_blocked_days,
                classification_tags=tuple(cfg.process_health_classification_tags),
                ad_hoc_tags=tuple(cfg.process_health_adhoc_tags),
                routing_tags=tuple(cfg.process_health_routing_tags),
                now=datetime.now(UTC),
            ))
        return out

    async def run_once(self) -> list[HealthReport]:
        reports = await self.reports()
        for report in reports:
            text = render_text(report)
            self._log.info(
                "process health",
                project=report.project, items=report.items,
                ad_hoc_pct=report.ad_hoc.percent, escaped=sum(report.escaped.values()),
                blocked=len(report.blocked), findings=report.has_findings,
            )
            # A clean report is worth computing (it is the evidence the process holds)
            # but not worth a notification — a digest that pings every day saying
            # "nothing" is a digest people mute, and then the real one is muted too.
            if report.has_findings and not self._config.dry_run:
                await self._broadcast(report, text)
        return reports

    async def _broadcast(self, report: HealthReport, text: str) -> None:
        await self._c.notifier.broadcast_digest(
            f"📋 Sức khoẻ quy trình — {report.project}", text
        )
