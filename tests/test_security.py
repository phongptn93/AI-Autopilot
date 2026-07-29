"""Tests for password hashing and config encryption helpers."""

from __future__ import annotations

import pytest

from ai_autopilot import security


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
