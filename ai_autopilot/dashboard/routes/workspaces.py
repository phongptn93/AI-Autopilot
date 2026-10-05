"""Workspace list and the sidebar workspace selector."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ai_autopilot import workspaces as workspaces_mod
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    _WS_COOKIE,
    _WS_ERROR_COOKIE,
    _flash,
    _log,
    _take_flash,
    _take_ws_reject,
    _ws_reject,
    forget_scans,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.workspace import discover_repos


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/workspaces", response_class=HTMLResponse)
    async def workspaces_page(request: Request):
        """Manage the workspaces: which folder builds which ADO project(s), on which
        base branch.

        Replaces three places that had to agree (the global settings fields, a
        ``workspaces:`` YAML list and a one-line textarea) with one list. The first row
        is the default workspace — editable, not removable, because it is also the
        fallback for any project no other workspace claims."""
        c: Container = request.app.state.container
        flash = _take_flash(request)
        rejected = _take_ws_reject(request)
        views = rejected.get("views") or workspaces_mod.resolve(c.config)
        try:
            discovered = {
                ws.id: discover_repos(ws.directory) for ws in views if ws.directory
            }
        except Exception:  # noqa: BLE001 — the page must render on an unreadable disk
            discovered = {}
        response = _TEMPLATES.TemplateResponse(
            request, "workspaces.html",
            _ctx(
                request, "workspaces", flash=flash, views=views,
                errors=rejected.get("errors") or [], discovered=discovered,
                projects=c.config.effective_ado_projects,
            ),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        if rejected:
            response.delete_cookie(_WS_ERROR_COOKIE, path="/dashboard")
        return response

    @router.post("/workspaces")
    async def save_workspaces(request: Request):
        """Save the workspace list.

        Validation BLOCKS the save rather than warning, because every problem it
        catches is invisible at runtime: a project claimed by two workspaces builds in
        whichever one happens to win, and a workspace with no folder quietly runs its
        items in the default one — both look like the config was applied."""
        c: Container = request.app.state.container
        form = await request.form()
        views, errors = workspaces_mod.parse_form(form)
        if errors:
            _log.info("workspace config rejected", count=len(errors))
            return _ws_reject(errors, views)

        updates = workspaces_mod.carry_secrets(
            workspaces_mod.to_settings_updates(views), c.config
        )
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        # A workspace may have just switched tracker, or had its Jira details filled in.
        c.build_providers()
        c.ado.refresh()   # the polled project set just changed
        forget_scans()   # cached scans describe the OLD config
        _log.info(
            "workspaces updated via dashboard",
            names=[v.label for v in views], projects=c.config.effective_ado_projects,
        )
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.workspaces_updated",
            target=", ".join(v.label for v in views)[:300],
        )
        return _flash("/dashboard/workspaces", "ws_saved")

    @router.post("/workspace/select")
    async def select_workspace(request: Request):
        """Point the dashboard at one workspace (or all of them).

        A POST because it writes a cookie, and a redirect back to where the operator
        was so the selector does not lose their place. It scopes the VIEW only — the
        autopilot keeps polling and running every workspace either way."""
        form = await request.form()
        chosen = str(form.get("workspace", "all") or "all").strip()
        back = str(form.get("back", "/dashboard") or "/dashboard")
        if not back.startswith("/dashboard"):
            back = "/dashboard"   # never bounce off-site on a value from the page
        response = RedirectResponse(back, status_code=303)
        response.set_cookie(
            _WS_COOKIE, chosen, max_age=60 * 60 * 24 * 365, httponly=True,
            samesite="lax", path="/dashboard",
        )
        return response

    return router
