"""Automated pre-PR review using Claude (ported from ``AutoReviewer``)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_autopilot.config import Settings
from ai_autopilot.execution.claude_client import run_claude
from ai_autopilot.logging_config import get_logger


@dataclass
class ReviewResult:
    passed: bool = False
    critical_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_output: str = ""


class AutoReviewer:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("execution.auto_reviewer")

    async def review(self, work_dir: str) -> ReviewResult:
        if not self._config.auto_review_enabled:
            return ReviewResult(passed=True)

        blocked = {
            s.strip().upper()
            for s in self._config.block_on_severity.split(",")
            if s.strip()
        }
        self._log.info("running auto-review", dir=work_dir)

        result = ReviewResult()
        output = await self._run_review(work_dir)
        result.raw_output = output + "\n"
        _parse_issues(output, result, blocked)

        self._log.info(
            "auto-review done",
            critical=len(result.critical_issues),
            warnings=len(result.warnings),
        )
        result.passed = len(result.critical_issues) == 0
        return result

    async def _run_review(self, work_dir: str) -> str:
        prompt = (
            "Review this branch for security issues. List each issue with severity: "
            "Critical, High, Medium, or Low."
        )
        try:
            run = await run_claude(
                prompt,
                work_dir,
                timeout_seconds=300,
                model=self._config.claude_model or None,
                permission_mode="plan",
            )
            return run.text
        except Exception as exc:  # noqa: BLE001
            self._log.warning("auto-review command failed", error=str(exc))
            return ""


def _parse_issues(output: str, result: ReviewResult, blocked: set[str]) -> None:
    for line in output.splitlines():
        upper = line.upper()
        if any(sev in upper for sev in blocked):
            result.critical_issues.append(line.strip())
        elif "MEDIUM" in upper or "LOW" in upper:
            result.warnings.append(line.strip())
