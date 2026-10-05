"""The per-task room, its artifact preview, and spec drift."""

from __future__ import annotations

import contextlib
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ai_autopilot import spec_drift
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES, _group_drifts
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.services.spec_guard import SpecGuard
from ai_autopilot.services.task_room import TaskRoomService


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/task/{work_item_id}", response_class=HTMLResponse)
    async def task_room(request: Request, work_item_id: int, tab: str = "overview"):
        """One task, one page: what it is, what the agent decided, what it changed.

        The diff is the expensive part (a fetch + a diff per repo), so it is gathered
        only when its tab is being shown — otherwise every glance at a task would pay
        for a network round trip nobody asked to see.
        """
        c: Container = request.app.state.container
        room = await TaskRoomService(c).gather(work_item_id, with_diff=(tab == "code"))
        org = (c.config.ado_organization or "").rstrip("/")
        return _TEMPLATES.TemplateResponse(
            request, "task_room.html",
            _ctx(
                request, "task", room=room, tab=tab,
                item_url=f"{org}/_workitems/edit/{work_item_id}" if org else "",
                drift_label=spec_drift.label_for, drift_icon=spec_drift.icon_for,
            ),
        )

    @router.get("/task/{work_item_id}/preview")
    async def task_preview(request: Request, work_item_id: int, path: str):
        """Serve one HTML artifact the run produced, for the preview pane.

        Two rails, because this turns a path in a query string into a file read:
        the resolved path must stay INSIDE the workspace (``..`` and absolute paths
        cannot escape it), and it must be a ``.html`` file — a repo is full of secrets,
        keys and source, and "render any file" would be an exfiltration endpoint
        wearing a feature's clothes. The page itself is then shown in a sandboxed
        iframe, so its scripts cannot reach the dashboard session around it.
        """
        c: Container = request.app.state.container
        # noqa on the path math below: these are local stats, not blocking reads, and
        # resolving BEFORE any read is the whole point of the containment check.
        root = Path(c.config.workspace_directory or "").resolve()  # noqa: ASYNC240
        try:
            target = (root / path).resolve()  # noqa: ASYNC240 — local path math
            target.relative_to(root)                     # raises if it escaped
        except (ValueError, OSError):
            return PlainTextResponse("Đường dẫn không hợp lệ.", status_code=400)
        if target.suffix.lower() != ".html" or not target.is_file():
            return PlainTextResponse("Chỉ xem được file .html trong workspace.", status_code=400)
        try:
            body = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return PlainTextResponse(f"Không đọc được file: {exc}", status_code=500)
        return HTMLResponse(
            body,
            headers={
                # Belt and braces with the iframe sandbox: even if the artifact is
                # hostile, it gets no network and no frame ancestors but ours.
                # `sandbox` holds when the URL is opened directly too — outside the
                # iframe the page would otherwise run agent-written script with the
                # dashboard session (fetch to 'self' is the dashboard's own API).
                "Content-Security-Policy":
                    "sandbox; default-src 'self' 'unsafe-inline' data:; "
                    "connect-src 'none'; form-action 'none'; frame-ancestors 'self'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/specs", response_class=HTMLResponse)
    async def specs_page(request: Request):
        """Spec drift: what the agent decided that the work item never said.

        Grouped by work item, because that is the unit a BA edits — a flat list of
        deviations would have them opening the same item three times.
        """
        c: Container = request.app.state.container
        org = (c.config.ado_organization or "").rstrip("/")

        def _url(work_item_id: int) -> str:
            return f"{org}/_workitems/edit/{work_item_id}" if org else ""

        open_rows = await c.spec_drift_repo.open_drifts()
        resolved_rows = await c.spec_drift_repo.recent_resolved(limit=30)
        kinds = Counter(r.kind for r in open_rows)
        return _TEMPLATES.TemplateResponse(
            request, "specs.html",
            _ctx(
                request, "specs",
                open_items=_group_drifts(open_rows),
                resolved=_group_drifts(resolved_rows),
                open_count=len(open_rows),
                kind_totals=kinds.most_common(),
                label=spec_drift.label_for, icon=spec_drift.icon_for,
                work_item_url=_url if org else None,
            ),
        )

    @router.post("/specs/resolve")
    async def specs_resolve(request: Request, work_item_id: int = Form(...)):
        """A human says the specification is back in line: clear the tag, say so on the
        item, and tick the rows off."""
        c: Container = request.app.state.container
        # The dashboard authenticates with a shared password, not per-person accounts,
        # so "who" is the surface, not a name — same convention as the audit log.
        await SpecGuard(c).mark_resolved(work_item_id, by="dashboard")
        with contextlib.suppress(Exception):
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="spec_drift.resolved",
                target=str(work_item_id), detail="",
            )
        return RedirectResponse("/dashboard/specs", status_code=303)

    return router
