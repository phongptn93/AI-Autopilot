"""Requirements & specs: where a BA writes a requirement and reads the spec it became.

Before this page a BA's way in was ADO itself, and the spec the autopilot wrote was
reachable only as an unfiltered recent-HTML list inside one task's room. Three things
now sit in one place:

* **Write** a requirement — title, the need, acceptance criteria — into the tracker,
  optionally handing it straight to the BA role to analyse and spec.
* **Find** every spec in the workspace, linked to its work item when the file says.
* **Read** one, rendered, and act on it: approve it (hand the item on to Dev), or send
  feedback (a comment the next BA run reads as its instructions).
"""

from __future__ import annotations

import html as html_mod
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ai_autopilot import markdown_lite, spec_library
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _log,
    _take_flash,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.execution import sdlc_plan
from ai_autopilot.services import planning_analyzer

#: Tag on a requirement written here but not yet handed to anybody — so a BA can find
#: their drafts on the board, and nothing picks them up by accident.
DRAFT_TAG = "requirement-draft"
_TYPES = ("User Story", "Product Backlog Item", "Requirement", "Feature", "Bug", "Task")


def _item_url(cfg, item_id: int) -> str:
    org = (cfg.ado_organization or "").rstrip("/")
    return f"{org}/_workitems/edit/{item_id}" if org and item_id else ""


