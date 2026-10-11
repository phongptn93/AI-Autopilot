"""👥 Users, roles and company SSO — the guard, the matrix, OIDC verification, lockouts."""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from starlette.testclient import TestClient

from ai_autopilot import oidc, rbac, security, user_auth
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings

# ── rbac ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method,path,perm", [
    ("GET", "/dashboard/board", "view"),
    ("GET", "/dashboard/settings/reveal/ado_pat", "secrets.view"),
    ("POST", "/dashboard/settings", "settings.edit"),
    ("POST", "/dashboard/board/run", "runs.start"),
    ("POST", "/dashboard/sessions/abc-1/cancel", "runs.start"),
    ("POST", "/dashboard/feedback/42", "runs.start"),
    ("POST", "/dashboard/board/move", "runs.control"),
    ("POST", "/dashboard/history/clear", "runs.control"),
    ("POST", "/dashboard/inbox/resume", "runs.control"),
    ("POST", "/dashboard/review/12/challenge", "review.run"),
    ("POST", "/dashboard/review/12/comment", "tracker.write"),
    ("POST", "/dashboard/users/3/update", "users.manage"),
    ("GET", "/dashboard/users", "users.manage"),
    ("POST", "/dashboard/login", ""),
    ("GET", "/dashboard/auth/sso/callback", ""),
    ("POST", "/dashboard/some-new-page", rbac.ADMIN),          # deny by default
])
def test_route_permissions(method, path, perm):
    assert rbac.required(method, path) == perm


def test_roles_are_customisable_but_admin_is_not():
    custom = {"ba": {"permissions": ["view"], "known": list(rbac.PERMISSIONS)},
              "pm": {"label": "PM", "permissions": ["view", "x"]},
              "admin": {"permissions": []}}
    table = rbac.roles(custom)
    assert table["ba"].permissions == ["view"] and table["pm"].permissions == ["view"]
    assert table["ba"].customized and not table["qc"].customized
    assert table["admin"].permissions == ["*"]
    perms = rbac.permissions_of(["ba", "qc"], custom)
    assert rbac.allowed(perms, "security.run") and not rbac.allowed(perms, "settings.edit")
    assert not rbac.allowed(perms, rbac.ADMIN) and rbac.allowed({"*"}, rbac.ADMIN)


def test_qc_and_ba_can_run_their_own_stage_but_not_steer_the_machine():
    for role in ("qc", "ba"):
        perms = rbac.permissions_of([role], {})
        assert rbac.allowed(perms, rbac.required("POST", "/dashboard/board/run"))
        assert not rbac.allowed(perms, rbac.required("POST", "/dashboard/history/clear"))
        assert not rbac.allowed(perms, rbac.required("POST", "/dashboard/board/move"))


def test_a_role_saved_before_a_permission_existed_gets_its_default():
    # Saved by an older build: no "known", so runs.start was never shown to the admin.
    legacy = {"qc": {"permissions": ["view", "review.run"]},
              "lead": {"permissions": ["view", "runs.control"]}}
    table = rbac.roles(legacy)
    assert "runs.start" in table["qc"].permissions
    assert "runs.start" in table["lead"].permissions          # implied by runs.control
    # Saved by this build with the box unticked: that is a real "no".
    table = rbac.roles({"qc": {"permissions": ["view"], "known": list(rbac.PERMISSIONS)}})
    assert table["qc"].permissions == ["view"]


def test_matrix_save_records_known_and_reset_restores_default(tmp_path, monkeypatch):
    from ai_autopilot.dashboard.routes import users as users_routes

    saved: dict = {}
    monkeypatch.setattr(users_routes, "config_file_path", lambda: tmp_path / "c.yaml")
    monkeypatch.setattr(users_routes.settings_form, "save_to_yaml",
                        lambda _p, d: saved.update(d))
    monkeypatch.setattr(users_routes.settings_form, "apply_to_config", lambda *_a: None)
    import asyncio
    from types import SimpleNamespace

    class _Form(dict):
        def getlist(self, k):
            return []

    class _Audit:
        async def record(self, **_kw):
            return None

    form = _Form({"perm_qc_view": "on", "reset_ba": "on", "perm_ba_view": "on"})
    req = SimpleNamespace(
        form=lambda: asyncio.sleep(0, form),
        app=SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(
            config=SimpleNamespace(user_roles={}), audit_repo=_Audit()))))
    route = next(r for r in users_routes.create_router().routes if r.path == "/users/roles")
    asyncio.run(route.endpoint(req))
    roles = saved["user_roles"]
    assert roles["qc"]["permissions"] == ["view"]
    assert set(roles["qc"]["known"]) == set(rbac.PERMISSIONS)
    assert "ba" not in roles and "admin" not in roles


