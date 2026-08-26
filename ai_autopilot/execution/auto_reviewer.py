"""Automated pre-PR review using Claude (ported from ``AutoReviewer``)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ai_autopilot.config import Settings
from ai_autopilot.execution.claude_client import ClaudeRun, run_claude
from ai_autopilot.logging_config import get_logger


@dataclass
class ReviewResult:
    passed: bool = False
    critical_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw_output: str = ""
    # The Claude call this review made, so the caller can bill its tokens to the work
    # item. The review gate is a full model run and was being counted as zero, which
    # understated the cost of every SDLC item by an entire stage.
    run: ClaudeRun | None = None


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
        output, result.run = await self._run_review(work_dir)
        result.raw_output = output + "\n"
        _parse_issues(output, result, blocked)

        self._log.info(
            "auto-review done",
            critical=len(result.critical_issues),
            warnings=len(result.warnings),
        )
        result.passed = len(result.critical_issues) == 0
        return result

    async def _run_review(self, work_dir: str) -> tuple[str, ClaudeRun | None]:
        prompt = (
            "Review the changes on this branch for security issues. Output EACH real "
            "finding on its own line, starting with a bracketed severity tag, exactly like:\n"
            "  - [Critical] <description>\n"
            "  - [High] <description>\n"
            "  - [Medium] <description>\n"
            "  - [Low] <description>\n"
            "Use the bracketed [severity] tag ONLY at the START of an actual finding line — "
            "never inside prose, summaries or negations. If there are no issues at all, "
            "output the single line: - [None] no issues found."
        )
        try:
            run = await run_claude(
                prompt,
                work_dir,
                timeout_seconds=300,
                model=self._config.claude_model or None,
                permission_mode="plan",
            )
            return run.text, run
        except Exception as exc:  # noqa: BLE001
            self._log.warning("auto-review command failed", error=str(exc))
            return "", None


# A finding line MUST lead with a bracketed severity tag (after an optional bullet), e.g.
# "- [High] …". Anchored at line start so prose like "no [Critical]/High issues" or "has no
# Critical/High vulnerabilities" is NOT mistaken for a finding — the false positive that
# used to block legitimate fixes.
_FINDING_RE = re.compile(r"^[\s\-*•>]*\[\s*(critical|high|medium|low)\s*\]", re.IGNORECASE)


def _parse_issues(output: str, result: ReviewResult, blocked: set[str]) -> None:
    for line in output.splitlines():
        m = _FINDING_RE.match(line)
        if not m:
            continue  # prose / summary / negation — not a finding
        if m.group(1).upper() in blocked:
            result.critical_issues.append(line.strip())  # blocks the PR
        else:
            result.warnings.append(line.strip())          # advisory only
