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
      "artifacts": [{"repo": "Backend-Fresh", "branch": "...", "pr_url": "https://...",
                     "work_item_id": 4021}],
      "needs_human": false,
      "reason": "why human input is needed / why it failed"
    }

A BATCHED run (several linked items handled in one agent run) writes ONE file,
keyed ``batch-<lead id>.json``, whose artifacts carry ``work_item_id`` so each PR
can be reported back on its own work item.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Where the agent writes its result, relative to the workspace root.
RUNS_SUBDIR = Path(".autopilot") / "runs"


@dataclass
class Artifact:
    """One thing the agent produced — typically a branch + PR in one repo.

    ``work_item_id`` is what makes a BATCHED run reportable: several work items
    share one run, and each PR has to find its way back to the item it belongs to
    (state, tag, comment, retry budget are all per item). 0 = unattributed, which
    is the normal case for a single-item run.
    """

    repo: str = ""
    branch: str = ""
    pr_url: str = ""
    work_item_id: int = 0


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


def batch_key(lead_item_id: int) -> str:
    """Result key for a batched run — several items, one run, one result file."""
    return f"batch-{lead_item_id}"


def _result_path(workspace: str, item_id: int | str) -> Path:
    return Path(workspace) / RUNS_SUBDIR / f"{item_id}.json"


def _stray_paths(workspace: str, item_id: int | str) -> list[Path]:
    """Places the agent plausibly wrote the result INSTEAD of the workspace root.

    The brief tells it not to touch anything outside the repo subfolders, so a
    literal-minded run writes ``<repo>/.autopilot/runs/<id>.json``. Treat that as
    the same result rather than losing the whole run over its location.
    """
    try:
        return sorted(Path(workspace or ".").glob(f"*/{RUNS_SUBDIR.as_posix()}/{item_id}.json"))
    except OSError:
        return []


def clear_result(workspace: str, item_id: int | str) -> None:
    """Remove any stale result file before a run so we never read an old one.

    Clears the stray per-repo copies too — otherwise a leftover from an earlier
    run would be picked up by ``find_result`` as if it were this run's outcome.
    """
    for path in [_result_path(workspace, item_id), *_stray_paths(workspace, item_id)]:
        try:
            path.unlink()
        except (FileNotFoundError, OSError):
            pass


def _as_id(value: object) -> int:
    """Tolerant work-item id: the agent may write 4021, "4021" or "#4021"."""
    try:
        return int(str(value).strip().lstrip("#"))
    except (TypeError, ValueError):
        return 0


def _parse(data: object) -> AgentResult | None:
    """Build an ``AgentResult`` from already-decoded JSON. None if not our shape."""
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
                    work_item_id=_as_id(a.get("work_item_id")),
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


def read_result(workspace: str, item_id: int | str) -> AgentResult | None:
    """Read + validate the agent's result file. Returns None if missing/invalid."""
    try:
        data = json.loads(_result_path(workspace, item_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _parse(data)


def find_result(workspace: str, item_id: int | str) -> AgentResult | None:
    """``read_result``, then the per-repo locations the agent may have used instead.

    The canonical path always wins; a stray copy is only consulted when the
    workspace root has nothing, so a correct run is never overridden by a nested
    leftover.
    """
    result = read_result(workspace, item_id)
    if result is not None:
        return result
    for path in _stray_paths(workspace, item_id):
        try:
            parsed = _parse(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if parsed is not None:
            return parsed
    return None


# Only the tail of a long transcript is scanned for a result envelope — the JSON,
# when the agent prints it instead of writing it, is at the very end.
_TEXT_SCAN_LIMIT = 50_000


def parse_result_text(text: str) -> AgentResult | None:
    """Last-resort recovery: pull the result envelope out of the agent's OUTPUT.

    An agent that did the work but printed the JSON in its final message instead
    of writing the file has still told us everything we need. Reading it here
    turns "no result file" — which discards a completed run, PR and all — into a
    normally-processed outcome. Returns None when the output holds no envelope.
    """
    if not text:
        return None
    tail = text[-_TEXT_SCAN_LIMIT:]
    decoder = json.JSONDecoder()
    found: dict | None = None
    for idx, ch in enumerate(tail):
        if ch != "{":
            continue
        try:
            data, _ = decoder.raw_decode(tail[idx:])
        except ValueError:
            continue
        # "status"/"needs_human" are what make it OUR envelope rather than some
        # unrelated JSON the agent happened to quote (a config, an API payload).
        if isinstance(data, dict) and ("status" in data or "needs_human" in data):
            found = data  # keep scanning: the LAST envelope is the final word
    return _parse(found) if found is not None else None
