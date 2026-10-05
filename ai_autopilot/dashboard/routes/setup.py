"""First-run setup wizard."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ai_autopilot import fleet as fleet_mod
from ai_autopilot import security
from ai_autopilot import workspaces as workspaces_mod
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _log,
    _take_flash,
    forget_scans,
)
from ai_autopilot.dashboard.routes._shared import _ctx, _setup_flow, _setup_role, _setup_source
from ai_autopilot.logging_config import describe_exc

# Which step offers a "test it now" button, and what that button proves. A wizard
# that only collects text and lets the failure surface hours later during a real run
# is a form with extra clicks.
_SETUP_CHECKS = {"ado": "ado", "connect": "fleet"}


def _setup_findings(cfg) -> list[dict]:
    """What the real doctor says about this machine, worst first."""
    from ai_autopilot import doctor as doctor_mod

    try:
        found = doctor_mod.diagnose(cfg)
    except Exception as exc:  # noqa: BLE001 — the last page must still render
        _log.warning("setup: audit failed", error=describe_exc(exc))
        return []
    rank = {"ERROR": 0, "WARN": 1, "OK": 2, "INFO": 3}
    rows = [
        {"level": str(getattr(f, "level", "")), "title": str(getattr(f, "title", "")),
         "detail": str(getattr(f, "detail", "")), "fix": str(getattr(f, "fix", ""))}
        for f in found
    ]
    return sorted(rows, key=lambda r: rank.get(r["level"].upper(), 9))


def _setup_jira_view(cfg) -> dict:
    """This machine's Jira workspace as the wizard shows it (blank when there is none)."""
    for view in workspaces_mod.resolve(cfg):
        if (view.provider or "").strip().lower() == "jira":
            return {
                "name": view.name, "url": view.jira_url, "email": view.jira_email,
                "project": view.jira_project, "token_set": view.jira_token_set,
                "index": max(0, len(cfg.workspaces or []) - 1),
            }
    return {"name": "", "url": "", "email": "", "project": "", "token_set": False,
            "index": len(cfg.workspaces or [])}


