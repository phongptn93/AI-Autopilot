"""The single funnel for durable quality signal: measure it AND learn from it.

Every rework / review signal the autopilot produces goes through :class:`QualityLog`.
It writes two things from one call:

* an append-only row in ``quality_events`` — the record that survives for analysis,
  because every counter it draws from is a *budget* that gets cleared exactly when
  the number finally means something (see :class:`~ai_autopilot.data.QualityEvent`);
* a **lesson**, when the signal carries a teachable reason, so the next brief on this
  workspace is warned.

Both from one place on purpose. The learning loop previously read lessons on every
execution path but only *wrote* them on one — the legacy path that had already been
replaced — so it drew from a well nothing filled and could never improve. Recording
and learning being the same call makes that class of gap unrepresentable: a new
signal cannot be measured without also being learned from.

Which signals teach, and which only count:

============================  =========================================================
Signal                        Lesson
============================  =========================================================
``REVIEW_FINDING``            the findings themselves — already actionable text
``TEST_FAILED``               the failing-test summary
``REVIEW_VOTE`` (negative)    that a human rejected the work, and what they voted
``REOPENED``                  that a human sent the item back after it looked done
``EXECUTION_RETRY``           nothing — an infra flake or timeout teaches no rule
``PR_REVISION``               nothing on its own; the review comment is the lesson
============================  =========================================================
"""

from __future__ import annotations

import contextlib
from datetime import datetime

from ai_autopilot import lessons
from ai_autopilot.config import Settings
from ai_autopilot.data import QualityKind, QualityRepository
from ai_autopilot.logging_config import get_logger

#: Vote at or below this means the reviewer is blocking (ADO: -5 waiting, -10 rejected).
_BLOCKING_VOTE = -5


def lesson_text(kind: str, detail: str, actor: str) -> str:
    """The lesson a signal should teach, or '' when it teaches nothing.

    Kept a pure function so the mapping is testable without a database or filesystem.
    """
    detail = (detail or "").strip()
    if kind == QualityKind.REVIEW_FINDING:
        return detail  # the findings text is already the lesson
    if kind == QualityKind.TEST_FAILED:
        return f"Tests failed on a previous run — check this before opening a PR: {detail}"
    if kind == QualityKind.REVIEW_VOTE:
        return (
            f"A human reviewer ({actor or 'unknown'}) blocked a previous PR "
            f"[{detail}]. Re-read the review comments on that PR before repeating "
            f"the same approach."
        )
    if kind == QualityKind.REOPENED:
        return (
            "A work item that looked finished was reopened by a human — the result "
            f"did not meet expectations. Context: {detail}"
        )
    return ""


class QualityLog:
    """Records quality signal durably and turns the teachable part into lessons.

    ``repos_provider`` yields the repo names a lesson should be filed under; signals
    that cannot be attributed to a repo land in :data:`lessons.SHARED_BUCKET`, which
    :func:`lessons.recent` always reads. Injected rather than imported so this module
    stays independent of workspace discovery.
    """

    def __init__(
        self,
        repo: QualityRepository,
        config: Settings,
        repos_provider=None,
    ) -> None:
        self._repo = repo
        self._config = config
        self._repos_provider = repos_provider
        self._log = get_logger("learning")

    async def record(
        self, *, work_item_id: int, kind: str, value: int = 0, stage: str = "",
        actor: str = "", pr_id: int = 0, detail: str = "",
    ) -> None:
        """Persist one signal, then learn from it. Never raises into the caller."""
        await self._repo.record(
            work_item_id=work_item_id, kind=kind, value=value, stage=stage,
            actor=actor, pr_id=pr_id, detail=detail,
        )
        self._learn(kind=kind, value=value, actor=actor, detail=detail)

    # ── learning ────────────────────────────────────────────────────────────────

    def _learn(self, *, kind: str, value: int, actor: str, detail: str) -> None:
        if not self._config.learning_loop_enabled:
            return
        workspace = self._config.workspace_directory
        if not workspace:
            # Without a workspace, `lessons.recent` reads nothing back, so writing would
            # only produce files no brief will ever carry. Say so once per signal rather
            # than failing silently — the silent version is what made this loop look
            # enabled while doing nothing.
            self._log.warning(
                "learning enabled but workspace_directory is empty — lesson dropped",
                kind=kind,
            )
            return
        if kind == QualityKind.REVIEW_VOTE and value > _BLOCKING_VOTE:
            return  # an approval teaches nothing
        text = lesson_text(kind, detail, actor)
        if not text:
            return
        for repo in self._lesson_buckets():
            with contextlib.suppress(Exception):  # learning must never break a run
                lessons.record_lessons(workspace, repo, [text], now=datetime.now())

    def _lesson_buckets(self) -> list[str]:
        """Where to file a lesson. The shared bucket always, because a work-item-level
        signal (a rejection, a reopen) belongs to no single repo."""
        if self._repos_provider is None:
            return [lessons.SHARED_BUCKET]
        try:
            repos = list(self._repos_provider() or [])
        except Exception:  # noqa: BLE001 — discovery failure must not lose the lesson
            repos = []
        # One repo in scope → file it there too, so a repo-specific brief carries it
        # even if the shared bucket is later pruned. More than one and we cannot say
        # which it belongs to, so the shared bucket alone is the honest answer.
        return [lessons.SHARED_BUCKET, *(repos if len(repos) == 1 else [])]
