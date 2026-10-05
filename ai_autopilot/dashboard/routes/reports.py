"""Loop reports and filing their findings as work items."""

from __future__ import annotations

import asyncio
import contextlib
import json
from html import escape as html_escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ai_autopilot import markdown_lite
from ai_autopilot import reports as reports_mod
from ai_autopilot import workspaces as workspaces_mod
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _log,
    _take_flash,
    work_item_link_base,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.logging_config import describe_exc

_SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")


def _finding_where(finding: dict) -> str:
    """``file:line`` for a finding, or the file alone, or ''."""
    where = str(finding.get("file") or "").strip()
    if where and finding.get("line"):
        where = f"{where}:{finding['line']}"
    return where


def _finding_work_item(finding: object) -> int:
    """The work item this finding already became, or 0. Tolerant of a report written
    before the field existed, and of a hand-edited one."""
    if not isinstance(finding, dict):
        return 0
    with contextlib.suppress(TypeError, ValueError):
        return max(0, int(finding.get("work_item_id") or 0))
    return 0


def _worst_severity(findings) -> str:
    """The most severe label among these, for a combined item's title. An item
    titled with the mildest of the six is an item nobody prioritises correctly."""
    seen = {str(f.get("severity") or "").strip().lower() for f in findings}
    return next((s for s in _SEVERITY_ORDER if s in seen), "audit")


# Fallback only, for when ADO cannot be reached. Work-item types are a property of
# the project's PROCESS TEMPLATE, not of Azure DevOps: Agile defines User Story and
# no Product Backlog Item, Scrum the reverse, CMMI defines Requirement. This list
# was the whole offer, so the picker named types half the projects would reject —
# which is what the report screen was doing on TLCL-DxFac.
_WORK_ITEM_TYPES = ("Bug", "Task", "Issue", "User Story")


