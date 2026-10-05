"""Scheduled loops: list, edit, delete, run now."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import activity
from ai_autopilot import workspaces as workspaces_mod
from ai_autopilot.config import ScheduledLoop, config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import loop_presets, settings_form
from ai_autopilot.dashboard.common import (
    _BACKGROUND_RUNS,
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _int_or_zero,
    _log,
    _next_run,
    _reschedule,
    _take_flash,
    _workspace_agents,
    _workspace_repos,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.services import loop_scheduler as loop_scheduler_mod


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/loops", response_class=HTMLResponse)
    async def loops_page(request: Request):
        """Agents that run on a clock: what they do, how often, and with which sub-agents.

        These existed only in ``config.yaml``. Editing a schedule therefore meant a file
        on the server and a restart, which is why the four audits this page ships as
        presets had never been set up: the cost of trying one was a deploy.
        """
        c: Container = request.app.state.container
        cfg = c.config
        scheduler = getattr(request.app.state, "loop_scheduler", None)
        flash = _take_flash(request)
        rows = []
        for loop in cfg.scheduled_loops or []:
            rows.append({
                "name": loop.name, "prompt": loop.prompt, "cron": loop.cron,
                "interval_minutes": loop.interval_minutes, "mode": loop.mode,
                "agents": list(loop.agents or []), "project": loop.project,
                "repo_path": loop.repo_path, "base_branch": loop.base_branch,
                "draft_pr": loop.draft_pr, "enabled": loop.enabled,
                "report_html": loop.report_html,
                # A cadence that does not parse is the failure mode with no symptom:
                # the loop is "configured", listed, enabled — and never fires.
                "cadence_ok": loop_scheduler_mod._trigger(loop) is not None,
                # Why this one cannot run, from the SAME rule the scheduler applies.
                # The page used to state the rule in help text under the repo box and
                # leave the reader to apply it — so a loop that stops on its first line
                # every night looked identical to one that works.
                "blockers": loop_scheduler_mod.loop_blockers(loop, cfg),
                # Scoped to THIS loop's workspace, not the root one: a loop bound to
                # another project runs in that project's workspace, so offering the
                # default workspace's repo names would name repos it cannot reach.
                "repos": _workspace_repos(cfg.scoped_for_project(loop.project)),
                "next_run": _next_run(scheduler, loop.name),
                # Its own live feed — an audit is minutes of silence otherwise, and the
                # only other place its progress appears is a log file on the server.
                "feed": activity.loop_key(loop.name),
                "running": bool(scheduler and scheduler.is_running(loop.name)),
            })
        response = _TEMPLATES.TemplateResponse(
            request, "loops.html",
            _ctx(request, "loops", rows=rows, flash=flash,
                 known_agents=_workspace_agents(cfg),
                 # The blank "add a loop" row has no workspace of its own yet, so it
                 # shows the default one's repos.
                 root_repos=_workspace_repos(cfg),
                 presets=loop_presets.PRESETS,
                 projects=sorted({
                     p for w in workspaces_mod.resolve(cfg) for p in (w.projects or []) if p
                 }),
                 scheduler_live=scheduler is not None),
        )
        if flash:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/loops")
    async def loops_save(request: Request):
        """Save every row on the page, then reschedule the running service."""
        c: Container = request.app.state.container
        form = await request.form()

        loops: list[dict] = []
        for key in form:
            if not key.startswith("loop_") or not key.endswith("_name"):
                continue
            idx = key[len("loop_"):-len("_name")]
            name = str(form.get(f"loop_{idx}_name", "")).strip()
            if not name:
                continue        # a blank name is how a row is deleted from the form
            loops.append({
                "name": name,
                "prompt": str(form.get(f"loop_{idx}_prompt", "")).strip(),
                "cron": str(form.get(f"loop_{idx}_cron", "")).strip(),
                "interval_minutes": _int_or_zero(form.get(f"loop_{idx}_interval")),
                "mode": (str(form.get(f"loop_{idx}_mode") or "pr")
                         if form.get(f"loop_{idx}_mode") in ("report", "scan") else "pr"),
                "agents": [a for a in form.getlist(f"loop_{idx}_agents") if a],
                "scan_tools": [t.strip() for t in
                               str(form.get(f"loop_{idx}_scan_tools", "")).split(",")
                               if t.strip()],
                "scan_ai_mode": str(form.get(f"loop_{idx}_scan_ai", "")).strip(),
                "scan_scope": str(form.get(f"loop_{idx}_scan_scope", "") or "full").strip(),
                "project": str(form.get(f"loop_{idx}_project", "")).strip(),
                "repo_path": str(form.get(f"loop_{idx}_repo", "")).strip(),
                "base_branch": str(form.get(f"loop_{idx}_base", "")).strip(),
                "draft_pr": bool(form.get(f"loop_{idx}_draft")),
                "report_html": bool(form.get(f"loop_{idx}_html")),
                "enabled": bool(form.get(f"loop_{idx}_enabled")),
            })

        names = [row["name"] for row in loops]
        if len(names) != len(set(names)):
            # The name is the APScheduler job id, so a duplicate does not produce two
            # loops — the second silently REPLACES the first, and one of the two rows
            # on this page would simply never run.
            _log.error("loops rejected: duplicate name", names=sorted(names))
            return _flash("/dashboard/loops", "err_loop_name")

        models = [ScheduledLoop(**row) for row in loops]
        bad = [m.name for m in models if m.enabled and loop_scheduler_mod._trigger(m) is None]
        if bad:
            _log.error("loops rejected: no valid cadence", loops=bad)
            return _flash("/dashboard/loops", "err_loop_cadence")

        settings_form.save_to_yaml(config_file_path(), {"scheduled_loops": loops})
        # YAML takes plain dicts; the live config needs validated models, or the
        # scheduler reads a raw dict where it expects a ScheduledLoop.
        settings_form.apply_to_config(c.config, {"scheduled_loops": models})
        live = _reschedule(request)
        _log.info("loops updated via dashboard", count=len(models), scheduled=live)
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.loops_updated",
            target=", ".join(f"{m.name}({m.mode})" for m in models)[:300],
        )
        return _flash("/dashboard/loops", "loops_saved")

    @router.post("/loops/delete")
    async def loops_delete(request: Request, name: str = Form(...)):
        c: Container = request.app.state.container
        remaining = [le for le in (c.config.scheduled_loops or []) if le.name != name]
        if len(remaining) == len(c.config.scheduled_loops or []):
            return _flash("/dashboard/loops", "err_loop_missing")
        settings_form.save_to_yaml(
            config_file_path(),
            {"scheduled_loops": [le.model_dump() for le in remaining]},
        )
        settings_form.apply_to_config(c.config, {"scheduled_loops": remaining})
        _reschedule(request)
        _log.info("loop deleted via dashboard", name=name)
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.loop_deleted", target=name,
        )
        return _flash("/dashboard/loops", "loop_deleted")

    @router.post("/loops/run")
    async def loops_run(request: Request, name: str = Form(...)):
        """Run one loop now, off-schedule — the only way to try a weekly audit today."""
        c: Container = request.app.state.container
        scheduler = getattr(request.app.state, "loop_scheduler", None)
        if scheduler is None:
            return _flash("/dashboard/loops", "err_loop_missing")
        if scheduler.is_running(name):
            # Saying "started" here would be a lie the page then cannot walk back: the
            # run is skipped, no second report appears, and the operator is left waiting
            # for one. Checked FIRST: "it is running right now" is a fact about this
            # moment and outranks anything the current config would refuse.
            return _flash("/dashboard/loops", "loop_busy")
        loop = next((le for le in (c.config.scheduled_loops or []) if le.name == name), None)
        if loop is not None and loop_scheduler_mod.loop_blockers(loop, c.config):
            # Saying "started" and then stopping on the first line is the behaviour that
            # made this feature look broken rather than unconfigured.
            return _flash("/dashboard/loops", "err_loop_blocked")
        # Detached: an audit takes minutes, and the operator should get the page back
        # rather than hold a request open until the agent is done.
        task = asyncio.create_task(scheduler.run_now(name))
        _BACKGROUND_RUNS.add(task)
        task.add_done_callback(_BACKGROUND_RUNS.discard)
        _log.info("loop run requested from dashboard", name=name)
        return _flash("/dashboard/loops", "loop_started")

    return router
