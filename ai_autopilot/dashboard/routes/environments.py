"""Environments & incidents page — what each environment runs, and production incidents."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import environments as env_mod
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES, work_item_link_base
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.services.process_health_service import to_health_item

WINDOWS = (30, 90, 180)
_log = get_logger("dashboard.environments")


async def gather_envs(c: Container, specs: list[env_mod.EnvSpec],
                      now: datetime) -> list[env_mod.EnvView]:
    since = now - timedelta(days=180)

    async def one(spec: env_mod.EnvSpec) -> env_mod.EnvView:
        try:
            builds = await c.ado.get_deploy_builds(spec.pipeline, spec.branch, since)
        except Exception as exc:  # noqa: BLE001
            _log.warning("environments: builds failed", env=spec.name, error=describe_exc(exc))
            builds = None
        ok = None if builds is None else [
            b for b in builds if str(b.get("result") or "").lower() == "succeeded"]
        view = env_mod.env_view(spec, ok)
        if view.current is not None:
            try:
                ids = await c.ado.get_build_work_item_ids(view.current.build_id) or []
                items = await c.ado.get_work_items_by_ids(ids[:50]) if ids else []
                view.items = [(i.id, i.title or "") for i in items]
            except Exception as exc:  # noqa: BLE001 — the build itself is still worth showing
                _log.warning("environments: items failed", env=spec.name,
                             error=describe_exc(exc))
        return view

    return list(await asyncio.gather(*(one(s) for s in specs)))


async def gather_incidents(c: Container, project: str, days: int, now: datetime):
    try:
        raw = await c.ado.get_raw_work_items(project, max(days, 180))
        categories = await c.ado.get_state_categories()
    except Exception as exc:  # noqa: BLE001
        return None, f"Không đọc được work item: {describe_exc(exc)}"
    items = [i for i in (to_health_item(f) for f in raw) if i is not None]
    return env_mod.incidents(items, categories=categories, hotfix_tags=c.config.hotfix_tags,
                             days=days, now=now), None


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/environments", response_class=HTMLResponse)
    async def environments_page(request: Request, project: str = "", days: int = 90):
        c: Container = request.app.state.container
        cfg = c.config
        days = days if days in WINDOWS else 90
        projects = list(cfg.effective_ado_projects or [])
        chosen = project if project in projects else (projects[0] if projects else "")
        connected = bool(cfg.ado_organization and cfg.ado_pat)
        now = datetime.now(UTC)
        specs = env_mod.parse_environments(cfg)
        envs = await gather_envs(c, specs, now) if connected and specs else []
        incidents, error = (await gather_incidents(c, chosen, days, now)
                            if connected and chosen else (None, None))
        return _TEMPLATES.TemplateResponse(
            request, "environments.html",
            _ctx(request, "environments", envs=envs, specs=specs, connected=connected,
                 incidents=incidents, error=error, projects=projects, project=chosen,
                 days=days, windows=WINDOWS, item_link=work_item_link_base(cfg),
                 hotfix_tags=cfg.hotfix_tags, configured_envs=bool(cfg.deploy_environments)),
        )

    return router
