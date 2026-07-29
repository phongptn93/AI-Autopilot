"""Execution outcome model (ported from ``ExecutionResult``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class ExecutionResult:
    """Result of running Claude against a single work item."""

    work_item_id: int
    success: bool
    skill_used: str = ""
    output: str = ""
    error: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None                       # primary PR (first) — back-compat
    pr_urls: list[str] = field(default_factory=list)  # every PR the task opened
    files_changed: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    cost_tokens: int = 0
    cost_usd: float | None = None
    # The agent ran but reports it needs a human (ambiguous AC, missing info).
    # Not a failure to retry — escalate and stop.
    needs_human: bool = False
    # Auto-test-gate outcome: True/False when the suite ran, None when skipped
    # (gate off / no runner). Feeds pr_scorer's ci signal.
    tests_passed: bool | None = None
    completed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def ok(cls, work_item_id: int, skill: str, output: str) -> ExecutionResult:
        return cls(work_item_id=work_item_id, success=True, skill_used=skill, output=output)

    @classmethod
    def fail(cls, work_item_id: int, skill: str, error: str) -> ExecutionResult:
        return cls(work_item_id=work_item_id, success=False, skill_used=skill, error=error)
