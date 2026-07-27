"""RBAC policy checks (ported from ``RbacPolicy``)."""

from __future__ import annotations

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
