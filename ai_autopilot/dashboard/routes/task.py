"""The per-task room, its artifact preview, and spec drift."""

from __future__ import annotations

import contextlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ai_autopilot import spec_drift, spec_library
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES, _group_drifts
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.services.spec_guard import SpecGuard
from ai_autopilot.services.task_room import TaskRoomService


def _asks(row) -> bool:
    """Does this point need a business decision (not just a wording fix)?"""
    return bool(getattr(row, "needs_decision", False)) or row.kind in ("spec_unclear", "assumption")


def _md(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


class _RowView:
    """A drift row plus the one derived fact the template needs."""

    def __init__(self, row) -> None:
        self._row = row
        self.ask = _asks(row)

    def __getattr__(self, name):
        return getattr(self._row, name)


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
    async def specs_page(request: Request, kind: str = ""):
        """Spec drift: what the agent decided that the work item never said.

        Grouped by work item, because that is the unit a BA edits; one row per point,
        laid out as the team's own rule asks — *Mục · Hiện tại · Điều chỉnh* — with a
        decision per point, because "update the spec" and "fix the code" are different
        answers and the old single button could only record the first.
        """
        c: Container = request.app.state.container
        cfg = c.config
        org = (cfg.ado_organization or "").rstrip("/")

        def _url(work_item_id: int) -> str:
            return f"{org}/_workitems/edit/{work_item_id}" if org else ""

        open_rows = await c.spec_drift_repo.open_drifts()
        resolved_rows = await c.spec_drift_repo.recent_resolved(limit=60)
        kinds = Counter(r.kind for r in open_rows)
        asks_open = sum(1 for r in open_rows if _asks(r))
        if kind == "ask":
            shown = [r for r in open_rows if _asks(r)]
        elif kind:
            shown = [r for r in open_rows if r.kind == kind]
        else:
            shown = open_rows
        # Link each item to its spec file when the library knows one — the reader
        # opens the document it has to edit, not just the work item.
        spec_for: dict[int, str] = {}
        with contextlib.suppress(Exception):
            for spec in spec_library.discover(cfg.workspace_directory):
                if spec.item_id and spec.kind == "md":
                    spec_for.setdefault(spec.item_id, spec.rel)
        now = datetime.now(UTC)
        groups = _group_drifts(shown)
        for group in groups:
            at = group.created_at
            at = at if at.tzinfo else at.replace(tzinfo=UTC)
            group["age_days"] = max(0, int((now - at).total_seconds() // 86400))
            group["asks"] = sum(1 for r in group.rows if _asks(r))
            spec_rel = spec_for.get(group.work_item_id, "")
            group["spec_q"] = quote(spec_rel) if spec_rel else ""
            group["rows"] = [_RowView(r) for r in group.rows]
        resolved = _group_drifts(resolved_rows)[:30]
        for group in resolved:
            group["decided"] = Counter(r.decision for r in group.rows if r.decision).most_common()
        return _TEMPLATES.TemplateResponse(
            request, "specs.html",
            _ctx(
                request, "specs",
                open_items=groups, resolved=resolved,
                open_count=len(open_rows), asks_open=asks_open, kind=kind,
                kind_totals=kinds.most_common(),
                stats=await c.spec_drift_repo.stats(),
                sla_days=max(1, int(cfg.spec_drift_sla_days or 7)),
                spec_drift_enabled=cfg.spec_drift_enabled,
                decisions=list(spec_drift.DECISIONS.items()),
                decision_label=spec_drift.decision_label,
                label=spec_drift.label_for, icon=spec_drift.icon_for,
                work_item_url=_url if org else None,
            ),
        )

    @router.post("/specs/decide")
    async def specs_decide(request: Request):
        """One point, one decision. The item closes itself when its last point is decided."""
        c: Container = request.app.state.container
        form = await request.form()
        back_kind = str(form.get("back") or "")
        back = "/dashboard/specs" + (f"?kind={quote(back_kind)}" if back_kind else "")
        try:
            row_id = int(str(form.get("row_id", "")))
        except ValueError:
            return RedirectResponse(back, status_code=303)
        decision = str(form.get("decision", "")).strip()
        note = str(form.get("note", "")).strip()[:400]
        item_id, left = await SpecGuard(c).decide(row_id, decision, note, by="dashboard")
        if item_id:
            with contextlib.suppress(Exception):
                await c.audit_repo.record(
                    actor="dashboard", source="dashboard", action="spec_drift.decided",
                    target=f"#{item_id}", detail=f"{decision}: {note}"[:300]
                    + (" · item closed" if left == 0 else f" · {left} left"),
                )
        return RedirectResponse(back, status_code=303)

    @router.get("/specs/export.md")
    async def specs_export(request: Request):
        """Every open point as a Markdown table — to paste into the spec, a mail, a meeting."""
        c: Container = request.app.state.container
        rows = await c.spec_drift_repo.open_drifts()
        out = ["# Lệch spec chờ quyết", ""]
        for group in _group_drifts(rows):
            out += [f"## #{group.work_item_id} — {group.title}", "",
                    "| Mục | Hiện tại (spec) | Điều chỉnh (code) | Cần chốt |",
                    "|---|---|---|---|"]
            for r in group.rows:
                cell = [r.where or "—", r.spec_says or "—", r.code_does or r.summary,
                        "❗" if _asks(r) else ""]
                out.append("| " + " | ".join(_md(x) for x in cell) + " |")
            out.append("")
        return PlainTextResponse("\n".join(out), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition":
                                          'attachment; filename="spec-drift.md"'})

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
