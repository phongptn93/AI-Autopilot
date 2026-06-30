"""Server-rendered dashboard (replaces the Blazor dashboard)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ai_autopilot import activity
from ai_autopilot.board import COLUMNS, build_board, latest_records
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.logging_config import get_logger
from ai_autopilot.skills_catalog import discover_skills
from ai_autopilot.workspace import discover_repos

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
        selected_tag = request.query_params.get("tag") or "all"
        tag_filter = None if selected_tag == "all" else selected_tag
        stats = await c.execution_repo.get_stats(trigger_tag=tag_filter)
        recent = await c.execution_repo.get_recent(20, trigger_tag=tag_filter)
        return _TEMPLATES.TemplateResponse(
            request,
            "overview.html",
            _ctx(
                request,
                "overview",
                stats=stats,
                recent=recent,
                tags=c.config.effective_trigger_tags,
                selected_tag=selected_tag,
            ),
        )

    async def _board_ctx(request: Request) -> dict:
        c: Container = request.app.state.container
        try:
            items = await c.ado.get_all_tagged_work_items()
            error = None
        except Exception as exc:  # noqa: BLE001
            items, error = [], str(exc)
        tags = c.config.effective_trigger_tags
        selected_tag = request.query_params.get("tag") or "all"
        if selected_tag != "all":
            sel = selected_tag.lower()
            items = [i for i in items if any(t.lower() == sel for t in i.tags)]
        records = await c.execution_repo.get_recent(200)
        states = {s.work_item_id: s.state.value for s in await c.state_repo.all()}
        cols = build_board(items, latest_records(records), c.config, states)
        return _ctx(
            request,
            "board",
            board=cols,
            columns=COLUMNS,
            total=len(items),
            error=error,
            project=c.config.ado_project,
            interactive=c.config.execution_mode == "interactive",
            tags=tags,
            selected_tag=selected_tag,
        )

    @router.get("/board", response_class=HTMLResponse)
    async def board(request: Request):
        return _TEMPLATES.TemplateResponse(request, "board.html", await _board_ctx(request))

    @router.get("/board/partial", response_class=HTMLResponse)
    async def board_partial(request: Request):
        """Just the columns — fetched by the page's auto-refresh, no full reload."""
        return _TEMPLATES.TemplateResponse(request, "_board_cols.html", await _board_ctx(request))

    @router.get("/activity/{item_id}", response_class=HTMLResponse)
    async def activity_view(request: Request, item_id: int):
        c: Container = request.app.state.container
        feed = activity.read(c.config.workspace_directory, item_id)
        return _TEMPLATES.TemplateResponse(
            request, "activity.html", _ctx(request, "board", item_id=item_id, feed=feed)
        )

    @router.get("/activity/{item_id}/partial", response_class=PlainTextResponse)
    async def activity_partial(request: Request, item_id: int):
        c: Container = request.app.state.container
        return PlainTextResponse(
            activity.read(c.config.workspace_directory, item_id) or "(no activity yet — waiting for the agent…)"
        )

    @router.get("/history", response_class=HTMLResponse)
    async def history(request: Request):
        c: Container = request.app.state.container
        records = await c.execution_repo.get_recent(100)
        return _TEMPLATES.TemplateResponse(
            request, "history.html", _ctx(request, "history", records=records)
        )

    @router.post("/history/{record_id}/delete")
    async def delete_history(request: Request, record_id: int):
        c: Container = request.app.state.container
        await c.execution_repo.delete(record_id)
        _log.info("execution record deleted via dashboard", record_id=record_id)
        return RedirectResponse(url="/dashboard/history", status_code=303)

    @router.post("/history/clear")
    async def clear_history(request: Request):
        c: Container = request.app.state.container
        removed = await c.execution_repo.clear_all()
        _log.info("execution history cleared via dashboard", removed=removed)
        return RedirectResponse(url="/dashboard/history", status_code=303)

    @router.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        c: Container = request.app.state.container
        return _TEMPLATES.TemplateResponse(
            request, "config.html", _ctx(request, "config", cfg=c.config)
        )

    @router.get("/capabilities", response_class=HTMLResponse)
    async def capabilities(request: Request):
        c: Container = request.app.state.container
        skills = discover_skills(c.config.workspace_directory)
        return _TEMPLATES.TemplateResponse(
            request,
            "capabilities.html",
            _ctx(
                request,
                "capabilities",
                skills=skills,
                workspace=c.config.workspace_directory,
                ai_native=bool(c.config.workspace_directory),
            ),
        )

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(
        request: Request,
        saved: int = 0,
        reloaded: int = 0,
        imported: int = 0,
        import_error: str = "",
    ):
        c: Container = request.app.state.container
        current = {
            f.key: getattr(c.config, f.key, "")
            for f in settings_form.FIELDS
            if f.key not in settings_form.SECRET_KEYS
        }
        has_pat = bool(getattr(c.config, "ado_pat", ""))
        discovered = discover_repos(c.config.workspace_directory)
        allowed = {r.lower() for r in c.config.allowed_repos}
        try:
            ado_states = await c.ado.get_states()
        except Exception:  # noqa: BLE001 — Settings must render even if ADO is down
            ado_states = []
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
                reloaded=bool(reloaded),
                imported=bool(imported),
                import_error=import_error,
                config_path=str(config_file_path()),
                repos=discovered,
                allowed_repos=allowed,
                ado_states=ado_states,
            ),
        )

    @router.post("/settings")
    async def save_settings(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        updates = settings_form.parse_form(form)
        updates["allowed_repos"] = settings_form.parse_repos(form)

        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()  # re-read org URL if it changed
        _log.info("settings updated via dashboard", keys=sorted(updates.keys()))

        return RedirectResponse(url="/dashboard/settings?saved=1", status_code=303)

    @router.post("/settings/reload")
    async def reload_settings(request: Request):
        """Re-read config.yaml from disk into the running app (no restart)."""
        c: Container = request.app.state.container
        changed = settings_form.reload_from_file(c.config)
        c.ado.refresh()  # re-read org URL if it changed
        _log.info("config reloaded from file via dashboard", changed=changed)
        return RedirectResponse(url="/dashboard/settings?reloaded=1", status_code=303)

    @router.get("/settings/export")
    async def export_config(request: Request):
        """Download the current config as YAML, minus secrets + machine-specific keys."""
        c: Container = request.app.state.container
        body = settings_form.export_yaml(c.config)
        _log.info("config exported via dashboard")
        return Response(
            content=body,
            media_type="application/x-yaml",
            headers={"Content-Disposition": 'attachment; filename="autopilot-config.yaml"'},
        )

    @router.post("/settings/import")
    async def import_config(request: Request):
        """Apply an uploaded YAML config (shared by a teammate). PAT is never imported."""
        c: Container = request.app.state.container
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return RedirectResponse("/dashboard/settings?import_error=No+file", status_code=303)
        raw = (await upload.read()).decode("utf-8", errors="replace")
        try:
            updates = settings_form.import_settings(raw, set(type(c.config).model_fields))
        except (ValueError, yaml.YAMLError) as exc:
            _log.warning("config import failed", error=str(exc))
            return RedirectResponse("/dashboard/settings?import_error=Invalid+file", status_code=303)
        if not updates:
            return RedirectResponse("/dashboard/settings?import_error=Nothing+to+import", status_code=303)
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()
        _log.info("config imported via dashboard", keys=sorted(updates.keys()))
        return RedirectResponse("/dashboard/settings?imported=1", status_code=303)

    return router


def files_changed_count(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return len(json.loads(raw))
    except (ValueError, TypeError):
        return 0
