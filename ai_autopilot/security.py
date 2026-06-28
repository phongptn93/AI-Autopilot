"""RBAC policy checks (ported from ``RbacPolicy``)."""

from __future__ import annotations

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import WorkItemInfo


class RbacPolicy:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("security.rbac")

    def is_user_allowed(self, item: WorkItemInfo) -> bool:
        if not self._config.allowed_users:
            return True
        created_by = item.created_by or ""
        allowed = any(u.lower() in created_by.lower() for u in self._config.allowed_users)
        if not allowed:
            self._log.warning("rbac: user not allowed", id=item.id, user=created_by)
        return allowed

    def is_skill_allowed(self, skill: str) -> bool:
        if not self._config.allowed_skills:
            return True
        return any(s.lower() in skill.lower() for s in self._config.allowed_skills)

    def is_approver(self, user_name: str) -> bool:
        if not self._config.approver_users:
            return True
        return any(u.lower() in user_name.lower() for u in self._config.approver_users)
