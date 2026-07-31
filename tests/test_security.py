"""Tests for password hashing and config encryption helpers."""

from __future__ import annotations

import time

import pytest

from ai_autopilot import security
from ai_autopilot.config import Settings


# ── Password hashing ──────────────────────────────────────────────────────────
def test_hash_password_verifies_and_is_salted():
    h1 = security.hash_password("s3cret")
    h2 = security.hash_password("s3cret")
    assert h1 != h2  # random salt → different hashes for the same password
    assert h1.startswith("pbkdf2_sha256$")
    assert security.verify_password("s3cret", h1)
    assert security.verify_password("s3cret", h2)


def test_verify_password_rejects_wrong_and_malformed():
    stored = security.hash_password("right")
    assert not security.verify_password("wrong", stored)
    # malformed / empty stored values never raise, just return False
    assert not security.verify_password("x", "")
    assert not security.verify_password("x", "not-a-valid-hash")
    assert not security.verify_password("x", "md5$1$a$b")  # wrong algorithm


# ── Config encryption ─────────────────────────────────────────────────────────
def test_encrypt_decrypt_round_trip():
    data = b"ado_pat: super-secret\nsmtp_password: hunter2\n"
    blob = security.encrypt_bytes(data, "Export#12345")
    assert blob.startswith(b"AUTOPILOT-ENC-v1")
    assert b"super-secret" not in blob  # ciphertext must not leak plaintext
    assert security.decrypt_bytes(blob, "Export#12345") == data


def test_decrypt_wrong_password_raises_valueerror():
    blob = security.encrypt_bytes(b"payload", "correct")
    with pytest.raises(ValueError):
        security.decrypt_bytes(blob, "incorrect")


def test_decrypt_unrecognised_blob_raises_valueerror():
    with pytest.raises(ValueError):
        security.decrypt_bytes(b"totally not our envelope", "whatever")


# ── Dashboard session token ───────────────────────────────────────────────────

def _cfg(password="s3cret"):
    return Settings(dashboard_auth_password_hash=security.hash_password(password))


def test_session_token_round_trips():
    cfg = _cfg()
    assert security.verify_session_token(security.make_session_token(cfg), cfg)


def test_expired_session_is_rejected():
    cfg = _cfg()
    token = security.make_session_token(cfg, ttl_hours=1)
    assert not security.verify_session_token(token, cfg, now=time.time() + 3601 + 1)


def test_tampered_expiry_cannot_extend_a_session():
    """The signature covers the expiry, so pushing the timestamp out invalidates it —
    otherwise the cookie would be trivially self-renewing."""
    cfg = _cfg()
    _, _, sig = security.make_session_token(cfg).partition(".")
    assert not security.verify_session_token(f"{int(time.time()) + 10**6}.{sig}", cfg)


def test_changing_the_password_invalidates_existing_sessions():
    """The signing key is derived from the stored hash, so a rotation logs everyone out
    without needing a session store to purge."""
    old = _cfg("old-one")
    token = security.make_session_token(old)
    assert security.verify_session_token(token, old)
    assert not security.verify_session_token(token, _cfg("new-one"))


def test_no_password_means_no_sessions():
    """Nothing to authenticate → no token is issued, and none is ever accepted (so an
    unlocked instance cannot be handed a cookie minted against an empty secret)."""
    open_cfg = Settings()
    assert security.make_session_token(open_cfg) == ""
    assert not security.verify_session_token("1.deadbeef", open_cfg)


def test_malformed_cookies_are_rejected_without_raising():
    cfg = _cfg()
    for junk in ("", None, "no-dot", "abc.def", ".", "999", "x.y.z"):
        assert not security.verify_session_token(junk, cfg)
