"""🔎 Audit trail: sign-in failures, logouts, denials, source IP — and the OCSF export."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ai_autopilot import audit_ocsf, rbac, security, user_auth
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.dashboard.routes import auth as auth_routes


def _ev(action, actor="a@ct.vn", target="", detail="", src_ip="10.0.0.5", id=1):
    return SimpleNamespace(id=id, at=datetime(2026, 10, 31, 8, 0, 0), actor=actor,
                           source="dashboard", action=action, target=target,
                           detail=detail, src_ip=src_ip)


# ── mapping ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action,cls,act", [
    ("dashboard.login", 3002, 1),
    ("dashboard.login_failed", 3002, 1),
    ("dashboard.logout", 3002, 2),
    ("dashboard.denied", 6004, 2),
    ("user.add", 3001, 1),
    ("user.disable", 3001, 5),
    ("user.delete", 3001, 6),
    ("user.password", 3001, 3),
    ("user.approve", 3005, 1),
    ("user.roles", 3004, 3),
    ("config.saved", 3004, 3),
    ("item.resumed", 6003, 99),
])
def test_actions_map_to_ocsf_classes(action, cls, act):
    out = audit_ocsf.to_ocsf(_ev(action))
    assert (out["class_uid"], out["activity_id"]) == (cls, act)
    assert out["type_uid"] == cls * 100 + act and out["category_uid"] == cls // 1000
    assert out["metadata"]["version"] == audit_ocsf.OCSF_VERSION
    assert out["unmapped"]["action"] == action          # the original is never lost


def test_failed_login_is_a_failure_with_source_and_reason():
    out = audit_ocsf.to_ocsf(_ev("dashboard.login_failed", actor="x@ct.vn",
                                 detail="local: wrong email or password"))
    assert out["status_id"] == 2 and out["severity_id"] == 2
    assert out["status_detail"] == "local: wrong email or password"
    assert out["src_endpoint"] == {"ip": "10.0.0.5"}
    assert out["user"]["email_addr"] == "x@ct.vn" and out["auth_protocol"] == "Password"


def test_time_is_utc_epoch_millis():
    out = audit_ocsf.to_ocsf(_ev("dashboard.login"))
    assert out["time"] == 1793433600000                  # 2026-10-31T08:00:00Z


def test_denied_and_account_change_carry_their_objects():
    denied = audit_ocsf.to_ocsf(_ev("dashboard.denied", target="POST /dashboard/settings",
                                    detail="settings.edit"))
    assert denied["http_request"] == {"http_method": "POST",
                                      "url": {"path": "/dashboard/settings"}}
    granted = audit_ocsf.to_ocsf(_ev("user.approve", target="qc@ct.vn", detail="qc,ba"))
    assert granted["user"]["name"] == "qc@ct.vn" and granted["privileges"] == ["qc", "ba"]


def test_ndjson_is_one_event_per_line():
    text = audit_ocsf.dumps_ndjson([_ev("dashboard.login", id=1), _ev("user.add", id=2)],
                                   product_version="9.9.9")
    lines = text.splitlines()
    assert len(lines) == 2 and text.endswith("\n")
    assert [json.loads(x)["metadata"]["uid"] for x in lines] == ["1", "2"]
    assert json.loads(lines[0])["metadata"]["product"]["version"] == "9.9.9"


def test_export_needs_users_manage():
    assert rbac.required("GET", "/dashboard/audit/export.ocsf") == "users.manage"
    assert rbac.required("GET", "/dashboard/audit") == "view"


# ── end to end ────────────────────────────────────────────────────────────────

def _app(tmp_path, monkeypatch, **cfg):
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(tmp_path / "c.yaml"))
    user_auth._key_cache.clear()
    user_auth._user_cache.clear()
    auth_routes._fail_seen.clear()
    base = dict(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'u.db'}",
                auth_mode="users",
                dashboard_auth_password_hash=security.hash_password("shared-pw-123"))
    base.update(cfg)
    return create_app(Settings(**base))


def _login(client, email, pw):
    return client.post("/dashboard/login", data={"email": email, "password": pw},
                       follow_redirects=False)


def test_sign_in_trail_end_to_end(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        c = app.state.container

        async def seed():
            await c.user_repo.create(email="admin@ct.vn", roles=["admin"],
                                     password_hash=security.hash_password("admin-pass-1"))
            await c.user_repo.create(email="view@ct.vn", roles=["viewer"],
                                     password_hash=security.hash_password("view-pass-12"))
        asyncio.run(seed())

        assert _login(client, "view@ct.vn", "wrong").status_code == 401
        assert _login(client, "view@ct.vn", "view-pass-12").status_code == 303
        assert client.post("/dashboard/settings", data={},
                           follow_redirects=False).status_code == 403
        assert client.get("/dashboard/audit/export.ocsf",
                          follow_redirects=False).status_code == 403
        client.post("/dashboard/logout", follow_redirects=False)

        events = asyncio.run(c.audit_repo.recent(50))
        by = {e.action: e for e in reversed(events)}
        failed = by["dashboard.login_failed"]
        assert failed.actor == "view@ct.vn" and "wrong" in failed.detail
        assert failed.src_ip == "testclient"
        assert by["dashboard.login"].src_ip == "testclient"
        denied = [e for e in events if e.action == "dashboard.denied"]
        assert {e.target for e in denied} == {"POST /dashboard/settings",
                                              "GET /dashboard/audit/export.ocsf"}
        assert all(e.actor == "view@ct.vn" for e in denied)
        assert by["dashboard.logout"].actor == "view@ct.vn"

        # an admin can export, and the export itself is audited
        assert _login(client, "admin@ct.vn", "admin-pass-1").status_code == 303
        r = client.get("/dashboard/audit/export.ocsf")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
        assert "attachment" in r.headers["content-disposition"]
        rows = [json.loads(x) for x in r.text.splitlines()]
        assert any(x["class_uid"] == 3002 and x["status_id"] == 2 for x in rows)
        assert any(x["class_uid"] == 6004 for x in rows)
        assert "⬇ OCSF" in client.get("/dashboard/audit").text
        assert any(e.action == "dashboard.audit_export"
                   for e in asyncio.run(c.audit_repo.recent(5)))


def test_failed_sign_ins_are_capped_per_address(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        for _ in range(auth_routes._FAIL_PER_PEER + 5):
            assert _login(client, "nobody@ct.vn", "x").status_code == 401
        events = asyncio.run(app.state.container.audit_repo.recent(
            100, action="dashboard.login_failed"))
        assert len(events) == auth_routes._FAIL_PER_PEER


def test_shared_password_failure_is_recorded_in_password_mode(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, auth_mode="password")
    with TestClient(app) as client:
        assert client.post("/dashboard/login", data={"password": "nope"},
                           follow_redirects=False).status_code == 401
        ev = asyncio.run(app.state.container.audit_repo.recent(5))[0]
        assert ev.action == "dashboard.login_failed" and ev.detail == "shared password"
