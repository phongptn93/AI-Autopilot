"""Security scanning: SAST + SCA + secrets, with a lifecycle for what they find.

The report loop (``reports.py``) already knows how to run a read-only agent and keep
what it says. What it did not have is what makes a security tool usable day after
day: the same finding recognised across runs (``fingerprint``), a baseline so only the
NEW ones raise an alarm, suppressions with a reason and an expiry, deterministic
scanners next to the model so a committed secret is caught by a regex and not by
whether the model happened to look, and an exit code a pipeline can gate on.

Layout::

    tools/        one adapter per scanner (builtin, semgrep, gitleaks, sca) → Finding
    fingerprint   stable identity + baseline diff
    suppressions  the .autopilot/security-suppressions.yaml contract
    ai_sast       the model-driven pass, fed the scanners' results to triage
    runner        orchestrates all of the above into one ScanResult
    sarif         SARIF 2.1.0 export for ADO Advanced Security / GitHub code scanning
    cli           ``ai-autopilot scan``
"""

from __future__ import annotations

from ai_autopilot.security_scan.runner import ScanRequest, ScanResult, run_scan

__all__ = ["ScanRequest", "ScanResult", "run_scan"]
