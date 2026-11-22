"""QC defect page — escape rate, reopen rate, defect age, time to close, by module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import defect_metrics
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES, work_item_link_base
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.services.process_health_service import to_health_item

WINDOWS = (14, 30, 90)
# Open defects are found through "changed recently"; reach back further than the
# window so a bug nobody touched for a while still shows up as old.
_OPEN_LOOKBACK_DAYS = 180
_log = get_logger("dashboard.defects")


async def gather(c: Container, project: str, days: int, now: datetime):
    """``(report, error)`` for one project. Never raises."""
    try:
        raw = await c.ado.get_raw_work_items(project, max(days, _OPEN_LOOKBACK_DAYS))
        categories = await c.ado.get_state_categories()
    except Exception as exc:  # noqa: BLE001
        return None, f"Không đọc được work item từ ADO: {describe_exc(exc)}"
    items = [i for i in (to_health_item(f) for f in raw) if i is not None]
    changes, since = None, None
    history = getattr(c, "state_history", None)
    if history is not None:
        try:
            changes = await history.changes_since(now - timedelta(days=days))
            since = await history.first_seen()
        except Exception as exc:  # noqa: BLE001
            _log.warning("defects: state history failed", error=describe_exc(exc))
    if changes is not None:
        changes = [ch for ch in changes if (ch.project or "").lower() == project.lower()]
    report = defect_metrics.compute(items, categories=categories, days=days,
                                    changes=changes, history_since=since, now=now)
    return report, None


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/defects", response_class=HTMLResponse)
    async def defects_page(request: Request, project: str = "", days: int = 30):
        c: Container = request.app.state.container
        cfg = c.config
        projects = list(cfg.effective_ado_projects or [])
        chosen = project if project in projects else (projects[0] if projects else "")
        days = days if days in WINDOWS else 30
        report, error = (None, None)
        if chosen:
            report, error = await gather(c, chosen, days, datetime.now(UTC))
        return _TEMPLATES.TemplateResponse(
            request, "defects.html",
            _ctx(request, "defects", projects=projects, project=chosen, days=days,
                 windows=WINDOWS, report=report, error=error,
                 item_link=work_item_link_base(cfg), lookback=_OPEN_LOOKBACK_DAYS),
        )

    return router
