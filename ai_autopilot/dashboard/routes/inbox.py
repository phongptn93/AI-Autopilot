"""The decision inbox page and the sidebar's red-count badge.

The aggregation lives in :mod:`ai_autopilot.inbox` (no HTTP in it, so it is tested
with a bare container); this module only renders it and filters by source.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ai_autopilot import inbox as inbox_mod
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES, FLASH_MESSAGES, _flash, _take_flash
from ai_autopilot.dashboard.routes._shared import _ctx

#: How long the sidebar badge's numbers may be reused. Every page load asks for them,
#: and one count reads six tables plus the lessons files — on a busy dashboard that is
#: the same work several times a second for a number that changes a few times an
#: hour. Short enough that a decision just made does not leave a stale badge for long;
#: the inbox page itself always recomputes and refreshes this cache.
_COUNT_TTL_SECONDS = 15.0


FLASH_MESSAGES.update({
    "inbox_plan_approved": ("green", "✅ Đã duyệt kế hoạch — autopilot sẽ triển khai ở vòng "
                                     "kế tiếp, theo đúng kế hoạch trong comment."),
    "inbox_plan_failed": ("red", "⛔ Không gắn được tag duyệt kế hoạch lên work item — "
                                 "kiểm tra kết nối Azure DevOps."),
    "inbox_resumed": ("green", "▶ Máy đã tiếp tục nhận việc."),
    "inbox_dry_run": ("amber", "Máy đang <code>dry_run</code> — không ghi gì lên tracker."),
})


def _machine_entries(request: Request) -> list:
    """This machine stopped taking work on its own (circuit breaker) or by hand.

    Not a database row — the poller holds it in memory — so it is read here rather
    than in ``inbox.gather``. A paused machine is the one decision that blocks every
    other item, so it is always red.
    """
    poller = getattr(request.app.state, "poller", None)
    if poller is None or not getattr(poller, "paused", False):
        return []
    reason = getattr(poller, "paused_reason", "") or "tạm dừng"
    return [inbox_mod.InboxEntry(
        source="machine", tier=inbox_mod.RED, icon="⏸",
        title="Máy này đang tạm dừng nhận việc",
        context=f"Lý do: {reason}. Run đang chạy vẫn chạy tiếp; item mới không được nhận.",
        at=None, href="/dashboard/now",
        actions=[inbox_mod.InboxAction(
            "▶ Tiếp tục nhận việc", "/dashboard/inbox/resume", "post", {},
            confirm="Cho máy này nhận việc lại?")],
    )]


async def _gather(request: Request) -> inbox_mod.Inbox:
    inbox = await inbox_mod.gather(request.app.state.container)
    inbox.entries[:0] = _machine_entries(request)
    return inbox


def _remember_counts(request: Request, inbox: inbox_mod.Inbox) -> dict:
    counts = {"red": inbox.count(inbox_mod.RED), "total": len(inbox.entries)}
    request.app.state.inbox_counts = (time.monotonic(), counts)
    return counts


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/inbox", response_class=HTMLResponse)
    async def inbox_page(request: Request, src: str = ""):
        """Every pending human decision, most urgent first, each with its action inline."""
        inbox = await _gather(request)
        _remember_counts(request, inbox)
        flash = _take_flash(request)
        src = src if src in inbox_mod.SOURCES else ""
        per_source = {k: sum(1 for e in inbox.entries if e.source == k)
                      for k in inbox_mod.SOURCES}
        shown = [e for e in inbox.entries if not src or e.source == src]
        tiers = [
            (tier, *inbox_mod.TIERS[tier], [e for e in shown if e.tier == tier])
            for tier in inbox_mod.TIER_ORDER
        ]
        return _TEMPLATES.TemplateResponse(
            request, "inbox.html",
            _ctx(request, "inbox", tiers=tiers, src=src, sources=inbox_mod.SOURCES, flash=flash,
                 per_source=per_source, failed=inbox.failed,
                 counts={t: inbox.count(t) for t in inbox_mod.TIER_ORDER},
                 total=len(inbox.entries), shown_total=len(shown)),
        )

    @router.get("/inbox/count.json")
    async def inbox_count(request: Request):
        """``{"red": n, "total": m}`` for the sidebar badge, fetched once per page load."""
        cached = getattr(request.app.state, "inbox_counts", None)
        if cached and time.monotonic() - cached[0] < _COUNT_TTL_SECONDS:
            counts = cached[1]
        else:
            counts = _remember_counts(request, await _gather(request))
        return JSONResponse(counts, headers={"Cache-Control": "no-store"})

    @router.post("/inbox/approve-plan")
    async def approve_plan(request: Request):
        """Approve an item's posted plan: add the approved-plan tag; the poller does the rest."""
        c: Container = request.app.state.container
        form = await request.form()
        try:
            item_id = int(str(form.get("item_id", "")))
        except ValueError:
            return RedirectResponse("/dashboard/inbox", status_code=303)
        if c.config.dry_run:
            return _flash("/dashboard/inbox", "inbox_dry_run")
        tag = (getattr(c.config, "plan_approved_tag", "") or "plan-approved").strip()
        try:
            ok = await c.ado.add_tag(item_id, tag)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            return _flash("/dashboard/inbox", "inbox_plan_failed")
        await c.audit_repo.record(actor="dashboard", source="dashboard",
                                  action="plan.approved", target=f"#{item_id}")
        request.app.state.inbox_counts = None
        return _flash("/dashboard/inbox", "inbox_plan_approved")

    @router.post("/inbox/resume")
    async def resume_machine(request: Request):
        """Lift a pause (the circuit breaker's or anyone's) on this machine's poller."""
        c: Container = request.app.state.container
        poller = getattr(request.app.state, "poller", None)
        if poller is not None:
            was = getattr(poller, "paused_reason", "")
            poller.paused, poller.paused_reason = False, ""
            await c.audit_repo.record(actor="dashboard", source="dashboard",
                                      action="machine.resumed", detail=was[:300])
        request.app.state.inbox_counts = None
        return _flash("/dashboard/inbox", "inbox_resumed")

    return router
