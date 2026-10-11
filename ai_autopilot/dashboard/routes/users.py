"""👥 Users & permissions — accounts, approvals, the role × permission matrix, SSO setup.

Every route here needs ``users.manage`` (see rbac._RULES). Two invariants are enforced
server-side, not just hidden in the page:

* the last active admin cannot be demoted, disabled or deleted;
* users/sso mode cannot be switched on while nobody could administer it afterwards
  (no active admin account and no break-glass password).
"""

from __future__ import annotations

import asyncio
import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ai_autopilot import oidc, rbac, security, user_auth
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.dashboard.common import _FLASH_COOKIE, _TEMPLATES, _flash, _take_flash
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.dashboard.routes.auth import redirect_uri
from ai_autopilot.logging_config import describe_exc

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _roles_of(row) -> list[str]:
    try:
        return list(json.loads(row.roles_json or "[]"))
    except ValueError:
        return []


async def _is_last_admin(c: Container, user_id: int) -> bool:
    row = await c.user_repo.get(user_id)
    return bool(row and row.status == "active" and rbac.ADMIN in _roles_of(row)
                and await c.user_repo.active_admins() <= 1)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        c: Container = request.app.state.container
        cfg = c.config
        flash = _take_flash(request)
        rows = await c.user_repo.all()
        order = {"pending": 0, "active": 1, "disabled": 2}
        users = sorted(rows, key=lambda r: (order.get(r.status, 3), r.email))
        table = rbac.roles(cfg.user_roles)
        response = _TEMPLATES.TemplateResponse(request, "users.html", _ctx(
            request, "users", users=users, roles_of=_roles_of, role_table=table,
            permissions=rbac.PERMISSIONS, flash=flash, cfg=cfg,
            redirect_uri=redirect_uri(request, cfg),
            pending=sum(1 for r in rows if r.status == "pending"),
            admins=sum(1 for r in rows if r.status == "active" and rbac.ADMIN in _roles_of(r)),
            breakglass_ready=bool(cfg.auth_breakglass and (
                cfg.dashboard_auth_password_hash or cfg.dashboard_auth_token)),
            sso_test=request.query_params.get("sso_test", ""),
        ))
        if flash:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/users/add")
    async def users_add(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        email = str(form.get("email") or "").strip().lower()
        if not _EMAIL.match(email):
            return _flash("/dashboard/users", "user_bad_email")
        if await c.user_repo.by_email(email):
            return _flash("/dashboard/users", "user_exists")
        known = rbac.roles(c.config.user_roles)
        roles = [r for r in form.getlist("roles") if r in known] or ["viewer"]
        password = str(form.get("password") or "")
        if password and len(password) < 10:
            return _flash("/dashboard/users", "user_weak_password")
        pw_hash = await asyncio.to_thread(security.hash_password, password) if password else None
        await c.user_repo.create(email=email, name=str(form.get("name") or "").strip(),
                                 source="local" if password else "sso", roles=roles,
                                 status="active", password_hash=pw_hash,
                                 approved_by=user_auth.current_actor.get())
        await c.audit_repo.record(actor="dashboard", source="dashboard", action="user.add",
                                  target=email, detail=",".join(roles))
        return _flash("/dashboard/users", "user_added")

    @router.post("/users/{user_id}/update")
    async def users_update(request: Request, user_id: int):
        c: Container = request.app.state.container
        row = await c.user_repo.get(user_id)
        if row is None:
            return RedirectResponse("/dashboard/users", status_code=303)
        form = await request.form()
        action = str(form.get("action") or "save")
        known = rbac.roles(c.config.user_roles)
        roles = [r for r in form.getlist("roles") if r in known]
        changes: dict = {}
        if action in ("save", "approve"):
            if rbac.ADMIN not in roles and await _is_last_admin(c, user_id):
                return _flash("/dashboard/users", "user_last_admin")
            changes["roles"] = roles or ["viewer"]
            if action == "approve":
                changes.update(status="active", approved_by=user_auth.current_actor.get())
        elif action == "disable":
            if await _is_last_admin(c, user_id):
                return _flash("/dashboard/users", "user_last_admin")
            changes["status"] = "disabled"
        elif action == "enable":
            changes["status"] = "active"
        elif action == "delete":
            if await _is_last_admin(c, user_id):
                return _flash("/dashboard/users", "user_last_admin")
            await c.user_repo.delete(user_id)
            user_auth.forget(user_id)
            await c.audit_repo.record(actor="dashboard", source="dashboard",
                                      action="user.delete", target=row.email)
            return _flash("/dashboard/users", "user_saved")
        elif action == "password":
            password = str(form.get("password") or "")
            if len(password) < 10:
                return _flash("/dashboard/users", "user_weak_password")
            changes["password_hash"] = await asyncio.to_thread(security.hash_password, password)
        await c.user_repo.update(user_id, **changes)
        user_auth.forget(user_id)
        await c.audit_repo.record(actor="dashboard", source="dashboard", action=f"user.{action}",
                                  target=row.email,
                                  detail=",".join(changes.get("roles", []))
                                  or changes.get("status", ""))
        return _flash("/dashboard/users", "user_saved")

    @router.post("/users/roles")
    async def users_roles(request: Request):
        """Save the role × permission matrix (and add / remove custom roles)."""
        c: Container = request.app.state.container
        form = await request.form()
        table = rbac.roles(c.config.user_roles)
        custom: dict = {}
        for key, role in table.items():
            if key == rbac.ADMIN or form.get(f"delete_{key}") and not role.builtin:
                continue
            perms = [p for p in rbac.PERMISSIONS if form.get(f"perm_{key}_{p}")]
            custom[key] = {"label": str(form.get(f"label_{key}") or role.label)[:60],
                           "permissions": perms}
        new_key = re.sub(r"[^a-z0-9_-]", "", str(form.get("new_key") or "").lower())[:40]
        if new_key and new_key not in table and new_key != rbac.ADMIN:
            custom[new_key] = {"label": str(form.get("new_label") or new_key)[:60],
                               "permissions": ["view"]}
        settings_form.save_to_yaml(config_file_path(), {"user_roles": custom})
        settings_form.apply_to_config(c.config, {"user_roles": custom})
        await c.audit_repo.record(actor="dashboard", source="dashboard", action="user.roles",
                                  target=", ".join(custom)[:300])
        return _flash("/dashboard/users#roles", "user_roles_saved")

    @router.post("/users/mode")
    async def users_mode(request: Request):
        """Switch password / users / sso — refusing a switch nobody could undo."""
        c: Container = request.app.state.container
        cfg = c.config
        form = await request.form()
        mode = str(form.get("mode") or "password")
        if mode not in ("password", "users", "sso"):
            return _flash("/dashboard/users", "user_bad_mode")
        if mode != "password":
            breakglass = cfg.auth_breakglass and (cfg.dashboard_auth_password_hash
                                                  or cfg.dashboard_auth_token)
            if not await c.user_repo.active_admins() and not breakglass:
                return _flash("/dashboard/users", "user_need_admin")
            if mode == "sso" and not (cfg.sso_client_id and cfg.sso_client_secret and (
                    cfg.sso_tenant_id or cfg.sso_provider != "entra")):
                return _flash("/dashboard/users", "user_sso_incomplete")
        settings_form.save_to_yaml(config_file_path(), {"auth_mode": mode})
        settings_form.apply_to_config(cfg, {"auth_mode": mode})
        await c.audit_repo.record(actor="dashboard", source="dashboard", action="auth.mode",
                                  target=mode)
        return _flash("/dashboard/users", f"auth_mode_{mode}")

    @router.post("/users/sso-test")
    async def users_sso_test(request: Request):
        c: Container = request.app.state.container
        try:
            meta = await oidc.discovery(c.http, c.config)
            msg = f"ok|Kết nối được {meta.get('issuer', '')} — sẵn sàng đăng nhập."
        except Exception as exc:  # noqa: BLE001
            msg = f"fail|{exc if isinstance(exc, oidc.OidcError) else describe_exc(exc)}"
        from urllib.parse import quote

        return RedirectResponse(f"/dashboard/users?sso_test={quote(msg[:300])}#sso",
                                status_code=303)

    return router
