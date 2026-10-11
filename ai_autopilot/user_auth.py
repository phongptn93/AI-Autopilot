"""Who is making this request — sessions, the current actor, and the access decision.

Only used when ``auth_mode`` is ``users`` or ``sso``; ``password`` mode keeps the shared
password exactly as before.

* **Sessions** are ``<payload>.<hmac>`` cookies carrying the user id and an expiry,
  signed with a key generated once and kept next to the config file (``.session.key``).
  The user row is re-read (cached a few seconds) on every request, so disabling a
  person or changing their roles takes effect immediately, not when a cookie expires.
* **The current actor** is a context variable the guard sets per request; the audit
  log reads it, so every recorded action names the person — not "dashboard".
* **Break-glass**: with ``auth_breakglass`` on, the old shared password still signs in,
  as an emergency admin, so a broken SSO setup can never lock everyone out. And while
  no admin account exists yet, a request from this machine (localhost) is let in as
  the bootstrap admin to create one.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from ai_autopilot import rbac

USER_COOKIE = "autopilot_user"
FLOW_COOKIE = "autopilot_sso_flow"
USER_TTL_HOURS = 12
BREAKGLASS = "breakglass"
current_actor: contextvars.ContextVar[str] = contextvars.ContextVar("current_actor", default="")

_key_cache: dict[str, bytes] = {}
_user_cache: dict[int, tuple[float, object]] = {}
_USER_CACHE_SECONDS = 5


@dataclass
class CurrentUser:
    id: int
    email: str
    name: str = ""
    roles: list[str] = field(default_factory=list)
    permissions: set[str] = field(default_factory=set)
    kind: str = "user"          # user | breakglass | bootstrap

    @property
    def label(self) -> str:
        return {"breakglass": "Admin khẩn cấp", "bootstrap": "Admin khởi tạo (localhost)"}.get(
            self.kind, self.name or self.email)

    def can(self, permission: str) -> bool:
        return rbac.allowed(self.permissions, permission)


def enabled(cfg) -> bool:
    return (getattr(cfg, "auth_mode", "password") or "password") in ("users", "sso")


def _key() -> bytes:
    """A per-install signing key, created once beside the config file."""
    from ai_autopilot.config import config_file_path

    path = Path(config_file_path()).with_name(".session.key")
    cached = _key_cache.get(str(path))
    if cached:
        return cached
    try:
        key = bytes.fromhex(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        key = secrets.token_bytes(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(key.hex(), encoding="utf-8")
            path.chmod(0o600)
        except OSError:
            pass            # an unwritable folder only means sessions end on restart
    _key_cache[str(path)] = key
    return key


def sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    mac = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def unsign(token: str | None, *, now: float | None = None) -> dict | None:
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    good = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, good):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    if float(payload.get("exp", 0)) < (now if now is not None else time.time()):
        return None
    return payload


def session_for(user_id: int, *, ttl_hours: int = USER_TTL_HOURS) -> str:
    return sign({"uid": int(user_id), "exp": int(time.time() + ttl_hours * 3600)})


def forget(user_id: int) -> None:
    """Drop a cached user row — call after any change to that user."""
    _user_cache.pop(int(user_id), None)


async def _load(repo, user_id: int):
    hit = _user_cache.get(user_id)
    if hit and time.time() - hit[0] < _USER_CACHE_SECONDS:
        return hit[1]
    row = await repo.get(user_id)
    _user_cache[user_id] = (time.time(), row)
    return row


def to_current(row, cfg) -> CurrentUser:
    roles = json.loads(row.roles_json or "[]")
    return CurrentUser(id=row.id, email=row.email, name=row.name, roles=roles,
                       permissions=rbac.permissions_of(roles, getattr(cfg, "user_roles", {})))


def _admin(kind: str) -> CurrentUser:
    return CurrentUser(id=0, email=kind, roles=[rbac.ADMIN], permissions={"*"}, kind=kind)


async def resolve(request, container) -> tuple[CurrentUser | None, str]:
    """``(user, why_not)`` for this request. ``why_not`` explains a None."""
    from ai_autopilot import security

    cfg = container.config
    payload = unsign(request.cookies.get(USER_COOKIE))
    if payload and payload.get("uid"):
        row = await _load(container.user_repo, int(payload["uid"]))
        if row is None:
            return None, "Tài khoản không còn tồn tại."
        if row.status == "pending":
            return None, "pending"
        if row.status != "active":
            return None, "Tài khoản đã bị khoá — liên hệ Admin."
        return to_current(row, cfg), ""
    if getattr(cfg, "auth_breakglass", True) and security.verify_session_token(
            request.cookies.get(security.SESSION_COOKIE), cfg):
        return _admin(BREAKGLASS), ""
    host = (request.client.host if request.client else "") or ""
    if host in ("127.0.0.1", "::1", "localhost") and not await container.user_repo.active_admins():
        return _admin("bootstrap"), ""
    return None, ""
