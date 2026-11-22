"""Sprint page — the default team's current iteration: committed, done, on track?"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import sprint as sprint_mod
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES, work_item_link_base
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.logging_config import describe_exc, get_logger

_log = get_logger("dashboard.sprint")


async def gather(c: Container, project: str, now: datetime) -> sprint_mod.SprintReport:
    try:
        iteration = await c.ado.get_current_iteration(project)
        raw = None
        if iteration:
            raw = await c.ado.get_iteration_items(project, iteration.get("path", ""))
        elif iteration == {}:
            raw = []
        categories = await c.ado.get_state_categories()
    except Exception as exc:  # noqa: BLE001
        _log.warning("sprint: read failed", error=describe_exc(exc))
        return sprint_mod.SprintReport(status="unknown",
                                       reason=f"Không đọc được sprint: {describe_exc(exc)}")
    ai_ids: set[int] = set()
    repo = getattr(c, "execution_repo", None)
    if repo is not None:
        try:
            ai_ids = await repo.work_item_ids()
        except Exception:  # noqa: BLE001
            ai_ids = set()
    return sprint_mod.compute(iteration, raw, categories=categories, ai_item_ids=ai_ids, now=now)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/sprint", response_class=HTMLResponse)
    async def sprint_page(request: Request, project: str = ""):
        c: Container = request.app.state.container
        projects = list(c.config.effective_ado_projects or [])
        chosen = project if project in projects else (projects[0] if projects else "")
        report = await gather(c, chosen, datetime.now(UTC)) if chosen else None
        return _TEMPLATES.TemplateResponse(
            request, "sprint.html",
            _ctx(request, "sprint", projects=projects, project=chosen, report=report,
                 item_link=work_item_link_base(c.config),
                 margin=sprint_mod.BEHIND_MARGIN_PCT),
        )

    return router
