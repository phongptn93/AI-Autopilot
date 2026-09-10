"""History rows for PR-level runs (auto-review, PR comment commands).

The poller records every work-item run it starts, but the two services that act on
PRs — the reviewer tracker and the babysitter — recorded nothing: an auto-review or a
``/review`` was a full model run that left only a log line behind, so History could not
say what the bot had reviewed, when, for how long, or at what cost. These two calls
bracket such a run: the row goes in RUNNING (which is also how the dashboard shows a
review that is happening right now) and is completed with the run's result.
"""

from __future__ import annotations

import contextlib
from dataclasses import replace

from ai_autopilot.config import Settings
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo

_log = get_logger("services.run_history")


async def open_run(container, config: Settings, item: WorkItemInfo, skill: str) -> int | None:
    """Open a History row for a PR-level run; ``None`` when it couldn't be opened.

    ``project`` is filled in because History is filtered by the workspace's projects,
    and a PR's work item is often synthetic (the PR id standing in when the branch
    encodes no item) — a blank project would file the run where no workspace sees it.
    """
    try:
        project = item.project or config.code_project or config.ado_project
        return await container.execution_repo.start_execution(
            replace(item, project=project), skill
        )
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never sink the run
        _log.warning("could not open history record", skill=skill, error=describe_exc(exc))
        return None


async def close_run(container, record_id: int | None, result: ExecutionResult) -> None:
    if record_id is None:
        return
    with contextlib.suppress(Exception):
        await container.execution_repo.complete_execution(record_id, result)
