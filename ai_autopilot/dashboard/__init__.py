"""Server-rendered dashboard (replaces the Blazor dashboard)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ai_autopilot.container import Container

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_CATEGORY_LABELS = {
    "backendtask": ("BE", "cat-be"),
    "frontendtask": ("FE", "cat-fe"),
    "bug": ("Bug", "cat-bug"),
    "databasetask": ("DB", "cat-db"),
    "testtask": ("QC", "cat-qc"),
    "requirement": ("Req", "cat-req"),
}
_STATUS_CLASS = {
    "Success": "badge-success",
    "Failed": "badge-failed",
    "Running": "badge-running",
}


def _category_badge(category: str) -> tuple[str, str]:
    label, css = _CATEGORY_LABELS.get((category or "").lower(), (category or "—", "cat-default"))
    return label, css


def _status_class(status: str) -> str:
    return _STATUS_CLASS.get(status, "badge-retrying")


def _mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def create_dashboard_router() -> APIRouter:
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])

    def _ctx(request: Request, active: str, **extra) -> dict:
        return {
            "request": request,
            "active": active,
            "mmss": _mmss,
            "fmt_duration": _fmt_duration,
            "category_badge": _category_badge,
            "status_class": _status_class,
            **extra,
        }

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        c: Container = request.app.state.container
        stats = await c.execution_repo.get_stats()
        recent = await c.execution_repo.get_recent(20)
        return _TEMPLATES.TemplateResponse(
            request, "overview.html", _ctx(request, "overview", stats=stats, recent=recent)
        )

    @router.get("/history", response_class=HTMLResponse)
    async def history(request: Request):
        c: Container = request.app.state.container
        records = await c.execution_repo.get_recent(100)
        return _TEMPLATES.TemplateResponse(
            request, "history.html", _ctx(request, "history", records=records)
        )

    @router.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        c: Container = request.app.state.container
        return _TEMPLATES.TemplateResponse(
            request, "config.html", _ctx(request, "config", cfg=c.config)
        )

    @router.get("/capabilities", response_class=HTMLResponse)
    async def capabilities(request: Request):
        return _TEMPLATES.TemplateResponse(
            request, "capabilities.html", _ctx(request, "capabilities")
        )

    return router


def files_changed_count(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return len(json.loads(raw))
    except (ValueError, TypeError):
        return 0
