"""Analytics and the delivery report."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import delivery
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _ACTION_LABELS, _TEMPLATES, scope_of, work_item_link_base
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.services import delivery_report


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/analytics", response_class=HTMLResponse)
    async def analytics_page(request: Request, days: int = 30, tag: str = ""):
        """Exec / ROI dashboard: throughput, success/PR rate, cost per merged PR."""
        from ai_autopilot.analytics import compute_analytics

        c: Container = request.app.state.container
        days = max(1, min(days, 180))
        now = datetime.now()
        dfrom = (now - timedelta(days=days - 1)).date().isoformat()
        records, _ = await c.execution_repo.search(
            dfrom=dfrom, trigger_tag=tag or None,
            projects=scope_of(request, c.config)[1], limit=5000,
        )
        report = compute_analytics(records, days=days, now=now)
        return _TEMPLATES.TemplateResponse(
            request, "analytics.html",
            _ctx(request, "analytics", report=report, days=days, tag=tag),
        )

    @router.get("/delivery", response_class=HTMLResponse)
    async def delivery_page(request: Request, days: int = 0, project: str = "all"):
        """The PM view: throughput, lead time, what is stuck, who is loaded.

        Every other page here reports on the AUTOPILOT (runs, tokens, success rate).
        This one reports on the PROJECT, counting work items rather than runs and
        putting age — the thing a board cannot show — in front.

        Gathering lives in ``services.delivery_report`` because the Teams digest reads
        the same report: two gatherers meant the chat message and this page could
        disagree, and a number that changes depending on where you read it is worse
        than no number."""
        c: Container = request.app.state.container
        cfg = c.config
        _, in_scope = scope_of(request, cfg)
        available = in_scope if in_scope is not None else cfg.effective_ado_projects
        # The page's own project dropdown narrows WITHIN the selected workspace, so an
        # unknown/stale value must not widen the scope back out to every project.
        if project != "all":
            wanted = [p for p in available if p.lower() == project.lower()]
            projects = wanted if wanted else []
        else:
            projects = in_scope
        report, error = await delivery_report.gather(c, days=days, projects=projects)
        return _TEMPLATES.TemplateResponse(
            request, "delivery.html",
            _ctx(
                request, "delivery", report=report, days=report.window_days, error=error,
                projects=available, selected_project=project,
                item_link=work_item_link_base(cfg),
                recording=cfg.delivery_history_enabled,
                bands=delivery.FLOW_BANDS,
                kind_labels=_ACTION_LABELS,
            ),
        )

    return router
