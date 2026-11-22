"""Code health page — hotspots, knowledge concentration, debt markers per repo.

The repo is chosen BY NAME from the git repos found in the configured workspaces,
never by a path in the query string: this page runs git in whatever directory it is
handed, so a typed path would let anyone who can open the dashboard point it at any
folder on the machine.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import code_health
from ai_autopilot.code_health import repo_choices
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES
from ai_autopilot.dashboard.routes._shared import _ctx

WINDOWS = (30, 90, 180, 365)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/code-health", response_class=HTMLResponse)
    async def code_health_page(request: Request, repo: str = "", days: int = 90):
        c: Container = request.app.state.container
        choices = repo_choices(c.config)
        chosen = next((r for r in choices if r.key == repo), None)
        if chosen is None and choices and not repo:
            chosen = choices[0]
        days = days if days in WINDOWS else 90
        report = None
        if chosen is not None:
            # git can take seconds on a big repo; keep the event loop free.
            report = await asyncio.to_thread(code_health.cached_analyze, chosen.path, days)
        return _TEMPLATES.TemplateResponse(
            request, "code_health.html",
            _ctx(request, "code_health", choices=choices, chosen=chosen,
                 unknown_repo=bool(repo) and chosen is None, requested=repo,
                 days=days, windows=WINDOWS, report=report,
                 share_limit=int(code_health.CONCENTRATION_SHARE * 100),
                 away_days=code_health.AWAY_DAYS,
                 min_commits=code_health.CONCENTRATION_MIN_COMMITS),
        )

    return router
