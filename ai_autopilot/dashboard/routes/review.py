"""🧭 Review Studio — check a spec, a report or a question against the real code."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from ai_autopilot import markdown_lite, review_docs, review_snapshot, spec_library
from ai_autopilot import review_studio as rs
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _take_flash,
    selected_workspace,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.services.review_studio import studio_for

_log = get_logger(__name__)
_KIND_LABEL = {rs.KIND_SPEC: "📄 Tài liệu ↔ code", rs.KIND_REPORT: "🛡️ Kiểm chứng báo cáo",
               rs.KIND_QUESTION: "💬 Hỏi đáp"}


def _ago(dt: datetime | None) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = (datetime.now(UTC) - dt).total_seconds()
    if s < 90:
        return "vừa xong"
    if s < 3600:
        return f"{int(s // 60)} phút trước"
    if s < 86400:
        return f"{int(s // 3600)} giờ trước"
    return f"{int(s // 86400)} ngày trước"


def _link(request: Request, rid: int) -> str:
    return str(request.base_url).rstrip("/") + f"/dashboard/review/{rid}"


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/review", response_class=HTMLResponse)
    async def review_hub(request: Request):
        c: Container = request.app.state.container
        flash = _take_flash(request)
        studio = studio_for(c)
        wss = studio.workspaces()
        # Default to the workspace the sidebar is showing; "all" → the first one.
        picked = selected_workspace(request)
        current = next((w for w in wss if w.id == picked), wss[0] if wss else None)
        last = await c.review_repo.latest_by_target(rs.KIND_SPEC)
        spec_rows = []
        for ws in wss or [None]:
            folder = ws.directory if ws else c.config.workspace_directory
            for s in spec_library.discover(folder):
                spec_rows.append({"spec": s, "last": last.get(s.rel),
                                  "ws": ws.id if ws else "", "ws_label": ws.label if ws else ""})
        specs = [r["spec"] for r in spec_rows]
        reports = await c.loop_report_repo.recent(15)
        history = await c.review_repo.recent(60)
        done = [r for r in history if r.status == "done"]
        scored = [r.score for r in done if r.kind == rs.KIND_SPEC and r.score is not None]
        open_q = 0
        for r in done[:30]:
            if r.kind == rs.KIND_SPEC:
                open_q += len(rs.load(r.result_json).questions)
        kpi = {
            "specs": len(specs),
            "reviewed": sum(1 for s in specs if s.rel in last),
            "avg": round(sum(scored) / len(scored), 1) if scored else None,
            "running": sum(1 for r in history if r.status in ("queued", "running")),
            "questions": open_q,
        }
        # Local state only (no fetch): enough to warn that the checkout is stale before
        # anyone reads a review of it. The run itself fetches.
        try:
            repos = await asyncio.wait_for(asyncio.to_thread(
                review_snapshot.inspect,
                current.directory if current else c.config.workspace_directory), timeout=15)
        except Exception:  # noqa: BLE001 — the hub renders without it
            repos = []
        response = _TEMPLATES.TemplateResponse(request, "review.html", _ctx(
            request, "review", spec_rows=spec_rows, reports=reports, history=history,
            kpi=kpi, kind_label=_KIND_LABEL, ago=_ago, flash=flash,
            workspace=c.config.workspace_directory, doc_types=rs.DOC_TYPES,
            accept=",".join(review_docs.ALLOWED), safe=c.config.review_safe_mode,
            upload_error=request.query_params.get("upload_error", ""),
            repos=repos, code_source=c.config.review_code_source,
            review_workspaces=wss, current_ws=current,
        ))
        if flash:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/review/start")
    async def review_start(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        kind = str(form.get("kind") or "")
        focus = str(form.get("focus") or "").strip()
        workspace = str(form.get("workspace") or "").strip()
        studio = studio_for(c)
        try:
            if kind == rs.KIND_SPEC:
                target = str(form.get("target") or "")
                if "|" in target:                      # "<workspace id>|<spec path>"
                    workspace, target = target.split("|", 1)
                rid = await studio.start_spec(target, focus=focus, by="dashboard",
                                              doc_type=str(form.get("doc_type") or "spec"),
                                              workspace=workspace)
            elif kind == rs.KIND_REPORT:
                rid = await studio.start_report(int(str(form.get("target") or "0")),
                                                focus=focus, by="dashboard", workspace=workspace)
            else:
                rid = await studio.start_question(str(form.get("question") or ""),
                                                  by="dashboard", workspace=workspace)
        except (ValueError, TypeError) as exc:
            _log.info("review not started", error=describe_exc(exc))
            return _flash("/dashboard/review", "review_bad_target")
        await c.audit_repo.record(actor="dashboard", source="dashboard", action="review.start",
                                  target=f"review #{rid}", detail=kind)
        return RedirectResponse(f"/dashboard/review/{rid}", status_code=303)

    @router.post("/review/upload")
    async def review_upload(request: Request):
        """A document a BA brings (PRD, BRD, SRS…) — extracted, stored as text, reviewed."""
        c: Container = request.app.state.container
        form = await request.form()
        up = form.get("file")
        if up is None or not hasattr(up, "read"):
            return RedirectResponse("/dashboard/review?upload_error=" + quote("Chưa chọn file."),
                                    status_code=303)
        data = await up.read(review_docs.MAX_BYTES + 1)
        raw_item = str(form.get("item_id") or "").strip().lstrip("#")
        try:
            rid = await studio_for(c).start_document(
                up.filename or "tai-lieu", data, doc_type=str(form.get("doc_type") or "brd"),
                focus=str(form.get("focus") or "").strip(),
                item_id=int(raw_item) if raw_item.isdigit() else 0, by="dashboard",
                workspace=str(form.get("workspace") or "").strip())
        except review_docs.DocumentError as exc:
            return RedirectResponse("/dashboard/review?upload_error=" + quote(str(exc)),
                                    status_code=303)
        await c.audit_repo.record(actor="dashboard", source="dashboard", action="review.upload",
                                  target=f"review #{rid}", detail=(up.filename or "")[:200])
        return RedirectResponse(f"/dashboard/review/{rid}", status_code=303)

    @router.post("/review/sweep")
    async def review_sweep(request: Request):
        c: Container = request.app.state.container
        ids = await studio_for(c).review_all_specs(by="dashboard")
        await c.audit_repo.record(actor="dashboard", source="dashboard", action="review.sweep",
                                  target=f"{len(ids)} spec", detail="")
        return _flash("/dashboard/review", "review_sweep" if ids else "review_bad_target")

    @router.get("/review/{rid}", response_class=HTMLResponse)
    async def review_detail(request: Request, rid: int):
        c: Container = request.app.state.container
        row = await c.review_repo.get(rid)
        if row is None:
            return RedirectResponse("/dashboard/review", status_code=303)
        flash = _take_flash(request)
        result = rs.load(row.result_json)
        prev = await c.review_repo.previous(row.kind, row.target, rid)
        messages = await c.review_repo.messages(rid)
        changes = (rs.diff(rs.load(prev.result_json), result, row.kind)
                   if prev is not None and row.status == "done" else None)
        response = _TEMPLATES.TemplateResponse(request, "review_detail.html", _ctx(
            request, "review", row=row, result=result, prev=prev, messages=messages,
            statuses=rs.statuses_for(row.kind), counts=result.counts(row.kind),
            gaps=set(rs.gaps(result, row.kind)), kind_label=_KIND_LABEL, ago=_ago,
            md=markdown_lite.render, flash=flash, doc_types=rs.DOC_TYPES,
            snapshot=review_snapshot.from_json(row.snapshot_json or ""),
            ws_label=next((w.label for w in studio_for(c).workspaces()
                           if w.id == (row.workspace or "")), ""),
            sides=rs.SIDES, confidence=rs.CONFIDENCE, doc_issues=rs.DOC_ISSUES,
            groups=rs.by_kind(result) if row.kind == rs.KIND_SPEC else [], changes=changes,
            started_iso=(row.started_at.replace(tzinfo=UTC) if row.started_at.tzinfo is None
                         else row.started_at).isoformat(),
            safe=c.config.review_safe_mode, gap_tag=c.config.review_gap_tag or 'review-gap',
            answer_html=markdown_lite.render(result.answer) if result.answer else "",
            analysis_html=markdown_lite.render(row.analysis_md) if row.analysis_md else "",
        ))
        if flash:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.get("/review/{rid}/status")
    async def review_status(request: Request, rid: int):
        """What the open page polls while a review or an answer is still running."""
        c: Container = request.app.state.container
        row = await c.review_repo.get(rid)
        if row is None:
            return JSONResponse({"status": "missing"}, status_code=404)
        pending = [m.id for m in await c.review_repo.messages(rid) if m.status == "running"]
        return {"status": row.status, "pending": pending}

    @router.post("/review/{rid}/challenge")
    async def review_challenge(request: Request, rid: int):
        """Dispute one matrix row — the agent re-checks it and accepts or rejects."""
        c: Container = request.app.state.container
        form = await request.form()
        # The matrix is one form (its checkboxes are also named "row"), so each row's
        # challenge button carries its own index and its note field is note_<index>.
        raw = str(form.get("challenge_row") or form.get("row") or "")
        note = str(form.get(f"note_{raw}") or form.get("note") or "")
        try:
            await studio_for(c).challenge(rid, int(raw) if raw.isdigit() else -1, note,
                                          by="dashboard")
        except ValueError:
            return _flash(f"/dashboard/review/{rid}", "review_bad_target")
        await c.audit_repo.record(actor="dashboard", source="dashboard",
                                  action="review.challenge", target=f"review #{rid} row {raw}",
                                  detail=note[:200])
        return RedirectResponse(f"/dashboard/review/{rid}#chat", status_code=303)

    @router.post("/review/{rid}/ask")
    async def review_ask(request: Request, rid: int):
        c: Container = request.app.state.container
        form = await request.form()
        try:
            await studio_for(c).ask(rid, str(form.get("question") or ""), by="dashboard")
        except ValueError:
            return _flash(f"/dashboard/review/{rid}#chat", "review_bad_target")
        return RedirectResponse(f"/dashboard/review/{rid}#chat", status_code=303)

    @router.post("/review/{rid}/rerun")
    async def review_rerun(request: Request, rid: int):
        c: Container = request.app.state.container
        try:
            new = await studio_for(c).rerun(rid, by="dashboard")
        except (ValueError, IndexError):
            return _flash(f"/dashboard/review/{rid}", "review_bad_target")
        return RedirectResponse(f"/dashboard/review/{new}", status_code=303)

    @router.get("/review/{rid}/report.html", response_class=HTMLResponse)
    async def review_report(request: Request, rid: int):
        """A standalone, print-ready report — open it and Ctrl+P → Save as PDF.

        Self-contained (inline CSS, no dashboard chrome, no scripts), so it can be saved
        and mailed as one file. ``audience=external`` is the copy for BA/customers: no
        file paths, no line numbers, and a header that says so.
        """
        c: Container = request.app.state.container
        row = await c.review_repo.get(rid)
        if row is None:
            return RedirectResponse("/dashboard/review", status_code=303)
        external = request.query_params.get("audience") == "external"
        result = rs.load(row.result_json)
        if external:
            result = rs.external_copy(result)
        prev = await c.review_repo.previous(row.kind, row.target, rid)
        chat = [(m.role, m.content) for m in await c.review_repo.messages(rid)
                if m.status == "done" and m.content]
        snapshot = review_snapshot.from_json(row.snapshot_json or "")
        return _TEMPLATES.TemplateResponse(request, "review_report.html", {
            "snapshot": [] if external else snapshot, "sides": rs.SIDES,
            "doc_issues": rs.DOC_ISSUES,
            "row": row, "result": result, "external": external, "prev": prev,
            "statuses": rs.statuses_for(row.kind), "counts": result.counts(row.kind),
            "groups": rs.by_kind(result) if row.kind == rs.KIND_SPEC else [],
            "doc_types": rs.DOC_TYPES, "kind_label": _KIND_LABEL,
            "chat": [(r, markdown_lite.render(
                rs.hide_locations(rs.redact(t)) if external else rs.redact(t))) for r, t in chat],
            "answer_html": markdown_lite.render(result.answer) if result.answer else "",
            "now": datetime.now(UTC).strftime("%d/%m/%Y %H:%M UTC"),
            "source": row.target.split(":", 1)[1] if row.target.startswith("upload:")
                      else row.target,
        })

    @router.get("/review/{rid}/export.md")
    async def review_export(request: Request, rid: int):
        c: Container = request.app.state.container
        row = await c.review_repo.get(rid)
        if row is None:
            return PlainTextResponse("not found", status_code=404)
        when = row.finished_at.strftime("%Y-%m-%d %H:%M") if row.finished_at else ""
        external = request.query_params.get("audience") == "external"
        chat = [(m.role, m.content) for m in await c.review_repo.messages(rid)
                if m.status == "done" and m.content]
        source = row.target.split(":", 1)[1] if row.target.startswith("upload:") else row.target
        md = rs.render_markdown(
            row.title, row.kind, rs.load(row.result_json),
            source="" if external and row.kind != rs.KIND_QUESTION else source,
            when=when, chat=chat, external=external)
        name = f"review-{rid}{'-chia-se' if external else ''}.md"
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{name}"'})

    @router.post("/review/{rid}/file")
    async def review_file(request: Request, rid: int):
        """Selected matrix rows → work items (one each, or one combined)."""
        c: Container = request.app.state.container
        cfg = c.config
        row = await c.review_repo.get(rid)
        if row is None:
            return RedirectResponse("/dashboard/review", status_code=303)
        form = await request.form()
        picked = sorted({int(v) for v in form.getlist("row") if str(v).isdigit()})
        item_type = str(form.get("item_type") or "Task").strip() or "Task"
        mode = "one" if str(form.get("mode")) == "one" else "each"
        result = rs.load(row.result_json)
        chosen = [i for i in picked if i < len(result.traces) and not result.traces[i].filed_as]
        if not chosen:
            return _flash(f"/dashboard/review/{rid}", "file_none_picked")
        parent = row.work_item_id or None
        link = _link(request, rid)
        created: list[int] = []
        groups = [chosen] if mode == "one" else [[i] for i in chosen]
        for group in groups:
            if len(group) == 1:
                title, body = rs.trace_work_item(row.title, result.traces[group[0]], row.kind,
                                                 link=link)
            else:
                title = f"[Rà soát] {len(group)} mục cần xử lý — {row.title}"[:250]
                body = "".join(rs.trace_work_item(row.title, result.traces[i], row.kind)[1]
                               + "<hr/>" for i in group) + f"<a href='{link}'>Mở phiên rà soát</a>"
            try:
                new_id = await c.ado.create_work_item(
                    title=title, item_type=item_type, parent_id=parent,
                    tag=cfg.review_gap_tag or "review-gap", description=body)
            except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
                _log.warning("review filing failed", error=describe_exc(exc))
                new_id = 0
            if new_id:
                created.append(new_id)
                for i in group:
                    result.traces[i].filed_as = new_id
        await c.review_repo.update(rid, result_json=result.to_json())
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="review.filed",
            target=f"review #{rid} → {', '.join(f'#{i}' for i in created)}"[:300],
            detail=item_type)
        if not created:
            return _flash(f"/dashboard/review/{rid}", "file_failed")
        return _flash(f"/dashboard/review/{rid}",
                      "file_partial" if len(created) < len(groups) else "file_created")

    @router.post("/review/{rid}/comment")
    async def review_comment(request: Request, rid: int):
        """Post the 4-part review summary on the spec's work item — where BA and QC read."""
        c: Container = request.app.state.container
        row = await c.review_repo.get(rid)
        if row is None:
            return RedirectResponse("/dashboard/review", status_code=303)
        form = await request.form()
        raw = str(form.get("item_id") or row.work_item_id or "").strip().lstrip("#")
        if not raw.isdigit():
            return _flash(f"/dashboard/review/{rid}", "review_no_item")
        item_id = int(raw)
        html = rs.render_ado_comment(row.title, rs.load(row.result_json), link=_link(request, rid))
        try:
            ok = await c.ado.add_comment(item_id, html)
        except Exception as exc:  # noqa: BLE001
            _log.warning("review comment failed", error=describe_exc(exc))
            ok = False
        if not ok:
            return _flash(f"/dashboard/review/{rid}", "review_comment_failed")
        await c.review_repo.update(rid, commented_on=item_id)
        await c.audit_repo.record(actor="dashboard", source="dashboard", action="review.comment",
                                  target=f"review #{rid} → #{item_id}", detail="")
        return _flash(f"/dashboard/review/{rid}", "review_commented")

    @router.get("/review/{rid}/result.json")
    async def review_json(request: Request, rid: int):
        c: Container = request.app.state.container
        row = await c.review_repo.get(rid)
        if row is None:
            return JSONResponse({}, status_code=404)
        return JSONResponse({"id": rid, "kind": row.kind, "status": row.status,
                             "score": row.score, "result": json.loads(row.result_json or "{}")})

    return router
