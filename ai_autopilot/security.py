"""Security helpers: RBAC policy, dashboard password hashing, config encryption.

Three concerns live here:

* :class:`RbacPolicy` — who may trigger the autopilot / which skills are allowed.
* Password hashing (:func:`hash_password` / :func:`verify_password`) — for the
  dashboard login. Stored, never reversible; standard library only.
* Config encryption (:func:`encrypt_bytes` / :func:`decrypt_bytes`) — for the
  *full* config export, which deliberately contains secrets (ADO PAT, SMTP/Zalo
  tokens, per-tenant PATs), so the download is encrypted at rest under a password.

Both password paths derive keys with PBKDF2-HMAC-SHA256 at a high iteration count
so a weak password is still expensive to brute-force offline.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import WorkItemInfo


def _identity_matches(allowed: list[str], identity: str) -> bool:
    """Whole-identity match, NOT substring.

    Substring matching (``"ann" in "roseanne@x.com"``) silently widened every
    allowlist. Match the full identity or its email part exactly instead. ADO
    identities often arrive as ``"Display Name <email@host>"`` — test both forms.
    """
    ident = (identity or "").lower().strip()
    email = ident
    if "<" in ident and ">" in ident:
        email = ident[ident.index("<") + 1 : ident.index(">")].strip()
    candidates = {ident, email} - {""}
    return any((a or "").lower().strip() in candidates for a in allowed if (a or "").strip())


def _norm_skill(skill: str) -> str:
    """Normalise a skill to its bare command token (drop leading ``/`` and args)."""
    token = (skill or "").strip().split()
    return token[0].lower().lstrip("/") if token else ""


class RbacPolicy:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("security.rbac")

    def is_user_allowed(self, item: WorkItemInfo) -> bool:
        if not self._config.allowed_users:
            return True
        created_by = item.created_by or ""
        allowed = _identity_matches(self._config.allowed_users, created_by)
        if not allowed:
            self._log.warning("rbac: user not allowed", id=item.id, user=created_by)
        return allowed

    def is_skill_allowed(self, skill: str) -> bool:
        if not self._config.allowed_skills:
            return True
        want = _norm_skill(skill)
        return bool(want) and any(_norm_skill(s) == want for s in self._config.allowed_skills)

    def is_approver(self, user_name: str) -> bool:
        if not self._config.approver_users:
            return True
        return _identity_matches(self._config.approver_users, user_name)


# ── Password hashing (stdlib only) ────────────────────────────────────────────
_HASH_ALGO = "pbkdf2_sha256"
_HASH_ITERATIONS = 480_000  # OWASP-recommended floor for PBKDF2-HMAC-SHA256
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash ``password`` into a self-describing, storable string.

    Format: ``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``. The salt is
    random per call, so the same password hashes differently each time.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _HASH_ITERATIONS)
    return (
        f"{_HASH_ALGO}${_HASH_ITERATIONS}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    )


def verify_password(password: str, stored: str) -> bool:
    """True if ``password`` matches the ``stored`` hash from :func:`hash_password`.

    Returns ``False`` (never raises) for any malformed / empty / wrong-algorithm
    stored value, so callers can treat it as a plain allow/deny check. The final
    comparison is constant-time.
    """
    if not stored:
        return False
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != _HASH_ALGO:
            return False
        iterations = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return secrets.compare_digest(digest, expected)


# ── Config encryption (cryptography / Fernet) ─────────────────────────────────
# A short magic header makes the file self-identifying and lets us reject an
# unrelated / corrupt file with a clear error before attempting decryption.
_ENC_HEADER = b"AUTOPILOT-ENC-v1"
_ENC_ITERATIONS = 480_000
_ENC_SALT_BYTES = 16


def _derive_fernet_key(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte urlsafe-base64 Fernet key from a password + salt."""
    kdf = PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt, iterations=_ENC_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def encrypt_bytes(data: bytes, password: str) -> bytes:
    r"""Encrypt ``data`` with a key derived from ``password``.

    Output envelope (newline-separated): ``<header>\n<salt_b64>\n<fernet_token>``.
    The salt is embedded so :func:`decrypt_bytes` needs only the password.
    """
    salt = secrets.token_bytes(_ENC_SALT_BYTES)
    token = Fernet(_derive_fernet_key(password, salt)).encrypt(data)
    return b"\n".join((_ENC_HEADER, base64.b64encode(salt), token))


def decrypt_bytes(blob: bytes, password: str) -> bytes:
    """Reverse :func:`encrypt_bytes`.

    Raises :class:`ValueError` if the file is not a recognised envelope or the
    password is wrong (so the two failure modes are indistinguishable to an
    attacker probing the ciphertext).
    """
    try:
        header, salt_b64, token = blob.split(b"\n", 2)
    except ValueError as exc:
        raise ValueError("not a recognised AI-Autopilot encrypted config") from exc
    if header != _ENC_HEADER:
        raise ValueError("not a recognised AI-Autopilot encrypted config")
    try:
        salt = base64.b64decode(salt_b64)
        return Fernet(_derive_fernet_key(password, salt)).decrypt(token)
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ValueError("wrong password or corrupted file") from exc
