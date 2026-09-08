"""Health checks: what they ask, and what an answer means."""

from __future__ import annotations


async def test_the_ado_check_asks_the_organization_not_the_bare_host():
    """`dev.azure.com/_apis/projects` with no org segment is a 404 for everyone,
    always, whatever the credentials.

    It reported "Degraded — ADO API returned 404" on a machine whose poller was
    querying ADO successfully in the same second, which sends a reader hunting a
    token that was never the problem.
    """
    from types import SimpleNamespace

    from ai_autopilot import health
    from ai_autopilot.ado.auth import AdoAuthService
    from ai_autopilot.config import Settings

    asked: list[str] = []

    class _Http:
        async def get(self, url, headers=None):
            asked.append(url)
            return SimpleNamespace(status_code=200)

    auth = AdoAuthService(Settings(ado_organization="https://dev.azure.com/acme/", ado_pat="x"))
    assert auth.organization == "https://dev.azure.com/acme"
    result = await health.check_ado(auth, _Http())
    assert asked == ["https://dev.azure.com/acme/_apis/projects?$top=1&api-version=7.1"]
    assert result.status is health.HealthStatus.HEALTHY

    # No organization at all is a configuration answer, not a mystery 404.
    blank = await health.check_ado(AdoAuthService(Settings(ado_pat="x")), _Http())
    assert blank.status is health.HealthStatus.DEGRADED
    assert "organization" in blank.description
