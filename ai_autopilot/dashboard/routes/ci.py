"""CI/CD page — the red builds of the last days, sorted into causes with a next step."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import ci_triage
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.logging_config import describe_exc, get_logger

WINDOWS = (3, 7, 14, 30)
# Each red build costs two more reads (timeline + log); bound both the count and the
# concurrency so opening the page cannot fan out into hundreds of ADO calls.
MAX_TRIAGED = 25
_CONCURRENCY = 6
_log = get_logger("dashboard.ci")


async def gather(ado, days: int, now: datetime) -> ci_triage.CiReport:
    report = ci_triage.CiReport(days=days)
    since = now - timedelta(days=days)
    try:
        builds = await ado.get_builds_since(since, top=500)
    except Exception as exc:  # noqa: BLE001
        _log.warning("ci: builds read failed", error=describe_exc(exc))
        builds = None
    if builds is None:
        report.status = "unknown"
        report.reason = "Không đọc được danh sách build từ ADO (kiểm PAT có quyền Build: Read)."
        return report
    report.total_builds = len(builds)
    red = [b for b in builds if str(b.get("result") or "").lower() == "failed"]
    green = [b for b in builds if str(b.get("result") or "").lower() == "succeeded"]
    failed = [ci_triage.from_build(b) for b in red[:MAX_TRIAGED]]
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def triage(f: ci_triage.FailedBuild) -> None:
        async with gate:
            timeline = await ado.get_build_timeline(f.build_id)
            f.failed_step, f.errors, log_id = ci_triage.failing_record(timeline)
            if log_id is not None:
                f.log_tail = await ado.get_build_log_tail(f.build_id, log_id)
        f.category, f.hint, f.evidence = ci_triage.classify(f.failed_step, f.errors, f.log_tail)

    results = await asyncio.gather(*(triage(f) for f in failed), return_exceptions=True)
    for f, res in zip(failed, results, strict=True):
        if isinstance(res, BaseException):
            f.hint = f"Không đọc được chi tiết build: {describe_exc(res)}"
    ci_triage.mark_flaky(failed, green)
    report.failed = failed
    # Counted over everything red, not just the triaged head of the list.
    report.reason = (f"Chỉ phân tích {MAX_TRIAGED} build lỗi gần nhất / {len(red)}."
                     if len(red) > MAX_TRIAGED else "")
    return report


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/ci", response_class=HTMLResponse)
    async def ci_page(request: Request, days: int = 7):
        c: Container = request.app.state.container
        days = days if days in WINDOWS else 7
        configured = bool(c.config.ado_organization and c.config.ado_pat)
        report = await gather(c.ado, days, datetime.now(UTC)) if configured else None
        return _TEMPLATES.TemplateResponse(
            request, "ci.html",
            _ctx(request, "ci", days=days, windows=WINDOWS, report=report,
                 configured=configured, labels=ci_triage.LABELS),
        )

    return router
