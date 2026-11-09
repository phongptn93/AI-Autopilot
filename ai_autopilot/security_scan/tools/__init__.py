"""Scanner adapters. Import :func:`registry` to get them by name."""

from __future__ import annotations

from ai_autopilot.security_scan.tools.base import ScannerAdapter, ToolRun, ToolStatus
from ai_autopilot.security_scan.tools.builtin import BuiltinScanner
from ai_autopilot.security_scan.tools.gitleaks import GitleaksScanner
from ai_autopilot.security_scan.tools.pkgguard import PkgGuardScanner
from ai_autopilot.security_scan.tools.sca import ScaScanner
from ai_autopilot.security_scan.tools.semgrep import SemgrepScanner
from ai_autopilot.security_scan.tools.testguard import TestGuardScanner

# Order is the order the runner starts them; results are merged by fingerprint so it
# does not affect output, only the log.
ALL_TOOLS = ("builtin", "gitleaks", "semgrep", "sca", "pkgguard", "testguard")


def registry(config=None) -> dict[str, ScannerAdapter]:
    """Every adapter, keyed by the name used in config / ``--tools``."""
    return {
        "builtin": BuiltinScanner(),
        "gitleaks": GitleaksScanner(),
        "semgrep": SemgrepScanner(
            rulesets=list(getattr(config, "semgrep_config", None) or []) if config else []
        ),
        "sca": ScaScanner(),
        "testguard": TestGuardScanner(),
        "pkgguard": PkgGuardScanner(
            min_age_days=int(getattr(config, "pkgguard_min_age_days", 14)),
            internal_prefixes=list(
                getattr(config, "pkgguard_internal_prefixes", None) or []),
        ),
    }


__all__ = [
    "ALL_TOOLS", "ScannerAdapter", "ToolRun", "ToolStatus", "registry",
    "BuiltinScanner", "GitleaksScanner", "PkgGuardScanner", "ScaScanner", "SemgrepScanner",
    "TestGuardScanner",
]
