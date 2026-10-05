"""Login and logout — the only dashboard pages reachable while it is locked."""

from __future__ import annotations

import asyncio
import contextlib
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ai_autopilot import security
from ai_autopilot.dashboard.common import _TEMPLATES, _log


def _safe_next(raw: str | None) -> str:
    """Only ever redirect back inside our own dashboard.

    `next` comes from the query string, so an attacker can put anything in it. A
    scheme-relative value like `//evil.example` is a valid *path* to a browser and
    would turn our login into an open redirect, which is why the check is a literal
    prefix test rather than "does it start with a slash".
    """
    target = raw or "/dashboard"
    if not target.startswith("/dashboard") or target.startswith("/dashboard//"):
        return "/dashboard"
    return target


def _login_locked(request: Request) -> bool:
    cfg = request.app.state.container.config
    return bool(cfg.dashboard_auth_password_hash or cfg.dashboard_auth_token)


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        nxt = _safe_next(request.query_params.get("next"))
        if not _login_locked(request):
            return RedirectResponse(nxt, status_code=303)
        cfg = request.app.state.container.config
        if security.verify_session_token(
            request.cookies.get(security.SESSION_COOKIE), cfg
        ):
            return RedirectResponse(nxt, status_code=303)   # already in
        return _TEMPLATES.TemplateResponse(
            request, "login.html",
            {"request": request, "version": request.app.version, "next": nxt, "error": ""},
        )

    @router.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        cfg = request.app.state.container.config
        form = await request.form()
        nxt = _safe_next(str(form.get("next") or ""))
        password = str(form.get("password") or "")
        ok = (
            # Off the loop: PBKDF2 at 480k iterations would stall every other request.
            await asyncio.to_thread(
                security.verify_password, password, cfg.dashboard_auth_password_hash
            )
            if cfg.dashboard_auth_password_hash
            else bool(cfg.dashboard_auth_token) and secrets.compare_digest(
                password, cfg.dashboard_auth_token
            )
        )
        if not ok:
            _log.warning("dashboard login failed", client=request.client.host
                         if request.client else "?")
            # 401, not 200: a failed login is not a successful page view, and the status
            # is what monitoring and fail2ban-style tooling actually key on.
            return _TEMPLATES.TemplateResponse(
                request, "login.html",
                {"request": request, "version": request.app.version, "next": nxt,
                 "error": "Mật khẩu không đúng."},
                status_code=401,
            )
        response = RedirectResponse(nxt, status_code=303)
        response.set_cookie(
            security.SESSION_COOKIE, security.make_session_token(cfg),
            max_age=security.SESSION_TTL_HOURS * 3600,
            httponly=True,          # not readable from JS
            samesite="lax",         # survives the redirect, blocks cross-site POSTs
            secure=request.url.scheme == "https",
            path="/dashboard",
        )
        with contextlib.suppress(Exception):
            await request.app.state.container.audit_repo.record(
                actor="dashboard", source="dashboard", action="dashboard.login",
                target=request.client.host if request.client else "?", detail="",
            )
        return response

    @router.post("/logout")
    async def logout(request: Request):
        response = RedirectResponse("/dashboard/login", status_code=303)
        response.delete_cookie(security.SESSION_COOKIE, path="/dashboard")
        return response

    return router
