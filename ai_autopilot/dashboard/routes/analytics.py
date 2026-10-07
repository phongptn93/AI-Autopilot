"""Analytics and the delivery report."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

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
        north = await _north_star(c, records, days=days)
        return _TEMPLATES.TemplateResponse(
            request, "analytics.html",
            _ctx(request, "analytics", report=report, days=days, tag=tag, north=north),
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


async def _north_star(c: Container, records: list, *, days: int) -> list:
    """The ⭐ cards for the same period and scope as the rest of the page.

    Quality events carry no project, so they are narrowed to the items the scoped run
    list contains — otherwise a workspace filter would show another team's reviews.
    Every read is best-effort: a KPI that cannot be read shows "—", it does not take
    the page down with it.
    """
    from ai_autopilot.data.entities import PipelineState
    from ai_autopilot.north_star import compute_north_star

    items = {r.work_item_id for r in records}
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    quality: list = []
    held: set[int] = set()
    merged: set[int] | None = None
    with contextlib.suppress(Exception):
        quality = [q for q in await c.quality_events.recent(limit=5000, since=since)
                   if q.work_item_id in items]
    with contextlib.suppress(Exception):
        held = {s.work_item_id for s in await c.state_repo.all()
                if s.state == PipelineState.NEEDS_HUMAN}
    # Merges are only recorded when state sync is on; without it "merged" is unknowable
    # and the KPI falls back to "PR opened", saying so in its tooltip.
    if getattr(c.config, "auto_transition_enabled", False):
        with contextlib.suppress(Exception):
            merged = await c.sync_repo.seen_merged_prs()
    return compute_north_star(
        records, quality, needs_human_items=held & items, merged_pr_ids=merged,
    ).kpis()
