"""Compliance evidence pack — preview and download one period's evidence as Markdown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from ai_autopilot import __version__, evidence_pack, flaky_tests, markdown_lite
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES
from ai_autopilot.dashboard.routes._shared import _ctx

WINDOWS = (7, 14, 30, 90)


async def build(c: Container, days: int, now: datetime) -> str:
    start = now - timedelta(days=days)
    ws = c.config.workspace_directory or ""
    ev = evidence_pack.EvidenceInput(
        start=start, end=now, version=__version__,
        runs=await c.execution_repo.get_recent(2000),
        scans=await c.security_repo.recent_scans(limit=500),
        open_findings=await c.security_repo.list_findings(status="open", limit=5000),
        suppressed=await c.security_repo.list_findings(status="suppressed", limit=500),
        audit=await c.audit_repo.recent(limit=5000),
        flaky=flaky_tests.load(ws),
        sbom_files=evidence_pack.sbom_files(ws),
    )
    return evidence_pack.render(ev)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/evidence", response_class=HTMLResponse)
    async def evidence_page(request: Request, days: int = 30):
        c: Container = request.app.state.container
        days = days if days in WINDOWS else 30
        md = await build(c, days, datetime.now(UTC))
        return _TEMPLATES.TemplateResponse(
            request, "evidence.html",
            _ctx(request, "evidence", days=days, windows=WINDOWS,
                 preview=markdown_lite.render(md)))

    @router.get("/evidence/download")
    async def evidence_download(request: Request, days: int = 30):
        c: Container = request.app.state.container
        days = days if days in WINDOWS else 30
        now = datetime.now(UTC)
        md = await build(c, days, now)
        name = f"evidence-{(now - timedelta(days=days)):%Y%m%d}-{now:%Y%m%d}.md"
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return router