async def _setup_save_jira(request: Request, form) -> None:
    """Write the Jira workspace the wizard just collected.

    Goes through the Workspaces page's own resolve → to_settings_updates →
    carry_secrets path rather than assembling a dict here, so the wizard cannot
    drift from the editor that owns this shape (and so a stored API token is not
    wiped by a page that never renders it).

    Sets the workspace's project list to the Jira key. Routing matches an item's own
    project against that list, and a Jira item carries its JIRA key — leave them to
    be typed separately and the day they disagree the lookup silently falls back to
    Azure DevOps. The wizard is the one place that can make them agree by
    construction instead of by a doctor warning afterwards.
    """
    c: Container = request.app.state.container
    cfg = c.config
    url = str(form.get("jira_url", "") or "").strip()
    email = str(form.get("jira_email", "") or "").strip()
    project = str(form.get("jira_project", "") or "").strip()
    name = str(form.get("jira_name", "") or "").strip() or (project or "Jira")
    if not project:
        return                      # nothing to route on — leave the config alone

    views = workspaces_mod.resolve(cfg)
    target = next(
        (v for v in views if (v.provider or "").lower() == "jira" and not v.is_default),
        None,
    )
    if target is None:
        target = workspaces_mod.WorkspaceView(
            id=workspaces_mod.slugify(name, {v.id for v in views}), name=name,
        )
        views.append(target)
    target.name = name
    target.provider = "jira"
    target.jira_url, target.jira_email, target.jira_project = url, email, project
    target.projects = [project]
    target.enabled = True
    if not target.directory:
        target.directory = cfg.workspace_directory or ""

    updates = workspaces_mod.carry_secrets(
        workspaces_mod.to_settings_updates(views), cfg
    )
    settings_form.save_to_yaml(config_file_path(), updates)
    settings_form.apply_to_config(cfg, updates)
    with contextlib.suppress(Exception):
        c.build_providers()     # the tracker for this project just changed
    await c.audit_repo.record(
        actor="dashboard", source="dashboard", action="setup.jira_configured",
        target=project, detail=url[:200],
    )


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        """The short path to a machine that works, in the order the answers depend."""
        c: Container = request.app.state.container
        cfg = c.config
        by_key = {f.key: f for f in settings_form.FIELDS}
        role = _setup_role(cfg)
        source = _setup_source(request, cfg)
        steps = _setup_flow(role, source)
        want = (request.query_params.get("step") or "").strip()
        # "role" is step zero and always reachable: changing your mind about what this
        # machine is must not mean editing config.yaml by hand.
        ids = ["role", *[s[0] for s in steps], "done"]
        current = want if want in ids else "role"
        step = next((s for s in steps if s[0] == current), None)
        index = ids.index(current)
        # Only for a step that actually shows a state picker. Reading the states costs
        # 1 + N requests to ADO (cached, but still), and every other step in the wizard
        # would have been paying for a list it does not render.
        step_keys = step[2] if step else ()
        ado_states: list[str] = []
        if any(by_key[k].kind in ("stateset", "stateone")
               for k in step_keys if k in by_key):
            with contextlib.suppress(Exception):   # offline → type them by hand
                ado_states = await c.ado.get_states()
        secrets_set = {
            key: bool(getattr(cfg, key, "")) for key in settings_form.SECRET_KEYS
        }
        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request, "setup.html",
            _ctx(
                request, "setup",
                role=role, source=source, steps=steps, step_id=current, flash=flash,
                heading=step[1] if step else "",
                step_fields=[by_key[k] for k in step_keys if k in by_key],
                ado_states=ado_states,
                check=_SETUP_CHECKS.get(current, ""),
                index=index, total=len(ids) - 1,
                next_id=ids[index + 1] if index + 1 < len(ids) else "done",
                prev_id=ids[index - 1] if index > 0 else "",
                current={f.key: getattr(cfg, f.key, "") for f in settings_form.FIELDS
                         if f.key not in settings_form.SECRET_KEYS},
                secrets_set=secrets_set,
                # The finish line reports with the SAME audit the CLI runs — a wizard
                # that grades itself against its own shorter checklist is how "setup
                # complete" and "actually working" drift apart.
                findings=_setup_findings(cfg) if current == "done" else [],
                central_url=(cfg.fleet_central_url or "").strip(),
                fleet_token_set=bool((cfg.fleet_token or "").strip()),
                jira=_setup_jira_view(cfg),
            ),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/setup")
    async def setup_save(request: Request):
        """Save one step and move on. Same write path as the Settings page."""
        c: Container = request.app.state.container
        cfg = c.config
        form = await request.form()
        step_id = str(form.get("step", "")).strip()
        source = str(form.get("src", "")).strip().lower()
        if source not in ("ado", "jira"):
            source = _setup_source(request, cfg)
        updates: dict = {}

        if step_id == "role":
            chosen = str(form.get("fleet_role", "")).strip()
            if chosen not in ("", "central", "worker"):
                raise HTTPException(status_code=422, detail="unknown role")
            updates["fleet_role"] = chosen
        elif step_id == "source":
            # Nothing is written here. The choice only decides which connection the
            # next step asks for, and it travels in the URL — creating a half-filled
            # Jira workspace off a radio button would leave a broken route behind for
            # anyone who changed their mind.
            chosen = str(form.get("work_item_source", "")).strip().lower()
            source = chosen if chosen in ("ado", "jira") else "ado"
        elif step_id == "jira":
            await _setup_save_jira(request, form)
        else:
            keys = next(
                (s[2] for s in _setup_flow(_setup_role(cfg), source) if s[0] == step_id),
                None,
            )
            if keys is None:
                raise HTTPException(status_code=404, detail="unknown step")
            by_key = {f.key: f for f in settings_form.FIELDS}
            # Reuse the page's own parser so coercion and the "blank password keeps the
            # stored one" rule cannot drift between the two ways into the same settings.
            parsed = settings_form.parse_form(form)
            for key in keys:
                if key not in by_key or key not in parsed:
                    continue
                if by_key[key].kind == "password" and not str(form.get(key, "")).strip():
                    continue            # blank = keep what is stored
                if settings_form.writable_here(key, cfg):
                    updates[key] = parsed[key]

        # The same refusal the Settings page makes, for the same reason — and it has to
        # be HERE too, not only there, now that the wizard can set this field. The
        # run-now sweep removes the tag it matched on; set it to the trigger tag and the
        # first sweep strips ownership off every item the autopilot has, silently and
        # unrecoverably. A wizard is exactly where somebody types the same tag twice.
        if "stage_entry_tag" in updates:
            why = settings_form.run_now_tag_conflict(updates["stage_entry_tag"], cfg)
            if why:
                _log.error("setup rejected: run-now tag collides", reason=why,
                           tag=updates["stage_entry_tag"], step=step_id)
                return _flash(f"/dashboard/setup?step={step_id}", "err_run_tag_clash")

        if updates:
            raw = updates.pop("dashboard_auth_password", None)
            if raw:
                updates["dashboard_auth_password_hash"] = security.hash_password(raw)
            settings_form.save_to_yaml(config_file_path(), updates)
            settings_form.apply_to_config(cfg, updates)
            with contextlib.suppress(Exception):
                c.ado.refresh()
                forget_scans()   # cached scans describe the OLD config
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="setup.step_saved",
                target=step_id, detail=", ".join(sorted(updates))[:300],
            )
        # Recomputed AFTER the save: choosing a role on step zero decides which steps
        # exist, so the "next" of that step only becomes knowable once it is applied.
        ids = ["role", *[s[0] for s in _setup_flow(_setup_role(cfg), source)], "done"]
        here = ids.index(step_id) if step_id in ids else 0
        nxt = ids[here + 1] if here + 1 < len(ids) else "done"
        return RedirectResponse(
            f"/dashboard/setup?step={nxt}&src={source}", status_code=303
        )

    @router.post("/setup/check/{what}")
    async def setup_check(request: Request, what: str):
        """Prove a step's answers before moving on: ADO credentials, or the central."""
        c: Container = request.app.state.container
        cfg = c.config
        if what == "ado":
            try:
                states = await c.ado.get_states()
            except Exception as exc:  # noqa: BLE001 — the answer IS the error
                return JSONResponse({"ok": False, "detail": describe_exc(exc)})
            return JSONResponse({
                "ok": True,
                "detail": f"Kết nối được — đọc thấy {len(states)} trạng thái của dự án.",
            })
        if what == "fleet":
            base = (cfg.fleet_central_url or "").strip().rstrip("/")
            token = (cfg.fleet_token or "").strip()
            if not base or not token:
                return JSONResponse({"ok": False, "detail": "Chưa có URL trung tâm hoặc token."})
            # A real heartbeat, not a ping: reachability proves nothing about whether
            # the token matches or whether that host is a central at all.
            # `probe` keeps the central from enrolling the machine that is merely
            # testing its answers — see WorkerReport.probe.
            report = fleet_mod.WorkerReport(
                name=(cfg.fleet_worker_name or "").strip() or "setup-check",
                probe=True,
            )
            try:
                resp = await c.http.post(
                    f"{base}/api/fleet/heartbeat", json=report.model_dump(mode="json"),
                    headers={fleet_mod.TOKEN_HEADER: token},
                )
            except Exception as exc:  # noqa: BLE001
                return JSONResponse({"ok": False, "detail": f"Không gọi được: {describe_exc(exc)}"})
            if resp.status_code == 401:
                return JSONResponse({
                    "ok": False,
                    "detail": "Trung tâm từ chối token (401) — token 2 phía chưa khớp.",
                })
            if resp.status_code == 404:
                return JSONResponse({
                    "ok": False,
                    "detail": "Địa chỉ này không bật API fleet — có chắc nó là máy trung tâm?",
                })
            if resp.status_code >= 400:
                return JSONResponse({
                    "ok": False, "detail": f"Trung tâm trả HTTP {resp.status_code}.",
                })
            body = resp.json() or {}
            return JSONResponse({
                "ok": True,
                "detail": f"Nối được trung tâm (v{body.get('central_version') or '?'}), "
                          f"token khớp. Bản cấu hình đang phát: {body.get('config_hash') or '?'}.",
            })
        raise HTTPException(status_code=404, detail="unknown check")

    return router