# ── OIDC token verification ───────────────────────────────────────────────────

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.public_key().public_numbers()
    jwk = {"kid": "k1", "kty": "RSA", "n": _b64(nums.n.to_bytes(256, "big")),
           "e": _b64(nums.e.to_bytes(3, "big"))}
    return key, {"keys": [jwk]}


def _token(key, claims, kid="k1", alg="RS256"):
    head = _b64(json.dumps({"alg": alg, "kid": kid}).encode())
    body = _b64(json.dumps(claims).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


_ISS = "https://login.microsoftonline.com/tid-1/v2.0"


def _claims(**over):
    c = {"iss": _ISS, "aud": "client-1", "exp": time.time() + 600, "nbf": time.time() - 5,
         "nonce": "n1", "tid": "tid-1", "oid": "o1", "preferred_username": "Ba@CongTy.vn",
         "name": "Ba One", "roles": ["AIAutopilot.BA"], "groups": ["g-qc"]}
    c.update(over)
    return c


def test_valid_token_is_accepted():
    key, jwks = _keypair()
    claims = oidc.verify_id_token(_token(key, _claims()), jwks, issuer=_ISS,
                                  audience="client-1", nonce="n1")
    cfg = SimpleNamespace(sso_provider="entra", sso_tenant_id="tid-1",
                          sso_allowed_domains=["congty.vn"])
    ident = oidc.identity_from(claims, cfg)
    assert ident.email == "ba@congty.vn" and ident.app_roles == ["AIAutopilot.BA"]
    assert oidc.mapped_roles(ident, {"aiautopilot.ba": "ba", "g-qc": "QC"}) == ["ba", "qc"]


@pytest.mark.parametrize("change,needle", [
    ({"aud": "other"}, "aud"), ({"iss": "https://evil/v2.0"}, "Issuer"),
    ({"exp": time.time() - 3600}, "hết hạn"), ({"nonce": "zz"}, "Nonce"),
])
def test_bad_claims_are_refused(change, needle):
    key, jwks = _keypair()
    with pytest.raises(oidc.OidcError, match=needle):
        oidc.verify_id_token(_token(key, _claims(**change)), jwks, issuer=_ISS,
                             audience="client-1", nonce="n1")


def test_forged_signature_and_alg_none_are_refused():
    key, jwks = _keypair()
    other, _ = _keypair()
    with pytest.raises(oidc.OidcError, match="Chữ ký"):
        oidc.verify_id_token(_token(other, _claims()), jwks, issuer=_ISS, audience="client-1",
                             nonce="n1")
    head = _b64(json.dumps({"alg": "none", "kid": "k1"}).encode())
    body = _b64(json.dumps(_claims()).encode())
    with pytest.raises(oidc.OidcError, match="Thuật toán"):
        oidc.verify_id_token(f"{head}.{body}.", jwks, issuer=_ISS, audience="client-1",
                             nonce="n1")


def test_wrong_tenant_and_domain_are_refused():
    cfg = SimpleNamespace(sso_provider="entra", sso_tenant_id="tid-1", sso_allowed_domains=[])
    with pytest.raises(oidc.OidcError, match="tenant"):
        oidc.identity_from(_claims(tid="tid-2"), cfg)
    cfg2 = SimpleNamespace(sso_provider="entra", sso_tenant_id="tid-1",
                           sso_allowed_domains=["congty.vn"])
    with pytest.raises(oidc.OidcError, match="domain"):
        oidc.identity_from(_claims(preferred_username="x@gmail.com"), cfg2)


# ── the guard, end to end ─────────────────────────────────────────────────────

def _app(tmp_path, monkeypatch, **cfg):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "c.yaml"))
    user_auth._key_cache.clear()
    user_auth._user_cache.clear()
    base = dict(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'u.db'}",
                auth_mode="users",
                dashboard_auth_password_hash=security.hash_password("shared-pw-123"))
    base.update(cfg)
    return create_app(Settings(**base))


def _login(client, email, pw):
    return client.post("/dashboard/login", data={"email": email, "password": pw},
                       follow_redirects=False)


