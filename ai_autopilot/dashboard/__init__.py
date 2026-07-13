"""Server-rendered dashboard (replaces the Blazor dashboard)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ai_autopilot import activity
from ai_autopilot.board import board_columns, build_board, latest_records, parse_drop_map
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.logging_config import get_logger
from ai_autopilot.services import planning_analyzer
from ai_autopilot.skills_catalog import discover_skills
from ai_autopilot.workspace import discover_repos

_log = get_logger("dashboard")

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_CATEGORY_LABELS = {
    "backendtask": ("BE", "cat-be"),
    "frontendtask": ("FE", "cat-fe"),
    "bug": ("Bug", "cat-bug"),
    "databasetask": ("DB", "cat-db"),
    "testtask": ("QC", "cat-qc"),
    "requirement": ("Req", "cat-req"),
}
_STATUS_CLASS = {
    "Success": "badge-success",
    "Failed": "badge-failed",
    "Running": "badge-running",
}
# Baseline work-item types offered in the Planning filter (merged with whatever
# types the loaded items actually have, so custom process types show up too).
_COMMON_WI_TYPES = ("Bug", "Task", "User Story", "Requirement", "Feature", "Test Case")


def _category_badge(category: str) -> tuple[str, str]:
    label, css = _CATEGORY_LABELS.get((category or "").lower(), (category or "—", "cat-default"))
    return label, css


def _status_class(status: str) -> str:
    return _STATUS_CLASS.get(status, "badge-retrying")


def _mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def create_dashboard_router() -> APIRouter:
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])

    def _ctx(request: Request, active: str, **extra) -> dict:
        return {
            "request": request,
            "active": active,
            "mmss": _mmss,
            "fmt_duration": _fmt_duration,
            "category_badge": _category_badge,
            "status_class": _status_class,
            **extra,
        }

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        c: Container = request.app.state.container
        selected_tag = request.query_params.get("tag") or "all"
        tag_filter = None if selected_tag == "all" else selected_tag
        stats = await c.execution_repo.get_stats(trigger_tag=tag_filter)
        recent = await c.execution_repo.get_recent(20, trigger_tag=tag_filter)
        return _TEMPLATES.TemplateResponse(
            request,
            "overview.html",
            _ctx(
                request,
                "overview",
                stats=stats,
                recent=recent,
                tags=c.config.effective_trigger_tags,
                selected_tag=selected_tag,
            ),
        )

    async def _board_ctx(request: Request) -> dict:
        c: Container = request.app.state.container
        try:
            items = await c.ado.get_all_tagged_work_items()
            error = None
        except Exception as exc:  # noqa: BLE001
            items, error = [], str(exc)
        tags = c.config.effective_trigger_tags
        selected_tag = request.query_params.get("tag") or "all"
        if selected_tag != "all":
            sel = selected_tag.lower()
            items = [i for i in items if any(t.lower() == sel for t in i.tags)]
        records = await c.execution_repo.get_recent(200)
        states = {s.work_item_id: s.state.value for s in await c.state_repo.all()}
        cols = build_board(items, latest_records(records), c.config, states)
        org = c.config.ado_organization.rstrip("/")
        proj = c.config.ado_project
        ado_item_base = f"{org}/{quote(proj)}/_workitems/edit" if org and proj else ""
        return _ctx(
            request,
            "board",
            board=cols,
            columns=board_columns(c.config),
            total=len(items),
            error=error,
            project=c.config.ado_project,
            interactive=c.config.execution_mode == "interactive",
            tags=tags,
            selected_tag=selected_tag,
            ado_item_base=ado_item_base,
        )

    @router.get("/board", response_class=HTMLResponse)
    async def board(request: Request):
        return _TEMPLATES.TemplateResponse(request, "board.html", await _board_ctx(request))

    async def _planning_ctx(request: Request, **over) -> dict:
        """Common context for the Planning workbench page (+ its POST re-renders)."""
        c: Container = request.app.state.container
        cfg = c.config
        org = cfg.ado_organization.rstrip("/")
        proj = cfg.ado_project
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
            ado_item_base=f"{org}/{quote(proj)}/_workitems/edit" if org and proj else "",
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

    async def _planning_load(c: Container, assignee: str, state: str, wtype: str) -> list:
        if not assignee:
            return []
        states = None if state in ("", "all") else [state]
        types = None if wtype in ("", "all") else [wtype]
        try:
            return await c.ado.get_work_items_by_assignee(
                assignee, states, types, top=c.config.planning_load_limit
            )
        except Exception:  # noqa: BLE001
            return []

    _FILTER_COOKIE = "planning_filter"

    def _restore_filter(request: Request, assignee: str, state: str, wtype: str
                        ) -> tuple[str, str, str]:
        """When the request carries no explicit filter, restore the last one from the
        cookie so the Planning view survives across sessions."""
        if any(k in request.query_params for k in ("assignee", "state", "type")):
            return assignee, state, wtype
        parsed = parse_qs(request.cookies.get(_FILTER_COOKIE, ""))
        if not parsed:
            return assignee, state, wtype
        return (
            (parsed.get("assignee") or [assignee])[0],
            (parsed.get("state") or [state])[0],
            (parsed.get("type") or [wtype])[0],
        )

    @router.get("/planning", response_class=HTMLResponse)
    async def planning_page(
        request: Request, assignee: str = "", state: str = "all", type: str = "all",
        started: int = 0, scheduled: int = 0,
    ):
        c: Container = request.app.state.container
        assignee, state, type = _restore_filter(request, assignee, state, type)
        who = assignee or c.config.auto_transition_assignee
        loaded = await _planning_load(c, who, state, type)
        ctx = await _planning_ctx(
            request, loaded=loaded, assignee=who, state_filter=state, type_filter=type,
            started=started, scheduled_n=scheduled,
        )
        resp = _TEMPLATES.TemplateResponse(request, "planning.html", ctx)
        # Remember the active filter for next time (30 days).
        resp.set_cookie(
            _FILTER_COOKIE,
            urlencode({"assignee": who, "state": state, "type": type}),
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
        assignee = str(form.get("assignee", "")) or c.config.auto_transition_assignee
        state = str(form.get("state", "all"))
        wtype = str(form.get("type", "all"))
        analysis = await planning_analyzer.analyze(c, ids) if ids else None
        loaded = await _planning_load(c, assignee, state, wtype)
        ctx = await _planning_ctx(
            request, loaded=loaded, selected=set(ids), analysis=analysis,
            assignee=assignee, state_filter=state, type_filter=wtype,
        )
        return _TEMPLATES.TemplateResponse(request, "planning.html", ctx)

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
                await c.planned_run_repo.create(ids, run_at, note=f"{len(ids)} item(s)")
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

    @router.get("/board/partial", response_class=HTMLResponse)
    async def board_partial(request: Request):
        """Just the columns — fetched by the page's auto-refresh, no full reload."""
        return _TEMPLATES.TemplateResponse(request, "_board_cols.html", await _board_ctx(request))

    @router.post("/board/move")
    async def board_move(request: Request):
        """Drag & drop: apply the configured tag/state for the target column."""
        c: Container = request.app.state.container
        form = await request.form()
        try:
            item_id = int(str(form.get("item_id", "")))
        except ValueError:
            return Response(status_code=204)
        column = str(form.get("column", "")).strip().lower()
        dmap = parse_drop_map(c.config.board_drop_map)
        action = dmap.get(column)
        if not item_id or action is None or c.config.dry_run:
            return Response(status_code=204)
        kind, value = action
        if kind == "state":
            await c.ado.update_state(item_id, value)
        else:  # tag — set exclusively among the configured drop-tags
            managed = {v for (k, v) in dmap.values() if k == "tag"}
            item = await c.ado.get_work_item(item_id)
            if item is not None:
                for tag in item.tags:
                    if tag in managed and tag != value:
                        await c.ado.remove_tag(item_id, tag)
            await c.ado.add_tag(item_id, value)
        _log.info("board move", id=item_id, column=column, action=action)
        return Response(status_code=204)

    @router.get("/activity/{item_id}", response_class=HTMLResponse)
    async def activity_view(request: Request, item_id: int):
        c: Container = request.app.state.container
        feed = activity.read(c.config.workspace_directory, item_id)
        return _TEMPLATES.TemplateResponse(
            request, "activity.html", _ctx(request, "board", item_id=item_id, feed=feed)
        )

    @router.get("/activity/{item_id}/partial", response_class=PlainTextResponse)
    async def activity_partial(request: Request, item_id: int):
        c: Container = request.app.state.container
        return PlainTextResponse(
            activity.read(c.config.workspace_directory, item_id) or "(no activity yet — waiting for the agent…)"
        )

    @router.get("/history", response_class=HTMLResponse)
    async def history(request: Request):
        c: Container = request.app.state.container
        records = await c.execution_repo.get_recent(100)
        return _TEMPLATES.TemplateResponse(
            request, "history.html", _ctx(request, "history", records=records)
        )

    @router.post("/history/{record_id}/delete")
    async def delete_history(request: Request, record_id: int):
        c: Container = request.app.state.container
        await c.execution_repo.delete(record_id)
        _log.info("execution record deleted via dashboard", record_id=record_id)
        return RedirectResponse(url="/dashboard/history", status_code=303)

    @router.post("/history/clear")
    async def clear_history(request: Request):
        c: Container = request.app.state.container
        removed = await c.execution_repo.clear_all()
        _log.info("execution history cleared via dashboard", removed=removed)
        return RedirectResponse(url="/dashboard/history", status_code=303)

    @router.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        c: Container = request.app.state.container
        return _TEMPLATES.TemplateResponse(
            request, "config.html", _ctx(request, "config", cfg=c.config)
        )

    @router.get("/capabilities", response_class=HTMLResponse)
    async def capabilities(request: Request):
        c: Container = request.app.state.container
        skills = discover_skills(c.config.workspace_directory)
        return _TEMPLATES.TemplateResponse(
            request,
            "capabilities.html",
            _ctx(
                request,
                "capabilities",
                skills=skills,
                workspace=c.config.workspace_directory,
                ai_native=bool(c.config.workspace_directory),
            ),
        )

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(
        request: Request,
        saved: int = 0,
        reloaded: int = 0,
        imported: int = 0,
        import_error: str = "",
    ):
        c: Container = request.app.state.container
        current = {
            f.key: getattr(c.config, f.key, "")
            for f in settings_form.FIELDS
            if f.key not in settings_form.SECRET_KEYS
        }
        has_pat = bool(getattr(c.config, "ado_pat", ""))
        cfg = c.config
        # Read-only overview of every ADO tag the autopilot writes/reads — so the
        # whole tag vocabulary is visible in one place (not scattered across fields).
        tag_overview = [
            {"label": "Trigger", "cls": "chip-accent",
             "tags": cfg.effective_trigger_tags, "hint": "items with these get processed"},
            {"label": "Review", "cls": "chip-amber",
             "tags": [cfg.review_tag], "hint": "draft PR opened, awaiting review"},
            {"label": "Done", "cls": "chip-green",
             "tags": [cfg.processed_tag], "hint": "handled (also report/failed unless overridden)"},
            {"label": "Needs human", "cls": "chip-red",
             "tags": [cfg.escalation_tag], "hint": "escalated & held"},
            {"label": "Live", "cls": "chip-blue",
             "tags": [cfg.live_tag], "hint": "interactive session running"},
            {"label": "Failed", "cls": "chip",
             "tags": [cfg.failed_tag or f"{cfg.processed_tag} (Done tag)"],
             "hint": "gave up after retries"},
        ]
        discovered = discover_repos(c.config.workspace_directory)
        allowed = {r.lower() for r in c.config.allowed_repos}
        try:
            ado_states = await c.ado.get_states()
        except Exception:  # noqa: BLE001 — Settings must render even if ADO is down
            ado_states = []
        return _TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                "settings",
                sections=settings_form.sections(),
                current=current,
                has_pat=has_pat,
                restart_keys=settings_form.RESTART_REQUIRED,
                saved=bool(saved),
                reloaded=bool(reloaded),
                imported=bool(imported),
                import_error=import_error,
                config_path=str(config_file_path()),
                repos=discovered,
                allowed_repos=allowed,
                ado_states=ado_states,
                tag_overview=tag_overview,
            ),
        )

    @router.post("/settings")
    async def save_settings(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        updates = settings_form.parse_form(form)
        updates["allowed_repos"] = settings_form.parse_repos(form)

        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()  # re-read org URL if it changed
        _log.info("settings updated via dashboard", keys=sorted(updates.keys()))

        return RedirectResponse(url="/dashboard/settings?saved=1", status_code=303)

    @router.post("/settings/reload")
    async def reload_settings(request: Request):
        """Re-read config.yaml from disk into the running app (no restart)."""
        c: Container = request.app.state.container
        changed = settings_form.reload_from_file(c.config)
        c.ado.refresh()  # re-read org URL if it changed
        _log.info("config reloaded from file via dashboard", changed=changed)
        return RedirectResponse(url="/dashboard/settings?reloaded=1", status_code=303)

    @router.get("/settings/export")
    async def export_config(request: Request):
        """Download the current config as YAML, minus secrets + machine-specific keys."""
        c: Container = request.app.state.container
        body = settings_form.export_yaml(c.config)
        _log.info("config exported via dashboard")
        return Response(
            content=body,
            media_type="application/x-yaml",
            headers={"Content-Disposition": 'attachment; filename="autopilot-config.yaml"'},
        )

    @router.post("/settings/import")
    async def import_config(request: Request):
        """Apply an uploaded YAML config (shared by a teammate). PAT is never imported."""
        c: Container = request.app.state.container
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return RedirectResponse("/dashboard/settings?import_error=No+file", status_code=303)
        raw = (await upload.read()).decode("utf-8", errors="replace")
        try:
            updates = settings_form.import_settings(raw, set(type(c.config).model_fields))
        except (ValueError, yaml.YAMLError) as exc:
            _log.warning("config import failed", error=str(exc))
            return RedirectResponse("/dashboard/settings?import_error=Invalid+file", status_code=303)
        if not updates:
            return RedirectResponse("/dashboard/settings?import_error=Nothing+to+import", status_code=303)
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()
        _log.info("config imported via dashboard", keys=sorted(updates.keys()))
        return RedirectResponse("/dashboard/settings?imported=1", status_code=303)

    return router


def files_changed_count(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return len(json.loads(raw))
    except (ValueError, TypeError):
        return 0
