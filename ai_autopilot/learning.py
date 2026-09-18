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
``PR_REVISION``               **what the reviewer actually asked for**, close to verbatim
``REOPENED``                  that a human sent the item back after it looked done
``EXECUTION_RETRY``           nothing — an infra flake or timeout teaches no rule
``REVIEW_VOTE``               nothing — the vote is a score; the comment is the lesson
============================  =========================================================

``PR_REVISION`` used to teach nothing and ``REVIEW_VOTE`` used to teach a pointer
("re-read the review comments on that PR"), which pointed at a PR that is closed by
the time any future brief reads it. Between them they threw away the best signal the
system produces — a person, looking at real code, saying what is wrong with it — and
filled the injected set with lines saying only that somebody had once been unhappy.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime

from ai_autopilot import lessons
from ai_autopilot.config import Settings
from ai_autopilot.data import QualityKind, QualityRepository
from ai_autopilot.logging_config import get_logger

#: Vote at or below this means the reviewer is blocking (ADO: -5 waiting, -10 rejected).
_BLOCKING_VOTE = -5

#: A leading ``@bot`` and/or ``/command`` — addressing, not content.
_ADDRESSING = re.compile(r"^(?:\s*@[\w.\-]+)*\s*(?:/[\w-]+)?\s*", re.UNICODE)
#: HTML a comment may arrive wrapped in.
_TAGS = re.compile(r"<[^>]+>")
#: Below this many characters of actual content, a comment is an instruction for THIS
#: pull request ("fix it", "sửa lại giúp", "ok chưa") and not a rule worth carrying to
#: the next one. A blunt length gate on purpose: the alternative is an LLM call per
#: comment, which is a bigger decision than this change should make on its own.
_MIN_TEACHABLE = 25


def teachable_ask(text: str) -> str:
    """The content of a reviewer's ask, or '' when there is no lesson in it.

    Strips the addressing (``@bot``, ``/ai``) and any HTML, then keeps it only if what
    remains says something. "fix it" is a command about one PR; "dùng ILogger thay cho
    Console.WriteLine" is a rule about every PR, and only the second is worth telling
    the next run.
    """
    body = _TAGS.sub(" ", text or "")
    body = _ADDRESSING.sub("", body, count=1)
    body = " ".join(body.split())
    return body if len(body) >= _MIN_TEACHABLE else ""


def lesson_text(kind: str, detail: str, actor: str) -> str:
    """The lesson a signal should teach, or '' when it teaches nothing.

    Kept a pure function so the mapping is testable without a database or filesystem.
    """
    detail = (detail or "").strip()
    if kind == QualityKind.REVIEW_FINDING:
        return detail  # the findings text is already the lesson
    if kind == QualityKind.TEST_FAILED:
        return f"Tests failed on a previous run — check this before opening a PR: {detail}"
    if kind == QualityKind.PR_REVISION:
        # The highest-quality signal in the system: a real person, looking at real
        # code, saying what is wrong with it — and it used to be thrown away. The
        # revision counter kept the NUMBER of rounds and dropped the sentence.
        #
        # Stored close to verbatim rather than paraphrased. A reviewer's own words
        # carry the specifics ("dùng ILogger, đừng Console.WriteLine") that a
        # generated summary sands off, and specifics are the whole value.
        ask = teachable_ask(detail)
        if not ask:
            return ""       # "fix it" is about one PR, not about the next one
        who = f" ({actor})" if actor and actor != "human" else ""
        return f"Reviewer{who} đã yêu cầu sửa: «{ask}». Kiểm tra điểm này TRƯỚC khi mở PR."
    if kind == QualityKind.REVIEW_VOTE:
        # Deliberately teaches nothing. It used to produce "…blocked a previous PR.
        # Re-read the review comments on that PR" — a pointer to a PR that is closed
        # by the time any future brief reads it, so no run could ever follow it. Two
        # of those sat permanently in the injected set saying only "somebody was once
        # unhappy". What the reviewer actually SAID now arrives via PR_REVISION.
        return ""
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