def _description_html(need: str, criteria: str, by: str) -> str:
    """The work item body: the need, then the acceptance criteria as a checklist.

    Rendered with the same escape-first renderer the dashboard uses, so whatever a BA
    pastes (a table, code, a customer's wording) arrives as structure, never as markup.
    """
    parts = [markdown_lite.render(need.strip())] if need.strip() else []
    lines = [ln.strip(" -*\t") for ln in criteria.splitlines() if ln.strip(" -*\t")]
    if lines:
        items = "".join(f"<li>{html_mod.escape(ln)}</li>" for ln in lines)
        parts.append(f"<h3>Acceptance criteria</h3><ol>{items}</ol>")
    parts.append(f"<p><i>Tạo từ AI-Autopilot · {html_mod.escape(by)}</i></p>")
    return "".join(parts)


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/requirements", response_class=HTMLResponse)
    async def requirements_page(request: Request):
        c: Container = request.app.state.container
        cfg = c.config
        specs = spec_library.discover(cfg.workspace_directory)
        flash = _take_flash(request)
        ba = sdlc_plan.effective_roles(cfg).get("ba")
        response = _TEMPLATES.TemplateResponse(
            request, "requirements.html",
            _ctx(request, "requirements", flash=flash,
                 specs=[{
                     "rel": s.rel, "title": s.title, "kind": s.kind, "folder": s.folder,
                     "item_id": s.item_id, "item_url": _item_url(cfg, s.item_id),
                     "modified": datetime.fromtimestamp(s.modified).strftime("%Y-%m-%d %H:%M"),
                     "q": quote(s.rel),
                 } for s in specs],
                 types=_TYPES, projects=list(cfg.effective_ado_projects),
                 workspace=cfg.workspace_directory or "",
                 spec_dirs=spec_library.SPEC_DIRS,
                 ba_wired=bool(ba and sdlc_plan.role_doors(ba)),
                 dry_run=cfg.dry_run),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/requirements")
    async def create_requirement(request: Request):
        """Write one requirement into the tracker; optionally hand it to the BA role."""
        c: Container = request.app.state.container
        cfg = c.config
        form = await request.form()
        title = str(form.get("title", "")).strip()[:250]
        if not title:
            return _flash("/dashboard/requirements", "req_title_required")
        if cfg.dry_run:
            return _flash("/dashboard/requirements", "req_dry_run")
        item_type = str(form.get("type", "User Story")).strip() or "User Story"
        project = str(form.get("project", "")).strip()
        analyse = bool(form.get("analyse"))
        tags = [DRAFT_TAG] if not analyse else []
        body = _description_html(str(form.get("need", "")), str(form.get("criteria", "")),
                                 "dashboard")
        item_id = await c.ado.create_work_item(
            title, item_type, None, "; ".join(tags), description=body, project=project,
        )
        if not item_id:
            return _flash("/dashboard/requirements", "req_create_failed")
        if analyse:
            # The BA role's own pin, exactly as the board's ▶ on the BA view does: it
            # names the role for this leg and is released by the hand-off.
            await c.ado.add_tag(item_id, sdlc_plan.profile_tag("ba", cfg))
            await planning_analyzer.start_items(c, [item_id])
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="requirement.created",
            target=f"#{item_id}", detail=f"{item_type}: {title}"[:300] + (
                " → BA" if analyse else ""),
        )
        _log.info("requirement created", id=item_id, analyse=analyse, project=project)
        return _flash("/dashboard/requirements",
                      "req_created_analysing" if analyse else "req_created")

    @router.get("/requirements/spec", response_class=HTMLResponse)
    async def read_spec(request: Request, path: str):
        """One spec, rendered, with its siblings and the actions that move it on."""
        c: Container = request.app.state.container
        cfg = c.config
        target = spec_library.resolve(cfg.workspace_directory, path)
        if target is None:
            return PlainTextResponse("Không mở được spec này (chỉ file .md/.html trong thư "
                                     "mục spec của workspace).", status_code=404)
        text = spec_library.read(target)
        kind = target.suffix.lower().lstrip(".")
        spec = next((s for s in spec_library.discover(cfg.workspace_directory)
                     if s.rel == path), None)
        item_id = spec.item_id if spec else spec_library.item_ref(text[:6000])
        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request, "requirement_spec.html",
            _ctx(request, "requirements", flash=flash, rel=path, q=quote(path), kind=kind,
                 title=spec_library.title_of(text[:6000], kind, target.stem),
                 body=markdown_lite.render(text) if kind == "md" else "",
                 siblings=[{"rel": r, "q": quote(r), "name": r.rsplit("/", 1)[-1]}
                           for r in (spec.siblings if spec else [])],
                 item_id=item_id, item_url=_item_url(cfg, item_id)),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.get("/requirements/raw")
    async def raw_spec(request: Request, path: str):
        """An HTML spec or mockup, for the sandboxed frame — same rails as the task
        room's preview: contained path, spec folders only, no script reaches us."""
        c: Container = request.app.state.container
        target = spec_library.resolve(c.config.workspace_directory, path)
        if target is None or target.suffix.lower() != ".html":
            return PlainTextResponse("Không mở được file này.", status_code=404)
        return HTMLResponse(spec_library.read(target), headers={
            "Content-Security-Policy":
                "sandbox; default-src 'self' 'unsafe-inline' data:; "
                "connect-src 'none'; form-action 'none'; frame-ancestors 'self'",
            "X-Content-Type-Options": "nosniff",
        })

    @router.post("/requirements/review")
    async def review_spec(request: Request):
        """Approve a spec (hand the item to Dev) or send it back with feedback.

        Both leave a comment on the item, because the work item is the one thing BA,
        QC and the customer all read — a decision recorded only here would be lost.
        """
        c: Container = request.app.state.container
        cfg = c.config
        form = await request.form()
        back = f"/dashboard/requirements/spec?path={quote(str(form.get('path', '')))}"
        try:
            item_id = int(str(form.get("item_id", "")))
        except ValueError:
            item_id = 0
        if item_id <= 0:
            return _flash(back, "req_no_item")
        if cfg.dry_run:
            return _flash(back, "req_dry_run")
        decision = str(form.get("decision", "")).strip()
        note = str(form.get("note", "")).strip()[:4000]
        spec_name = html_mod.escape(str(form.get("path", "")))
        if decision == "approve":
            await c.ado.add_comment(item_id, (
                "<div><b>✅ SPEC APPROVED</b> — duyệt trên AI-Autopilot."
                f"<br/>Spec: <code>{spec_name}</code>"
                + (f"<p>{html_mod.escape(note)}</p>" if note else "") + "</div>"
            ))
            with_dev = bool(form.get("start_dev"))
            if with_dev:
                # Release whatever role is pinned (in ADO's own casing — its tag delete
                # is case-sensitive), then name Dev for the next leg.
                item = await c.ado.get_work_item(item_id)
                for stale in sdlc_plan.profile_pins(item.tags if item else [], cfg):
                    await c.ado.remove_tag(item_id, stale)
                await c.ado.add_tag(item_id, sdlc_plan.profile_tag("dev", cfg))
                await planning_analyzer.start_items(c, [item_id])
            await c.ado.remove_tag(item_id, DRAFT_TAG)
            action, code = "requirement.spec_approved", (
                "req_approved_dev" if with_dev else "req_approved")
        elif decision == "feedback":
            if not note:
                return _flash(back, "req_feedback_empty")
            await c.ado.add_comment(item_id, (
                "<div><b>💬 SPEC FEEDBACK</b> — cần chỉnh lại spec."
                f"<br/>Spec: <code>{spec_name}</code><p>{html_mod.escape(note)}</p></div>"
            ))
            action, code = "requirement.spec_feedback", "req_feedback_sent"
        else:
            return _flash(back, "req_no_item")
        await c.audit_repo.record(actor="dashboard", source="dashboard", action=action,
                                  target=f"#{item_id}", detail=note[:300])
        return _flash(back, code)

    return router
