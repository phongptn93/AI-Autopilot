"""Planning workbench: load, analyze, start or cancel a planned run."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import (
    _COMMON_WI_TYPES,
    _TEMPLATES,
    scope_of,
    work_item_link_base,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.services import planning_analyzer


async def _planning_ctx(request: Request, **over) -> dict:
    """Common context for the Planning workbench page (+ its POST re-renders)."""
    c: Container = request.app.state.container
    cfg = c.config
    try:
        scheduled = await c.planned_run_repo.list_active()
    except Exception:  # noqa: BLE001
        scheduled = []
    try:
        sched_history = await c.scheduler_history_repo.recent(cfg.scheduler_history_limit or 20)
    except Exception:  # noqa: BLE001
        sched_history = []
    base = dict(
        view=getattr(c, "scheduler_view", None),
        enabled=cfg.dependency_scheduling_enabled,
        sibling=cfg.sibling_conflict_scheduling,
        poll_interval=cfg.poll_interval_seconds,
        ado_item_base=work_item_link_base(cfg),
        trigger_tags={t.lower() for t in cfg.effective_trigger_tags},
        default_assignee=cfg.auto_transition_assignee,
        states=sorted({*cfg.trigger_states, *cfg.done_states}),
        default_when=datetime.now().replace(  # noqa: DTZ005 — local wall-clock for the picker
            hour=max(0, min(23, cfg.planning_schedule_default_hour)),
            minute=0, second=0, microsecond=0,
        ).strftime("%Y-%m-%dT%H:%M"),
        live_refresh=cfg.planning_live_refresh_seconds,
        scheduled=scheduled,
        sched_history=sched_history,
        loaded=[], selected=set(), analysis=None,
        assignee=cfg.auto_transition_assignee, state_filter="all", type_filter="all",
        started=0, scheduled_n=0,
    )
    base.update(over)
    loaded = base.get("loaded") or []
    base["wtypes"] = sorted(
        {*_COMMON_WI_TYPES, *(i.work_item_type for i in loaded if i.work_item_type)}
    )
    return _ctx(request, "planning", **base)


async def _planning_load(
    c: Container, assignee: str, state: str, wtype: str,
    in_scope: list[str] | None = None,
) -> list:
    """Load the work items to plan. A BLANK ``assignee`` is a real choice — the whole
    team's board — not a reason to show nothing (comma-separate names for a subset).

    ``in_scope`` is the selected workspace's projects (``None`` = every project).
    Filtering happens here rather than in the WIQL so the conflict analyser still
    sees one consistent set."""
    states = None if state in ("", "all") else [state]
    types = None if wtype in ("", "all") else [wtype]
    try:
        items = await c.ado.get_work_items_by_assignee(
            assignee, states, types, top=c.config.planning_load_limit
        )
    except Exception:  # noqa: BLE001
        return []
    if in_scope is None:
        return items
    allowed = {p.lower() for p in in_scope}
    return [i for i in items if (i.project or "").lower() in allowed]


_FILTER_COOKIE = "planning_filter"


def _restore_filter(request: Request, assignee: str, state: str, wtype: str,
                    default_assignee: str = "") -> tuple[str, str, str]:
    """Resolve the active filter: explicit query params win, then the cookie from the
    last visit, then this machine's own assignee as the opening default.

    An EMPTY assignee means "everyone", which is a choice a user can make — so it has
    to survive the cookie round-trip (``keep_blank_values``) and must not be silently
    replaced by the default, or clearing the box would snap back to one person.
    """
    if any(k in request.query_params for k in ("assignee", "state", "type")):
        return assignee, state, wtype
    parsed = parse_qs(request.cookies.get(_FILTER_COOKIE, ""), keep_blank_values=True)
    if not parsed:
        return default_assignee or assignee, state, wtype
    return (
        (parsed.get("assignee") or [assignee])[0],
        (parsed.get("state") or [state])[0],
        (parsed.get("type") or [wtype])[0],
    )


def _planning_redirect(form, **extra) -> RedirectResponse:
    """Redirect back to the workbench, preserving the active filter (assignee /
    state / type carried in the POST body) so the loaded list survives the POST."""
    params = {
        "assignee": str(form.get("assignee", "")).strip(),
        "state": str(form.get("state", "all")).strip() or "all",
        "type": str(form.get("type", "all")).strip() or "all",
        **extra,
    }
    # Drop values equal to the GET-route defaults to keep the URL clean.
    query = urlencode(
        {k: v for k, v in params.items() if v not in ("", "all", 0)}
    )
    url = "/dashboard/planning" + (f"?{query}" if query else "")
    return RedirectResponse(url, status_code=303)


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/planning", response_class=HTMLResponse)
    async def planning_page(
        request: Request, assignee: str = "", state: str = "all", type: str = "all",
        started: int = 0, scheduled: int = 0,
    ):
        c: Container = request.app.state.container
        assignee, state, type = _restore_filter(
            request, assignee, state, type, c.config.auto_transition_assignee
        )
        loaded = await _planning_load(c, assignee, state, type, scope_of(request, c.config)[1])
        ctx = await _planning_ctx(
            request, loaded=loaded, assignee=assignee, state_filter=state, type_filter=type,
            started=started, scheduled_n=scheduled,
        )
        resp = _TEMPLATES.TemplateResponse(request, "planning.html", ctx)
        # Remember the active filter for next time (30 days).
        resp.set_cookie(
            _FILTER_COOKIE,
            urlencode({"assignee": assignee, "state": state, "type": type}),
            max_age=60 * 60 * 24 * 30, samesite="lax", httponly=True,
        )
        return resp

    @router.get("/planning/live-partial", response_class=HTMLResponse)
    async def planning_live_partial(request: Request):
        """Just the Live-schedule card — polled by the page for auto-refresh."""
        c: Container = request.app.state.container
        cfg = c.config
        try:
            sched_history = await c.scheduler_history_repo.recent(
                cfg.scheduler_history_limit or 20
            )
        except Exception:  # noqa: BLE001
            sched_history = []
        return _TEMPLATES.TemplateResponse(
            request,
            "planning_live.html",
            {
                "view": getattr(c, "scheduler_view", None),
                "enabled": cfg.dependency_scheduling_enabled,
                "poll_interval": cfg.poll_interval_seconds,
                "sched_history": sched_history,
            },
        )

    @router.post("/planning/analyze", response_class=HTMLResponse)
    async def planning_analyze(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        ids = [int(x) for x in form.getlist("ids") if str(x).strip().isdigit()]
        # Blank = the whole team; do NOT fall back to this machine's assignee or the
        # analysis would silently re-scope to one person after every Analyze click.
        assignee = str(form.get("assignee", ""))
        state = str(form.get("state", "all"))
        wtype = str(form.get("type", "all"))
        analysis = await planning_analyzer.analyze(c, ids) if ids else None
        loaded = await _planning_load(c, assignee, state, wtype, scope_of(request, c.config)[1])
        ctx = await _planning_ctx(
            request, loaded=loaded, selected=set(ids), analysis=analysis,
            assignee=assignee, state_filter=state, type_filter=wtype,
        )
        return _TEMPLATES.TemplateResponse(request, "planning.html", ctx)

    @router.post("/planning/start")
    async def planning_start(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        ids = [int(x) for x in form.getlist("ids") if str(x).strip().isdigit()]
        mode = str(form.get("mode", "now")).strip()
        when_at = str(form.get("when_at", "")).strip()
        if not ids:
            return _planning_redirect(form)
        if mode == "schedule" and when_at:
            try:
                run_at = datetime.fromisoformat(when_at)
            except ValueError:
                run_at = None
            if run_at is not None:
                await c.planned_run_repo.create(ids, run_at, note=f"{len(ids)} item")
                return _planning_redirect(form, scheduled=len(ids))
        n = await planning_analyzer.start_items(c, ids)
        return _planning_redirect(form, started=n)

    @router.post("/planning/cancel")
    async def planning_cancel(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        try:
            run_id = int(str(form.get("run_id", "")))
        except ValueError:
            run_id = 0
        if run_id:
            await c.planned_run_repo.set_status(run_id, "cancelled")
        return _planning_redirect(form)

    return router
