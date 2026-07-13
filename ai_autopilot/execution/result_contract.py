"""Structured contract between the control plane and the agent.

Instead of scraping Claude's free-form stdout, the agent (Claude + workspace
skills/agents) writes a structured outcome to
``<workspace>/.autopilot/runs/<item_id>.json``. The control plane reads that
file to learn what happened — which PRs were opened, whether the work needs a
human, etc. This module defines the schema and the read/clear helpers.

The schema (all fields optional; the parser is tolerant):

    {
      "status": "completed" | "failed" | "needs_human",
      "summary": "what was done",
      "artifacts": [{"repo": "Backend-Fresh", "branch": "...", "pr_url": "https://..."}],
      "needs_human": false,
      "reason": "why human input is needed / why it failed"
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Where the agent writes its result, relative to the workspace root.
RUNS_SUBDIR = Path(".autopilot") / "runs"


@dataclass
class Artifact:
    """One thing the agent produced — typically a branch + PR in one repo."""

    repo: str = ""
    branch: str = ""
    pr_url: str = ""


@dataclass
class AgentResult:
    status: str = "failed"  # "completed" | "failed" | "needs_human"
    summary: str = ""
    artifacts: list[Artifact] = field(default_factory=list)
    needs_human: bool = False
    reason: str = ""

    @property
    def pr_url(self) -> str | None:
        for a in self.artifacts:
            if a.pr_url:
                return a.pr_url
        return None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed" and not self.needs_human


def _result_path(workspace: str, item_id: int) -> Path:
    return Path(workspace) / RUNS_SUBDIR / f"{item_id}.json"


def clear_result(workspace: str, item_id: int) -> None:
    """Remove any stale result file before a run so we never read an old one."""
    try:
        _result_path(workspace, item_id).unlink()
    except (FileNotFoundError, OSError):
        pass


def read_result(workspace: str, item_id: int) -> AgentResult | None:
    """Read + validate the agent's result file. Returns None if missing/invalid."""
    try:
        data = json.loads(_result_path(workspace, item_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    artifacts: list[Artifact] = []
    for a in data.get("artifacts") or []:
        if isinstance(a, dict):
            artifacts.append(
                Artifact(
                    repo=str(a.get("repo", "")),
                    branch=str(a.get("branch", "")),
                    pr_url=str(a.get("pr_url", "")),
                )
            )

    status = str(data.get("status", "")).lower().strip()
    needs_human = bool(data.get("needs_human", False)) or status == "needs_human"
    return AgentResult(
        status=status or "failed",
        summary=str(data.get("summary", "")),
        artifacts=artifacts,
        needs_human=needs_human,
        reason=str(data.get("reason", "")),
    )
