"""Server-rendered dashboard (replaces the Blazor dashboard)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.logging_config import get_logger

_log = get_logger("dashboard")

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

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: int = 0):
        c: Container = request.app.state.container
        current = {
            f.key: getattr(c.config, f.key, "")
            for f in settings_form.FIELDS
            if f.key not in settings_form.SECRET_KEYS
        }
        has_pat = bool(getattr(c.config, "ado_pat", ""))
        return _TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                "settings",
                sections=settings_form.sections(),
                current=current,
                has_pat=has_pat,
                restart_keys=settings_form.RESTART_REQUIRED,
                saved=bool(saved),
                config_path=str(config_file_path()),
            ),
        )

    @router.post("/settings")
    async def save_settings(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        updates = settings_form.parse_form(form)

        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()  # re-read org URL if it changed
        _log.info("settings updated via dashboard", keys=sorted(updates.keys()))

        return RedirectResponse(url="/dashboard/settings?saved=1", status_code=303)

    return router


def files_changed_count(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return len(json.loads(raw))
    except (ValueError, TypeError):
        return 0
