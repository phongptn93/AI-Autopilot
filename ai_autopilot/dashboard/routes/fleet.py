"""Fleet view, sync, and the self-update actions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ai_autopilot import fleet as fleet_mod
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _json_list,
    _log,
    _take_flash,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.execution import sdlc_plan
from ai_autopilot.logging_config import describe_exc


def _worker_fleet_ctx(request: Request) -> dict:
    """What this worker can say about its own link to the centre.

    Everything is local: the agent's last round trip, and the fingerprint of the
    shareable half of this machine's config — the same one the central compares
    against, so "khớp" here means the same thing it means on the central's page.
    """
    c: Container = request.app.state.container
    cfg = c.config
    agent = getattr(request.app.state, "fleet_agent", None)
    _, local_hash = fleet_mod.config_document(cfg)
    beat_at = getattr(agent, "last_beat_at", None)
    return {
        "central_url": (cfg.fleet_central_url or "").strip(),
        "token_set": bool((cfg.fleet_token or "").strip()),
        "worker_name": getattr(agent, "worker_name", cfg.fleet_worker_name or ""),
        "interval": cfg.fleet_sync_interval_minutes,
        "local_hash": local_hash,
        "local_keys": [str(k) for k in (cfg.fleet_local_keys or []) if str(k).strip()],
        "shared_count": len(settings_form.fleet_settings(cfg)),
        # None until the first beat of this process — which is not the same as "it
        # failed", and the page says so rather than painting a red cross at boot.
        "last_ok": getattr(agent, "last_ok", None),
        "last_detail": getattr(agent, "last_detail", ""),
        "last_applied": list(getattr(agent, "last_applied", []) or []),
        "beat_ago": (
            int((datetime.now(UTC) - beat_at).total_seconds()) if beat_at else None
        ),
        # No agent at all means the service never started: fleet_role says worker but
        # this process is not running one, which no amount of button-pressing fixes.
        "agent_live": agent is not None,
        # "The centre moved on without you." A worker on older code can silently
        # DROP settings it has no field for, so this is a correctness warning, not
        # a cosmetic one — and it belongs on the machine that has to act on it.
        "my_version": request.app.version,
        "central_version": getattr(agent, "central_version", ""),
        "behind": bool(getattr(agent, "behind", False)),
        "upgrade_cmd": (
            "pip install --upgrade https://github.com/phongptn93/AI-Autopilot/"
            "releases/latest/download/ai_autopilot-"
            f"{getattr(agent, 'central_version', '') or request.app.version}"
            "-py3-none-any.whl"
        ),
    }


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/fleet", response_class=HTMLResponse)
    async def fleet_page(request: Request):
        """The worker machines: alive, on which version, running what, config in step.

        Only reachable on a central. Everywhere else the table is empty by definition,
        and an empty page with no explanation reads as a broken feature rather than as
        a mode that is switched off.
        """
        c: Container = request.app.state.container
        cfg = c.config
        role = cfg.fleet_role or ""
        if role == fleet_mod.ROLE_WORKER:
            # A worker has no table of machines — it IS one. Same page, its own half:
            # who it calls, whether that call worked, and the button to make it now.
            flash = _take_flash(request)
            response = _TEMPLATES.TemplateResponse(
                request, "fleet_worker.html",
                _ctx(request, "fleet", flash=flash, **_worker_fleet_ctx(request)),
            )
            if flash is not None:
                response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
            return response
        if role != fleet_mod.ROLE_CENTRAL:
            raise HTTPException(status_code=404, detail="fleet mode is not enabled here")
        _, central_hash = fleet_mod.config_document(cfg)
        now = datetime.now(UTC)
        offline_after = max(1, int(cfg.fleet_offline_after_minutes)) * 60
        workers = []
        for row in await c.fleet_repo.list_all():
            seen = row.last_seen
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=UTC)
            quiet = (now - seen).total_seconds()
            synced = row.config_synced_at
            if synced is not None and synced.tzinfo is None:
                synced = synced.replace(tzinfo=UTC)
            workers.append({
                "name": row.name,
                "hostname": row.hostname,
                "version": row.version,
                # A worker on an older build may not even understand the settings it is
                # being sent, so the drift is worth showing next to the sync state.
                #
                # Two facts, not one. "Different" flagged a machine running AHEAD of the
                # central — normal for the hour of a rollout — with the same red chip as
                # one running code that predates the settings it is being handed, and it
                # never said which side had to move.
                "version_drift": bool(row.version and row.version != request.app.version),
                "version_behind": fleet_mod.is_behind(row.version, request.app.version),
                "profile": row.profile,
                "stages": [s.name for s in sdlc_plan.profile_stages(row.profile, cfg)]
                          if row.profile else [],
                # Minus the shared run-now tag. Workers stopped sending it, but one
                # already in the field goes on doing so until it is upgraded, and the
                # column claims to show what each machine chose for ITSELF.
                "tags": [
                    t for t in _json_list(row.tags)
                    if str(t).strip().lower() != (cfg.stage_entry_tag or "").strip().lower()
                ],
                "in_sync": bool(row.config_hash) and row.config_hash == central_hash,
                "synced_ago": int((now - synced).total_seconds()) if synced else None,
                "online": quiet <= offline_after,
                "quiet": int(quiet),
                "running": _json_list(row.running),
                "done_today": row.done_today,
                "failed_today": row.failed_today,
                # These two are whatever the machine last REPORTED, and the central keeps
                # showing them after it goes quiet. Under a heading reading "Hôm nay" that
                # is simply false: a machine last heard from three days ago was presenting
                # Wednesday's "10 lỗi" as today's, which is the kind of number somebody
                # acts on. Say whether the figures are actually from today; the template
                # re-labels them when they are not.
                "stats_today": seen.astimezone().date() == now.astimezone().date(),
            })
        return _TEMPLATES.TemplateResponse(
            request, "fleet.html",
            _ctx(request, "fleet", workers=workers, central_hash=central_hash,
                 offline_after_minutes=cfg.fleet_offline_after_minutes),
        )

    @router.post("/update")
    async def apply_update(request: Request):
        """Take the newer release. One button, never automatic.

        Returns immediately and does the work in a background task: draining can take
        the better part of an hour, and a request that hangs that long is a request the
        browser gives up on — after which nobody can tell whether it is still going.
        """
        c: Container = request.app.state.container
        updater = getattr(request.app.state, "updater", None)
        if updater is None or not updater.available:
            return _flash("/dashboard/settings", "update_none")
        if updater.job.running:
            return _flash("/dashboard/settings", "update_busy")
        block = updater.blocked()
        if block:
            return _flash("/dashboard/settings", f"update_blocked_{block}")
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="update.requested",
            target=updater.latest.version if updater.latest else "",
            detail=f"from {request.app.version}",
        )
        # Detached on purpose — see the docstring. Held on app.state so it is not
        # garbage-collected mid-flight.
        request.app.state.update_task = asyncio.create_task(
            updater.apply(getattr(request.app.state, "poller", None)),
            name="update-apply",
        )
        return _flash("/dashboard/settings", "update_started")

    @router.post("/update/check")
    async def check_update(request: Request):
        """Ask GitHub now, instead of waiting out the interval (or when the periodic check
        is switched off). Only a check — installing is still the separate, confirmed
        "Cập nhật ngay" press."""
        updater = getattr(request.app.state, "updater", None)
        back = "/dashboard/settings#update-panel"
        if updater is None:
            return _flash(back, "update_unavailable")
        try:
            release = await asyncio.wait_for(updater.check(), timeout=25)
        except Exception as exc:  # noqa: BLE001 — network trouble is a message, not a 500
            _log.info("manual update check failed", error=describe_exc(exc))
            return _flash(back, "update_check_failed")
        if release is None:
            return _flash(back, "update_check_failed")
        return _flash(back, "update_found" if updater.available else "update_uptodate")

    @router.post("/fleet/sync")
    async def fleet_sync(request: Request):
        """Beat now, instead of waiting out the interval.

        The interval is a floor of one minute and usually ten, so every "did the centre
        get my change" question cost either a restart or a wait. ``beat()`` has always
        returned whether the round trip worked — this is the caller it was written for.
        """
        c: Container = request.app.state.container
        if (c.config.fleet_role or "") != fleet_mod.ROLE_WORKER:
            raise HTTPException(status_code=404, detail="only a worker syncs")
        agent = getattr(request.app.state, "fleet_agent", None)
        if agent is None:
            return _flash("/dashboard/fleet", "fleet_no_agent")
        ok = await agent.beat()
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="fleet.sync_requested",
            target=(c.config.fleet_central_url or "")[:300],
            detail=agent.last_detail,
        )
        if not ok:
            return _flash("/dashboard/fleet", "fleet_sync_failed")
        return _flash(
            "/dashboard/fleet",
            "fleet_synced_changed" if agent.last_applied else "fleet_synced_same",
        )

    @router.post("/fleet/forget")
    async def fleet_forget(request: Request):
        """Drop a machine that is gone for good. Never automatic: a machine that stops
        reporting is the most important thing this page can tell you."""
        c: Container = request.app.state.container
        if (c.config.fleet_role or "") != "central":
            raise HTTPException(status_code=404, detail="fleet mode is not enabled here")
        form = await request.form()
        name = str(form.get("name", "")).strip()
        if name:
            await c.fleet_repo.forget(name)
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="fleet.forgotten", target=name,
            )
        return RedirectResponse("/dashboard/fleet", status_code=303)

    return router