def test_users_mode_end_to_end(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        c = app.state.container
        import asyncio

        async def seed():
            await c.user_repo.create(email="admin@ct.vn", roles=["admin"],
                                     password_hash=security.hash_password("admin-pass-1"))
            await c.user_repo.create(email="view@ct.vn", roles=["viewer"],
                                     password_hash=security.hash_password("view-pass-12"))
        asyncio.run(seed())

        # anonymous → login page; not a 200 board
        r = client.get("/dashboard/board", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/dashboard/login")
        assert "Email" in client.get("/dashboard/login").text

        assert _login(client, "view@ct.vn", "wrong").status_code == 401
        assert _login(client, "view@ct.vn", "view-pass-12").status_code == 303
        assert client.get("/dashboard/board").status_code == 200          # viewer may look
        r = client.post("/dashboard/settings", data={}, follow_redirects=False)
        assert r.status_code == 403 and "Sửa thiết lập" in r.text
        assert client.get("/dashboard/users", follow_redirects=False).status_code == 403
        client.cookies.clear()

        assert _login(client, "admin@ct.vn", "admin-pass-1").status_code == 303
        page = client.get("/dashboard/users").text
        assert "view@ct.vn" in page and "Vai &amp; quyền" in page
        # the last admin cannot demote itself
        admin = asyncio.run(c.user_repo.by_email("admin@ct.vn"))
        r = client.post(f"/dashboard/users/{admin.id}/update",
                        data={"action": "save", "roles": ["viewer"]}, follow_redirects=False)
        assert asyncio.run(c.user_repo.by_email("admin@ct.vn")).roles_json == '["admin"]'
        # actions are audited under the person's email
        client.post("/dashboard/users/add", data={"email": "qc@ct.vn", "roles": ["qc"]})
        events = asyncio.run(c.audit_repo.recent(20))
        assert any(e.actor == "admin@ct.vn" and e.action == "user.add" for e in events)
        # disabling takes effect on the very next request
        viewer = asyncio.run(c.user_repo.by_email("view@ct.vn"))
        client.post(f"/dashboard/users/{viewer.id}/update", data={"action": "disable"})
        client.cookies.clear()
        _login(client, "view@ct.vn", "view-pass-12")
        assert client.get("/dashboard/board", follow_redirects=False).status_code == 303


def test_breakglass_and_mode_switch_guard(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        r = client.post("/dashboard/login", data={"password": "shared-pw-123", "breakglass": "1"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert client.get("/dashboard/users").status_code == 200          # emergency admin
        # SSO cannot be switched on without its app details
        r = client.post("/dashboard/users/mode", data={"mode": "sso"}, follow_redirects=False)
        assert app.state.container.config.auth_mode == "users"


def test_switching_on_without_any_admin_or_breakglass_is_refused(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, auth_mode="password", dashboard_auth_password_hash="",
               dashboard_allow_remote_without_auth=True)
    with TestClient(app) as client:
        client.post("/dashboard/users/mode", data={"mode": "users"})
        assert app.state.container.config.auth_mode == "password"


def test_sso_callback_creates_a_pending_user(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, auth_mode="sso", sso_tenant_id="tid-1",
               sso_client_id="client-1", sso_client_secret="s",
               sso_role_map={"AIAutopilot.BA": "ba"})

    async def disc(http, cfg):
        return {"issuer": _ISS, "authorization_endpoint": "https://idp/authorize",
                "token_endpoint": "https://idp/token", "jwks_uri": "https://idp/keys"}

    async def exch(http, meta, cfg, code, flow, uri):
        return oidc.identity_from(_claims(nonce=flow["nonce"]), cfg)

    monkeypatch.setattr(oidc, "discovery", disc)
    monkeypatch.setattr(oidc, "exchange", exch)
    with TestClient(app) as client:
        r = client.get("/dashboard/auth/sso/start", follow_redirects=False)
        assert r.headers["location"].startswith("https://idp/authorize?")
        assert "code_challenge_method=S256" in r.headers["location"]
        state = r.headers["location"].split("state=")[1].split("&")[0]
        # a forged state is refused
        bad = client.get("/dashboard/auth/sso/callback?code=c&state=nope", follow_redirects=False)
        assert "error=" in bad.headers["location"]
        r = client.get("/dashboard/auth/sso/start", follow_redirects=False)
        state = r.headers["location"].split("state=")[1].split("&")[0]
        r = client.get(f"/dashboard/auth/sso/callback?code=c&state={state}", follow_redirects=False)
        assert r.headers["location"] == "/dashboard/login?pending=1"
        import asyncio
        row = asyncio.run(app.state.container.user_repo.by_email("ba@congty.vn"))
        assert row.status == "pending" and json.loads(row.roles_json) == ["ba"]
        assert client.get("/dashboard/board", follow_redirects=False).headers["location"] \
            == "/dashboard/login?pending=1"
        assert "chờ Admin duyệt" in client.get("/dashboard/login?pending=1").text


def test_password_mode_is_untouched(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, auth_mode="password")
    with TestClient(app) as client:
        assert client.get("/dashboard/board", follow_redirects=False).status_code in (302, 303, 401)
        r = client.post("/dashboard/login", data={"password": "shared-pw-123"},
                        follow_redirects=False)
        assert r.status_code == 303 and client.get("/dashboard/settings").status_code == 200


def test_keypair_helper_exports_pem():          # keeps the serialization import honest
    key, _ = _keypair()
    assert key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).startswith(b"-----BEGIN")
