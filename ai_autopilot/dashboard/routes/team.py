"""Team page — one card per role, worst first, linking to that role's desk."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.services import team_pulse_service


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/team", response_class=HTMLResponse)
    async def team_page(request: Request):
        c: Container = request.app.state.container
        signals = await team_pulse_service.gather(c)
        return _TEMPLATES.TemplateResponse(
            request, "team.html", _ctx(request, "team", signals=signals))

    return router
