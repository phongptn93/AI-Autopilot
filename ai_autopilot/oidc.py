"""OpenID Connect sign-in — Microsoft Entra ID first, any OIDC provider by issuer URL.

Authorization Code flow with PKCE, ``state`` and ``nonce``. The ID token's RS256
signature is verified against the provider's JWKS here, with ``cryptography`` (already
a dependency) — no extra package, and no trust placed in an unverified token: issuer,
audience, expiry, nonce and (for Entra) tenant are all checked before a user exists.

Entra ID notes (shown on the Users page too):
* Redirect URI to register: ``<dashboard URL>/dashboard/auth/sso/callback`` (Web).
* Roles come from **App roles** (``roles`` claim) or **group claims** (``groups``,
  object ids). Either can be mapped to a dashboard role.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_DISCOVERY_TTL = 3600
_cache: dict[str, tuple[float, Any]] = {}


class OidcError(Exception):
    """Sign-in failed — the message is safe to show the user."""


@dataclass
class Identity:
    subject: str
    email: str
    name: str = ""
    tenant: str = ""
    groups: list[str] = field(default_factory=list)
    app_roles: list[str] = field(default_factory=list)


def issuer_for(cfg) -> str:
    """The issuer URL: Entra from its tenant id, otherwise the configured issuer."""
    provider = (cfg.sso_provider or "entra").lower()
    if provider == "entra":
        tenant = (cfg.sso_tenant_id or "").strip()
        if not tenant:
            raise OidcError("Chưa cấu hình Tenant ID của Microsoft Entra.")
        return f"https://login.microsoftonline.com/{tenant}/v2.0"
    if provider == "google":
        return "https://accounts.google.com"
    issuer = (cfg.sso_issuer or "").strip().rstrip("/")
    if not issuer:
        raise OidcError("Chưa cấu hình Issuer URL của nhà cung cấp OIDC.")
    return issuer


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


async def _get_json(client: httpx.AsyncClient, url: str) -> dict:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < _DISCOVERY_TTL:
        return hit[1]
    resp = await client.get(url, timeout=15)
    if resp.status_code >= 400:
        raise OidcError(f"Không đọc được cấu hình SSO ({resp.status_code}) từ {url}")
    data = resp.json()
    _cache[url] = (time.time(), data)
    return data


async def discovery(client: httpx.AsyncClient, cfg) -> dict:
    return await _get_json(client, issuer_for(cfg) + "/.well-known/openid-configuration")


def new_flow() -> dict:
    """state + nonce + PKCE verifier, kept in a short-lived signed cookie by the caller."""
    verifier = _b64url(secrets.token_bytes(48))
    return {"state": _b64url(secrets.token_bytes(24)), "nonce": _b64url(secrets.token_bytes(24)),
            "verifier": verifier, "at": int(time.time())}


def authorize_url(meta: dict, cfg, flow: dict, redirect_uri: str) -> str:
    challenge = _b64url(hashlib.sha256(flow["verifier"].encode()).digest())
    params = {
        "client_id": cfg.sso_client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": "openid profile email", "state": flow["state"], "nonce": flow["nonce"],
        "code_challenge": challenge, "code_challenge_method": "S256", "response_mode": "query",
    }
    if (cfg.sso_provider or "entra").lower() == "entra":
        params["prompt"] = "select_account"
    return meta["authorization_endpoint"] + "?" + urlencode(params)


async def exchange(client: httpx.AsyncClient, meta: dict, cfg, code: str, flow: dict,
                   redirect_uri: str) -> Identity:
    resp = await client.post(meta["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": cfg.sso_client_id, "client_secret": cfg.sso_client_secret,
        "code_verifier": flow["verifier"],
    }, timeout=20)
    if resp.status_code >= 400:
        try:
            err = resp.json().get("error_description") or resp.json().get("error")
        except ValueError:
            err = resp.text[:200]
        raise OidcError(f"Nhà cung cấp SSO từ chối đổi mã đăng nhập: {str(err)[:300]}")
    id_token = resp.json().get("id_token") or ""
    jwks = await _get_json(client, meta["jwks_uri"])
    claims = verify_id_token(id_token, jwks, issuer=meta["issuer"], audience=cfg.sso_client_id,
                             nonce=flow["nonce"])
    return identity_from(claims, cfg)


def verify_id_token(token: str, jwks: dict, *, issuer: str, audience: str, nonce: str,
                    now: float | None = None, leeway: int = 120) -> dict:
    """Verify an RS256 ID token's signature and claims; return its claims."""
    try:
        head_b, body_b, sig_b = token.split(".")
        header = json.loads(_b64url_decode(head_b))
        claims = json.loads(_b64url_decode(body_b))
        signature = _b64url_decode(sig_b)
    except (ValueError, json.JSONDecodeError) as exc:
        raise OidcError("ID token không hợp lệ.") from exc
    if header.get("alg") != "RS256":
        raise OidcError(f"Thuật toán ký không được chấp nhận: {header.get('alg')}")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if key is None:
        raise OidcError("Không tìm thấy khoá ký của nhà cung cấp (kid).")
    public = rsa.RSAPublicNumbers(int.from_bytes(_b64url_decode(key["e"]), "big"),
                                  int.from_bytes(_b64url_decode(key["n"]), "big")).public_key()
    try:
        public.verify(signature, f"{head_b}.{body_b}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:  # noqa: BLE001 — InvalidSignature and friends
        raise OidcError("Chữ ký ID token không đúng.") from exc
    t = now if now is not None else time.time()
    iss = claims.get("iss", "")
    # Entra's discovery for a tenant lists "{tenantid}" placeholders for multi-tenant
    # apps; a single-tenant issuer matches exactly.
    if "{tenantid}" in issuer:
        issuer = issuer.replace("{tenantid}", str(claims.get("tid", "")))
    if iss != issuer:
        raise OidcError("Issuer của token không khớp.")
    aud = claims.get("aud")
    if audience not in (aud if isinstance(aud, list) else [aud]):
        raise OidcError("Token không dành cho ứng dụng này (aud).")
    if float(claims.get("exp", 0)) + leeway < t:
        raise OidcError("Token đã hết hạn — đăng nhập lại.")
    if float(claims.get("nbf", 0)) - leeway > t:
        raise OidcError("Token chưa có hiệu lực (lệch giờ máy chủ?).")
    if claims.get("nonce") != nonce:
        raise OidcError("Nonce không khớp — thử đăng nhập lại.")
    return claims


def identity_from(claims: dict, cfg) -> Identity:
    email = (claims.get("email") or claims.get("preferred_username") or claims.get("upn")
             or "").strip().lower()
    if not email:
        raise OidcError("Tài khoản SSO không có email — kiểm tra quyền 'email' của ứng dụng.")
    tenant = str(claims.get("tid") or "")
    want_tenant = (cfg.sso_tenant_id or "").strip().lower()
    if (cfg.sso_provider or "entra").lower() == "entra" and want_tenant \
            and want_tenant not in ("common", "organizations") and tenant.lower() != want_tenant:
        raise OidcError("Tài khoản không thuộc tổ chức (tenant) được phép.")
    domains = [d.strip().lower().lstrip("@") for d in (cfg.sso_allowed_domains or []) if d.strip()]
    if domains and email.split("@")[-1] not in domains:
        raise OidcError(f"Email {email} không thuộc domain được phép.")
    return Identity(
        subject=str(claims.get("oid") or claims.get("sub") or ""), email=email,
        name=str(claims.get("name") or ""), tenant=tenant,
        groups=[str(g) for g in claims.get("groups") or []],
        app_roles=[str(r) for r in claims.get("roles") or []],
    )


def mapped_roles(identity: Identity, mapping: dict | None) -> list[str]:
    """Dashboard roles from the identity's groups / app roles (case-insensitive keys)."""
    table = {str(k).strip().lower(): str(v).strip().lower() for k, v in (mapping or {}).items()}
    out: list[str] = []
    for key in [*identity.app_roles, *identity.groups]:
        role = table.get(key.strip().lower())
        if role and role not in out:
            out.append(role)
    return out
