"""Scanner adapters. Import :func:`registry` to get them by name."""

from __future__ import annotations

from ai_autopilot.security_scan.tools.base import ScannerAdapter, ToolRun, ToolStatus
from ai_autopilot.security_scan.tools.builtin import BuiltinScanner
from ai_autopilot.security_scan.tools.gitleaks import GitleaksScanner
from ai_autopilot.security_scan.tools.sca import ScaScanner
from ai_autopilot.security_scan.tools.semgrep import SemgrepScanner

# Order is the order the runner starts them; results are merged by fingerprint so it
# does not affect output, only the log.
ALL_TOOLS = ("builtin", "gitleaks", "semgrep", "sca")


def registry(config=None) -> dict[str, ScannerAdapter]:
    """Every adapter, keyed by the name used in config / ``--tools``."""
    return {
        "builtin": BuiltinScanner(),
        "gitleaks": GitleaksScanner(),
        "semgrep": SemgrepScanner(
            rulesets=list(getattr(config, "semgrep_config", None) or []) if config else []
        ),
        "sca": ScaScanner(),
    }


__all__ = [
    "ALL_TOOLS", "ScannerAdapter", "ToolRun", "ToolStatus", "registry",
    "BuiltinScanner", "GitleaksScanner", "ScaScanner", "SemgrepScanner",
]
