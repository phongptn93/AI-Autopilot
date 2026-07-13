"""Readiness/health checks (ported from the .NET HealthChecks).

Each check is an async callable returning a ``HealthCheckResult``. The aggregate
status mirrors ASP.NET's Healthy / Degraded / Unhealthy semantics.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from ai_autopilot.ado.auth import AdoAuthService
from ai_autopilot.logging_config import get_logger

_log = get_logger("health")
_MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB


class HealthStatus(StrEnum):
    HEALTHY = "Healthy"
    DEGRADED = "Degraded"
    UNHEALTHY = "Unhealthy"


@dataclass
class HealthCheckResult:
    name: str
    status: HealthStatus
    description: str = ""
    duration_ms: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)


async def check_ado(auth: AdoAuthService, http: httpx.AsyncClient) -> HealthCheckResult:
    start = time.monotonic()
    try:
        headers = await auth.get_auth_header()
        resp = await http.get(
            "https://dev.azure.com/_apis/projects?$top=1&api-version=7.1", headers=headers
        )
        status = (
            HealthStatus.HEALTHY if resp.status_code < 400 else HealthStatus.DEGRADED
        )
        desc = (
            "ADO API reachable"
            if status is HealthStatus.HEALTHY
            else f"ADO API returned {resp.status_code}"
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("ado health check failed", error=str(exc))
        return _result("ado", HealthStatus.UNHEALTHY, "ADO API unreachable", start)
    return _result("ado", status, desc, start)


async def check_claude() -> HealthCheckResult:
    """The Agent SDK bundles the Claude Code CLI, so we just confirm it imports."""
    start = time.monotonic()
    try:
        import claude_agent_sdk  # noqa: F401

        return _result("claude", HealthStatus.HEALTHY, "claude-agent-sdk available", start)
    except Exception as exc:  # noqa: BLE001
        _log.warning("claude health check failed", error=str(exc))
        return _result("claude", HealthStatus.UNHEALTHY, "claude-agent-sdk not importable", start)


def check_disk() -> HealthCheckResult:
    start = time.monotonic()
    usage = shutil.disk_usage(".")
    free_gb = usage.free / (1024**3)
    data = {
        "freeGB": round(free_gb, 2),
        "totalGB": round(usage.total / (1024**3), 2),
    }
    if usage.free >= _MIN_FREE_BYTES:
        return _result("disk", HealthStatus.HEALTHY, f"{free_gb:.1f} GB free", start, data)
    return _result(
        "disk", HealthStatus.UNHEALTHY, f"Only {free_gb:.1f} GB free (min 1 GB)", start, data
    )


def aggregate(results: list[HealthCheckResult]) -> HealthStatus:
    if any(r.status is HealthStatus.UNHEALTHY for r in results):
        return HealthStatus.UNHEALTHY
    if any(r.status is HealthStatus.DEGRADED for r in results):
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


def _result(
    name: str,
    status: HealthStatus,
    description: str,
    start: float,
    data: dict[str, Any] | None = None,
) -> HealthCheckResult:
    return HealthCheckResult(
        name=name,
        status=status,
        description=description,
        duration_ms=(time.monotonic() - start) * 1000,
        data=data or {},
    )