async def _types_by_project(c: Container, projects: list[str]) -> dict[str, list[str]]:
    """``{project: [work-item type]}`` for the picker, one request per project.

    Concurrent, and best-effort per project: one unreachable project falls back to
    the static list rather than emptying the dropdown for every other project too.
    """
    wanted = list(dict.fromkeys(p for p in projects if p)) or [""]
    found = await asyncio.gather(
        *(c.ado.get_work_item_types(p) for p in wanted), return_exceptions=True
    )
    out: dict[str, list[str]] = {}
    for project, types in zip(wanted, found, strict=True):
        if isinstance(types, BaseException):
            _log.warning("work-item types unavailable", project=project,
                         error=describe_exc(types))
            types = []
        out[project] = list(types) or list(_WORK_ITEM_TYPES)
    return out


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/reports", response_class=HTMLResponse)
    async def reports_page(request: Request, loop: str = ""):
        """Every audit a report loop has produced, newest first."""
        c: Container = request.app.state.container
        rows = await c.loop_report_repo.recent(limit=100, loop_name=loop)
        return _TEMPLATES.TemplateResponse(
            request, "reports.html",
            _ctx(request, "reports", rows=rows, loops=await c.loop_report_repo.loop_names(),
                 selected=loop, severities=reports_mod.SEVERITIES),
        )

    @router.get("/reports/{report_id}", response_class=HTMLResponse)
    async def report_detail(request: Request, report_id: int):
        c: Container = request.app.state.container
        cfg = c.config
        row = await c.loop_report_repo.get(report_id)
        if row is None:
            return RedirectResponse("/dashboard/reports", status_code=303)
        try:
            findings = json.loads(row.findings_json or "[]")
        except (ValueError, TypeError):
            findings = []       # a malformed row must still render its text
        flash = _take_flash(request)
        views = workspaces_mod.resolve(cfg)
        types_by_project = await _types_by_project(
            c, [p for ws in views if ws.enabled for p in ws.projects]
        )
        response = _TEMPLATES.TemplateResponse(
            request, "report_detail.html",
            _ctx(request, "reports", row=row, findings=findings, flash=flash,
                 body=reports_mod.strip_findings_block(row.body_md or ""),
                 # Rendered here rather than in the template: the renderer escapes
                 # every byte before producing a tag, which is what makes the result
                 # safe to mark `|safe` — an audit quotes the very code it is warning
                 # about, so the report must never execute what it is showing you.
                 body_html=markdown_lite.render(
                     reports_mod.strip_findings_block(row.body_md or "")
                 ),
                 workspaces=views,
                 # What each finding already became, so the row can say so and refuse
                 # to file it twice. Resolved here rather than in the template: the
                 # template must not have to know how a finding stores it.
                 filed={i: _finding_work_item(f) for i, f in enumerate(findings)},
                 item_link=work_item_link_base(cfg),
                 # Per PROJECT, because that is what decides them. The template swaps
                 # the Loại options when the Project dropdown changes, so the two can
                 # never disagree.
                 types_by_project=types_by_project,
                 item_types=_WORK_ITEM_TYPES,
                 severities=reports_mod.SEVERITIES),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/reports/{report_id}/file")
    async def report_file_work_items(request: Request, report_id: int):
        """Turn the findings somebody ticked into work items.

        An audit that nobody can act on is a document. Its findings already carry a
        title, a severity, a file and a line — everything a work item needs — and
        re-typing that by hand is where they stop being acted on at all.

        Two shapes, because the audit produces both kinds of finding.

        ``mode=each`` (the default) files one item per finding: they are usually fixed
        by different people at different times, and a single item holding nine findings
        is closed when the easiest one is done.

        ``mode=one`` files a single item listing all of them. That is the right shape
        when the findings are one defect seen from several angles — six sequential-loop
        findings in the same service are one refactor, and six items for it is six
        people reading the same context and three of them rewriting the same method.
        The reader picks, because only the reader knows which of the two they have.

        A finding that already produced a work item is NOT filed again unless the form
        says so explicitly. Checked here and not only in the template: a disabled
        checkbox is a suggestion, and the same POST can arrive from curl.
        """
        c: Container = request.app.state.container
        cfg = c.config
        row = await c.loop_report_repo.get(report_id)
        if row is None:
            return RedirectResponse("/dashboard/reports", status_code=303)
        form = await request.form()
        picked = {str(v) for v in form.getlist("finding")}
        # Indices the reader explicitly asked to file AGAIN, having seen what they
        # already became.
        refile = {str(v) for v in form.getlist("refile")}
        mode = "one" if str(form.get("mode") or "each").strip() == "one" else "each"
        project = str(form.get("project") or "").strip()
        item_type = str(form.get("item_type") or "Bug").strip()
        # Validate against the project that will actually receive the item, not against
        # a list of types someone hoped every template has. Falls back to that list when
        # ADO cannot be reached, so an outage cannot make filing impossible.
        allowed = (await _types_by_project(c, [project]))[project or ""]
        if item_type not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"{item_type!r} is not a work item type in "
                       f"{project or cfg.ado_project!r}",
            )
        if not picked:
            return _flash(f"/dashboard/reports/{report_id}", "file_none_picked")
        try:
            findings = json.loads(row.findings_json or "[]")
        except (ValueError, TypeError):
            findings = []

        # Already filed, and not explicitly asked for again → skip. Silently refiling is
        # how an audit ends up with the same finding open three times, each with its own
        # half-done discussion.
        chosen: list[int] = []
        skipped = 0
        for index, finding in enumerate(findings):
            if str(index) not in picked:
                continue
            filed_as = _finding_work_item(finding)
            if filed_as and str(index) not in refile:
                skipped += 1
                continue
            chosen.append(index)
        if not chosen:
            return _flash(
                f"/dashboard/reports/{report_id}",
                "file_all_already" if skipped else "file_none_picked",
            )

        created: list[int] = []
        failed = 0
        if mode == "one":
            worst = _worst_severity(findings[i] for i in chosen)
            title = (
                f"[{worst}] {len(chosen)} finding từ audit "
                f"{row.loop_name or ''} (report #{report_id})"
            )[:250]
            body = "".join(
                f"<div><b>{html_escape(str(findings[i].get('severity') or 'audit'))}</b> · "
                f"{html_escape(str(findings[i].get('title') or 'Audit finding'))}"
                + (f" — <code>{html_escape(_finding_where(findings[i]))}</code>"
                   if _finding_where(findings[i]) else "")
                + (f"<br/>{html_escape(str(findings[i].get('detail') or ''))}"
                   if findings[i].get("detail") else "")
                + "</div>"
                for i in chosen
            ) + (
                f"<div><b>From audit:</b> {html_escape(row.loop_name or '')} "
                f"(report #{report_id})</div>"
            )
            try:
                new_id = await c.ado.create_work_item(
                    title=title, item_type=item_type, parent_id=None,
                    tag=cfg.trigger_tag, description=body, project=project,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning("filing findings failed", error=describe_exc(exc))
                new_id = 0
            if new_id:
                created.append(new_id)
                await c.loop_report_repo.mark_findings_filed(report_id, chosen, new_id)
            else:
                failed = len(chosen)
        else:
            for index in chosen:
                finding = findings[index]
                title = str(finding.get("title") or "").strip() or "Audit finding"
                where = _finding_where(finding)
                severity = str(finding.get("severity") or "")
                body = "<br/>".join(filter(None, [
                    html_escape(str(finding.get("detail") or "")),
                    f"<b>Where:</b> <code>{html_escape(where)}</code>" if where else "",
                    f"<b>Severity:</b> {html_escape(severity)}" if severity else "",
                    f"<b>From audit:</b> {html_escape(row.loop_name or '')} "
                    f"(report #{report_id})",
                ]))
                try:
                    new_id = await c.ado.create_work_item(
                        title=f"[{severity or 'audit'}] {title}"[:250],
                        item_type=item_type, parent_id=None,
                        tag=cfg.trigger_tag, description=body, project=project,
                    )
                except Exception as exc:  # noqa: BLE001 — one bad finding must not stop the rest
                    _log.warning("filing a finding failed", error=describe_exc(exc), title=title)
                    new_id = 0
                if new_id:
                    created.append(new_id)
                    await c.loop_report_repo.mark_findings_filed(report_id, [index], new_id)
                else:
                    failed += 1

        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="report.filed",
            target=f"report #{report_id} → {', '.join(f'#{i}' for i in created)}"[:300],
            detail=f"{project or cfg.ado_project}: {item_type}",
        )
        _log.info("audit findings filed as work items",
                  report=report_id, created=created, failed=failed, project=project)
        if not created:
            return _flash(f"/dashboard/reports/{report_id}", "file_failed")
        return _flash(
            f"/dashboard/reports/{report_id}",
            "file_partial" if failed else "file_created",
        )

    return router
