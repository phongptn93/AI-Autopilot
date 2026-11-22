"""Gathers the team pulse from the role pages' own gatherers (see ``team_pulse``).

Every role is read concurrently and independently: one unreachable source turns ITS
role ``unknown`` and leaves the others alone.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ai_autopilot import flaky_tests, team_pulse
from ai_autopilot.logging_config import describe_exc, get_logger

_log = get_logger("services.team_pulse")


async def _safe(coro, what: str):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 — one role must not blank the others
        _log.warning("team pulse: source failed", source=what, error=describe_exc(exc))
        return None


async def gather(c, now: datetime | None = None) -> list[team_pulse.RoleSignal]:
    # Imported here: these live with their pages, and the pages import services.
    from ai_autopilot.dashboard.routes import ba, ci, defects, environments, sprint
    from ai_autopilot.services import delivery_report

    now = now or datetime.now(UTC)
    cfg = c.config
    projects = list(cfg.effective_ado_projects or [])
    project = projects[0] if projects else ""
    if not (cfg.ado_organization and cfg.ado_pat and project):
        reason = "chưa kết nối Azure DevOps"
        return [team_pulse.RoleSignal(r, i, h, reason=reason) for r, i, h in (
            ("BA", "🗒️", "/dashboard/ba?tab=changes"), ("PM", "🏃", "/dashboard/sprint"),
            ("Dev", "🧑‍💻", "/dashboard/ci"), ("QC", "🐞", "/dashboard/defects"),
            ("DevSecOps", "🚀", "/dashboard/environments"))]

    async def ba_report():
        rows = await ba._spec_rows(c)
        return (await ba._changes(c, rows))["report"]

    async def qc_report():
        report, _error = await defects.gather(c, project, 7, now)
        return report

    async def incidents():
        report, _error = await environments.gather_incidents(c, project, 30, now)
        return report

    cr, sp, cib, qc, dora, inc = await asyncio.gather(
        _safe(ba_report(), "ba"),
        _safe(sprint.gather(c, project, now), "sprint"),
        _safe(ci.gather(c.ado, 1, now), "ci"),
        _safe(qc_report(), "defects"),
        _safe(delivery_report.gather_dora(c, days=14, now=now), "dora"),
        _safe(incidents(), "incidents"),
    )
    flaky = (len(flaky_tests.load(cfg.workspace_directory or ""))
             if cfg.test_gate_enabled and cfg.test_gate_flaky_rerun else None)
    return team_pulse.sort_for_attention([
        team_pulse.ba_signal(cr), team_pulse.pm_signal(sp),
        team_pulse.dev_signal(cib, flaky), team_pulse.qc_signal(qc),
        team_pulse.ops_signal(dora, inc),
    ])
