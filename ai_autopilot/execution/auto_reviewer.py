"""Automated pre-PR review using Claude (ported from ``AutoReviewer``) — with a
deterministic scanner gate in front of it.

The model is asked to look for security issues, and mostly does. But "mostly" is the
wrong guarantee for a committed secret: whether the PR that adds ``AccountKey=…`` to
``appsettings.json`` is blocked must not depend on whether the model happened to open
that file. So the branch diff first goes through the security scanners that are
installed (``security_scan.pr_gate_tools`` — the builtin rules always, gitleaks when
present), and any finding at or above ``block_on_severity`` blocks the PR **before**
the model is consulted. The model then sees those findings, so it can spend its
attention on what a regex cannot see rather than re-reporting what one already did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ai_autopilot.config import Settings
from ai_autopilot.execution.claude_client import ClaudeRun, run_claude
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.reports import SEVERITIES, Finding


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
    # What the deterministic scanners found on the diff, whatever their severity —
    # kept whole so the caller can store them with a lifecycle (Security page) rather
    # than only as strings in a comment.
    scanner_findings: list[Finding] = field(default_factory=list)
    scanner_status: dict[str, str] = field(default_factory=dict)


class AutoReviewer:
    def __init__(self, config: Settings) -> None:
        self._config = config
        self._log = get_logger("execution.auto_reviewer")

    async def review(self, work_dir: str, base_branch: str = "") -> ReviewResult:
        if not self._config.auto_review_enabled:
            return ReviewResult(passed=True)

        blocked = {
            s.strip().upper()
            for s in self._config.block_on_severity.split(",")
            if s.strip()
        }
        self._log.info("running auto-review", dir=work_dir)

        result = ReviewResult()
        base = base_branch or self._config.base_branch
        seeded = await self._scanner_gate(work_dir, base, result, blocked)

        output, result.run = await self._run_review(work_dir, seeded)
        result.raw_output = output + "\n"
        _parse_issues(output, result, blocked)

        self._log.info(
            "auto-review done",
            critical=len(result.critical_issues),
            warnings=len(result.warnings),
            scanners=result.scanner_status or None,
        )
        result.passed = len(result.critical_issues) == 0
        return result

    async def _scanner_gate(
        self, work_dir: str, base_branch: str, result: ReviewResult, blocked: set[str],
    ) -> list[Finding]:
        """Run the deterministic scanners on the diff; block on what they find.

        Never raises and never blocks on its own failure: a scanner that could not run
        is recorded in ``scanner_status`` and the model review proceeds — the gate adds
        certainty where it can, it does not take the review away where it cannot.
        """
        sec = getattr(self._config, "security_scan", None)
        if sec is None or not sec.enabled or not sec.pr_gate_tools:
            return []
        try:
            from ai_autopilot.security_scan.runner import ScanRequest, run_scan

            scan = await run_scan(ScanRequest(
                repo=work_dir, tools=list(sec.pr_gate_tools), ai_mode="off", scope="diff",
                base_branch=base_branch or "main", fail_on="info", trigger="pr-gate",
                store=False, write_html=False, max_findings_per_tool=sec.max_findings_per_tool,
                gitleaks_log_opts=f"origin/{base_branch}..HEAD" if base_branch else "",
                disabled_rules=list(sec.disabled_rules), ignore_paths=list(sec.ignore_paths),
            ))
        except Exception as exc:  # noqa: BLE001
            self._log.warning("pre-PR scanner gate failed", error=describe_exc(exc))
            result.scanner_status = {"gate": f"error: {describe_exc(exc)}"[:200]}
            return []
        result.scanner_findings = list(scan.findings)
        result.scanner_status = scan.tool_labels()
        for f in scan.findings:
            line = _finding_line(f)
            if f.severity.upper() in blocked:
                result.critical_issues.append(line)
            else:
                result.warnings.append(line)
        if any(f.severity.upper() in blocked for f in scan.findings):
            self._log.warning(
                "pre-PR scanner gate found blocking issues",
                count=sum(1 for f in scan.findings if f.severity.upper() in blocked),
            )
        return list(scan.findings)

    async def _run_review(
        self, work_dir: str, seeded: list[Finding] | None = None,
    ) -> tuple[str, ClaudeRun | None]:
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
        if seeded:
            listed = "\n".join(f"  - {_finding_line(f)}" for f in seeded[:40])
            prompt += (
                "\n\nAlready found by static scanners on this diff — do NOT report these "
                "again; look for what they cannot see (authorisation on the changed "
                "endpoints, ownership checks on ids, logic errors):\n" + listed
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
            self._log.warning("auto-review command failed", error=describe_exc(exc))
            return "", None


def _finding_line(f: Finding) -> str:
    """A scanner finding in the same ``[Severity] text`` shape the model's lines use, so
    the caller's comment / lessons / quality log treat both alike."""
    where = f"{f.file}:{f.line}" if f.line else f.file
    tag = f.severity.capitalize() if f.severity in SEVERITIES else "Medium"
    ident = f"{f.tool}/{f.rule_id}" if f.rule_id else f.tool
    return f"[{tag}] {f.title} — {where} ({ident})"


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
