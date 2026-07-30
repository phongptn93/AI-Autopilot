"""Multi-tenant configuration (ported from ``TenantManager``/``TenantContext``)."""

from __future__ import annotations

from ai_autopilot.config import Settings, TenantConfig
from ai_autopilot.logging_config import get_logger


class TenantManager:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("multitenant.manager")

    def get_tenants(self) -> list[TenantConfig]:
        if self._config.tenants:
            self._log.info("multi-tenant mode", count=len(self._config.tenants))
            return self._config.tenants

        # Single-tenant fallback: synthesise a virtual tenant from the root config.
        return [
            TenantConfig(
                name=self._config.ado_project,
                ado_organization=self._config.ado_organization,
                ado_project=self._config.ado_project,
                ado_pat=self._config.ado_pat,
                trigger_tag=self._config.trigger_tag,
                processed_tag=self._config.processed_tag,
                repo_working_directory=self._config.repo_working_directory,
                base_branch=self._config.base_branch,
                repos=self._config.repos,
                teams_webhook_url=self._config.teams_webhook_url,
                teams_webhook_urls=list(self._config.teams_webhook_urls),
            )
        ]
