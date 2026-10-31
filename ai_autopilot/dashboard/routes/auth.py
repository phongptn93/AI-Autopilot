"""Login, logout and company SSO — the only dashboard pages reachable while locked.

Three modes (``auth_mode``):

* ``password`` — one shared password, unchanged.
* ``users``    — personal accounts (email + password) with roles.
* ``sso``      — company sign-in over OpenID Connect (Microsoft Entra ID preset), with
  roles; first sign-in waits for an admin (``sso_new_users``).

In users/sso mode the shared password keeps working as an emergency admin login when
``auth_breakglass`` is on, so a broken SSO setup cannot lock everyone out.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ai_autopilot import oidc, rbac, security, user_auth
from ai_autopilot.dashboard.common import _TEMPLATES, _log

_FLOW_TTL = 600
_FAIL_WINDOW = 600
_FAIL_PER_PEER = 20
_fail_seen: dict[str, list[float]] = {}


async def _audit_failure(c, peer: str, *, actor: str, detail: str) -> None:
    """Record a failed sign-in — at most ``_FAIL_PER_PEER`` rows per address per window.

    These pages answer before anyone has signed in, so one row per attempt would let
    anybody on the network grow the database without bound. Past the cap the attempt
    is still in the application log; the trail keeps the first ones, which are the
    ones that say a guessing run started.
    """
    now = time.monotonic()
    if len(_fail_seen) > 5000:
        _fail_seen.clear()
    hits = [t for t in _fail_seen.get(peer, []) if now - t < _FAIL_WINDOW]
    _fail_seen[peer] = hits
    if len(hits) >= _FAIL_PER_PEER:
        return
    hits.append(now)
    with contextlib.suppress(Exception):
        await c.audit_repo.record(actor=actor, source="dashboard",
                                  action="dashboard.login_failed", target=peer, detail=detail)


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
    return user_auth.enabled(cfg) or bool(cfg.dashboard_auth_password_hash
                                          or cfg.dashboard_auth_token)


def _cookie(response, name: str, value: str, request: Request, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/dashboard")


async def _shared_password_ok(cfg, password: str) -> bool:
    if cfg.dashboard_auth_password_hash:
        # Off the loop: PBKDF2 at 480k iterations would stall every other request.
        return await asyncio.to_thread(
            security.verify_password, password, cfg.dashboard_auth_password_hash)
    return bool(cfg.dashboard_auth_token) and secrets.compare_digest(
        password, cfg.dashboard_auth_token)


def redirect_uri(request: Request, cfg) -> str:
    base = ((cfg.sso_redirect_base or "").strip() or (cfg.dashboard_public_url or "").strip()
            or str(request.base_url)).rstrip("/")
    return f"{base}/dashboard/auth/sso/callback"


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    def page(request: Request, *, nxt: str, error: str = "", info: str = "",
             status: int = 200, bg_open: bool = False):
        cfg = request.app.state.container.config
        mode = cfg.auth_mode if user_auth.enabled(cfg) else "password"
        return _TEMPLATES.TemplateResponse(
            request, "login.html",
            {"request": request, "version": request.app.version, "next": nxt,
             "error": error, "info": info, "mode": mode, "sso_provider": cfg.sso_provider,
             "breakglass": cfg.auth_breakglass and bool(
                 cfg.dashboard_auth_password_hash or cfg.dashboard_auth_token),
             "bg_open": bg_open},
            status_code=status)

    @router.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        nxt = _safe_next(request.query_params.get("next"))
        if not _login_locked(request):
            return RedirectResponse(nxt, status_code=303)
        cfg = request.app.state.container.config
        if user_auth.enabled(cfg):
            if user_auth.unsign(request.cookies.get(user_auth.USER_COOKIE)) and \
                    request.query_params.get("pending") != "1":
                return RedirectResponse(nxt, status_code=303)
            info = ("Tài khoản của bạn đã được tạo và đang chờ Admin duyệt. Bạn sẽ vào được "
                    "ngay khi được duyệt." if request.query_params.get("pending") == "1" else "")
            return page(request, nxt=nxt, info=info,
                        error=request.query_params.get("error", "")[:300])
        if security.verify_session_token(request.cookies.get(security.SESSION_COOKIE), cfg):
            return RedirectResponse(nxt, status_code=303)   # already in
        return page(request, nxt=nxt)

    @router.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        c = request.app.state.container
        cfg = c.config
        form = await request.form()
        nxt = _safe_next(str(form.get("next") or ""))
        password = str(form.get("password") or "")
        client = request.client.host if request.client else "?"
        response = RedirectResponse(nxt, status_code=303)

        # Personal account (users mode).
        if user_auth.enabled(cfg) and form.get("email") and cfg.auth_mode == "users":
            row = await c.user_repo.by_email(str(form.get("email")))
            ok = bool(row and row.password_hash) and await asyncio.to_thread(
                security.verify_password, password, row.password_hash)
            if not ok or row.status != "active":
                _log.warning("dashboard login failed", client=client, email=str(form.get("email")))
                await _audit_failure(c, client, actor=str(form.get("email"))[:200],
                                     detail="local: " + (f"account {row.status}" if ok
                                                         else "wrong email or password"))
                msg = ("Tài khoản đang chờ duyệt hoặc đã bị khoá." if ok
                       else "Email hoặc mật khẩu không đúng.")
                return page(request, nxt=nxt, error=msg, status=401)
            await c.user_repo.update(row.id, last_login_at=datetime.now(UTC))
            _cookie(response, user_auth.USER_COOKIE, user_auth.session_for(row.id), request,
                    user_auth.USER_TTL_HOURS * 3600)
            with contextlib.suppress(Exception):
                await c.audit_repo.record(actor=row.email, source="dashboard",
                                          action="dashboard.login", target=client, detail="local")
            return response

        # Shared password: the only login in password mode, the emergency one otherwise.
        if user_auth.enabled(cfg) and not cfg.auth_breakglass:
            return page(request, nxt=nxt, error="Đăng nhập bằng mật khẩu chung đã bị tắt.",
                        status=401)
        if not await _shared_password_ok(cfg, password):
            _log.warning("dashboard login failed", client=client)
            await _audit_failure(
                c, client, actor=user_auth.BREAKGLASS if user_auth.enabled(cfg) else "dashboard",
                detail="shared password")
            # 401, not 200: a failed login is not a successful page view, and the status
            # is what monitoring and fail2ban-style tooling actually key on.
            return page(request, nxt=nxt, error="Mật khẩu không đúng.", status=401,
                        bg_open=user_auth.enabled(cfg))
        _cookie(response, security.SESSION_COOKIE, security.make_session_token(cfg), request,
                security.SESSION_TTL_HOURS * 3600)
        with contextlib.suppress(Exception):
            await c.audit_repo.record(
                actor=user_auth.BREAKGLASS if user_auth.enabled(cfg) else "dashboard",
                source="dashboard", action="dashboard.login", target=client,
                detail="breakglass" if user_auth.enabled(cfg) else "")
        return response

    @router.post("/logout")
    async def logout(request: Request):
        c = request.app.state.container
        # /logout is public, so the guard set no actor — read it from the session.
        session = user_auth.unsign(request.cookies.get(user_auth.USER_COOKIE)) or {}
        row = await c.user_repo.get(int(session["uid"])) if session.get("uid") else None
        if row is not None or security.verify_session_token(
                request.cookies.get(security.SESSION_COOKIE), c.config):
            with contextlib.suppress(Exception):
                await c.audit_repo.record(
                    actor=row.email if row is not None else (
                        user_auth.BREAKGLASS if user_auth.enabled(c.config) else "dashboard"),
                    source="dashboard", action="dashboard.logout",
                    target=request.client.host if request.client else "")
        response = RedirectResponse("/dashboard/login", status_code=303)
        response.delete_cookie(security.SESSION_COOKIE, path="/dashboard")
        response.delete_cookie(user_auth.USER_COOKIE, path="/dashboard")
        return response

    # ── SSO ─────────────────────────────────────────────────────────────────
    @router.get("/auth/sso/start")
    async def sso_start(request: Request):
        c = request.app.state.container
        cfg = c.config
        nxt = _safe_next(request.query_params.get("next"))
        try:
            meta = await oidc.discovery(c.http, cfg)
        except Exception as exc:  # noqa: BLE001 — shown on the login page
            return RedirectResponse(f"/dashboard/login?error={quote(_why(exc))}", status_code=303)
        flow = oidc.new_flow()
        flow["next"] = nxt
        flow["exp"] = flow["at"] + _FLOW_TTL
        response = RedirectResponse(oidc.authorize_url(meta, cfg, flow, redirect_uri(request, cfg)),
                                    status_code=303)
        _cookie(response, user_auth.FLOW_COOKIE, user_auth.sign(flow), request, _FLOW_TTL)
        return response

    @router.get("/auth/sso/callback")
    async def sso_callback(request: Request):
        c = request.app.state.container
        cfg = c.config
        flow = user_auth.unsign(request.cookies.get(user_auth.FLOW_COOKIE))
        q = request.query_params
        peer = request.client.host if request.client else "?"
        if q.get("error"):
            why = q.get("error_description") or q.get("error")
            await _audit_failure(c, peer, actor="sso", detail=f"sso: idp error {why}"[:300])
            return _back(f"Nhà cung cấp SSO báo lỗi: {why}")
        if not flow or not q.get("state") or not secrets.compare_digest(
                str(q.get("state")), str(flow.get("state"))):
            await _audit_failure(c, peer, actor="sso", detail="sso: missing or forged state")
            return _back("Phiên đăng nhập hết hạn hoặc không hợp lệ — thử lại.")
        try:
            meta = await oidc.discovery(c.http, cfg)
            ident = await oidc.exchange(c.http, meta, cfg, str(q.get("code") or ""), flow,
                                        redirect_uri(request, cfg))
        except Exception as exc:  # noqa: BLE001
            _log.warning("sso sign-in failed", error=_why(exc))
            await _audit_failure(c, peer, actor="sso", detail=f"sso: {_why(exc)}"[:300])
            return _back(_why(exc))

        row = await c.user_repo.by_email(ident.email)
        mapped = oidc.mapped_roles(ident, cfg.sso_role_map)
        known = rbac.roles(cfg.user_roles)
        mapped = [r for r in mapped if r in known]
        if row is None:
            pending = (cfg.sso_new_users or "pending") != "viewer"
            uid = await c.user_repo.create(
                email=ident.email, name=ident.name, source="sso", subject=ident.subject,
                roles=mapped or (["viewer"] if not pending else []),
                status="pending" if pending else "active")
            row = await c.user_repo.get(uid)
            with contextlib.suppress(Exception):
                await c.audit_repo.record(actor=ident.email, source="dashboard",
                                          action="user.signup", target=ident.email,
                                          detail="pending" if pending else "viewer")
        else:
            changes = {"last_login_at": datetime.now(UTC), "name": ident.name or row.name,
                       "subject": ident.subject or row.subject}
            # Mapped roles follow the IdP: a person moved between groups gets the new role
            # on their next sign-in, without an admin touching the dashboard.
            if mapped:
                changes["roles"] = mapped
            await c.user_repo.update(row.id, **changes)
            user_auth.forget(row.id)
            row = await c.user_repo.get(row.id)

        target = ("/dashboard/login?pending=1" if row.status == "pending"
                  else flow.get("next") or "/dashboard")
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(user_auth.FLOW_COOKIE, path="/dashboard")
        if row.status == "disabled":
            await _audit_failure(c, peer, actor=row.email, detail="sso: account disabled")
            return _back("Tài khoản đã bị khoá — liên hệ Admin.")
        _cookie(response, user_auth.USER_COOKIE, user_auth.session_for(row.id), request,
                user_auth.USER_TTL_HOURS * 3600)
        if row.status == "active":
            with contextlib.suppress(Exception):
                await c.audit_repo.record(actor=row.email, source="dashboard",
                                          action="dashboard.login", target=row.email, detail="sso")
        return response

    return router


def _why(exc: Exception) -> str:
    return str(exc) if isinstance(exc, oidc.OidcError) else f"Không kết nối được SSO: {exc}"[:300]


def _back(message: str) -> RedirectResponse:
    resp = RedirectResponse(f"/dashboard/login?error={quote(message[:300])}", status_code=303)
    resp.delete_cookie(user_auth.FLOW_COOKIE, path="/dashboard")
    return resp
