"""1-tap feedback on the agent's work: 👍 / 👎 on one run, with an optional reason.

The quality signal the autopilot already records is all *machine* signal — retries, PR
revisions, auto-review findings, test failures. None of it says whether the person who
had to read the PR thought the work was any good, which is the only judgement that
decides whether the autopilot is saving anyone time. A link on the completion notice
makes that judgement cost one tap instead of a meeting.

A vote is stored three ways, each for a different reader:

* a ``QualityEvent`` (``human_feedback``, value ±1) — the number Analytics aggregates and
  the task room's story shows;
* an ``AuditEvent`` — who-did-what, alongside every other dashboard action;
* for a 👎 that says WHY, a learned lesson — so the next brief for that repo carries the
  reviewer's complaint instead of repeating the mistake.

No per-user identity exists (the dashboard has one shared password), so the rule is one
vote per run and the latest replaces the earlier — see ``QualityRepository.replace_feedback``.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ai_autopilot import lessons
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    FLASH_MESSAGES,
    _flash,
    _take_flash,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.run_timeline import HUMAN_FEEDBACK, feedback_execution_id
from ai_autopilot.services.pr_feedback import parse_pr_url

_log = get_logger("dashboard.feedback")

#: Reason chips offered for a 👎. "Khác" alone carries no lesson — it says something was
#: wrong without saying what, and a brief line reading "Khác" would teach nothing.
REASONS = (
    "Sai yêu cầu",
    "Code chất lượng kém",
    "Thiếu test",
    "Đụng file không liên quan",
    "Khác",
)
_OTHER = "Khác"

FLASH_MESSAGES.update({
    "feedback_up": ("green", "👍 Đã ghi nhận — cảm ơn bạn đã đánh giá."),
    "feedback_down": ("green", "👎 Đã ghi nhận. Lý do (nếu có) được nạp thành bài học cho "
                               "các lần chạy sau."),
})


def _vote(raw: str | None) -> int:
    v = (raw or "").strip().lower()
    return 1 if v in ("up", "+1", "1", "good") else (-1 if v in ("down", "-1", "bad") else 0)


def compose_reason(chips: list[str], text: str) -> str:
    """Chips plus the free text, as one readable line. Unknown chips are dropped — the
    form is the only legitimate source, and the reason ends up in a brief."""
    picked = [c for c in REASONS if c in set(chips)]
    text = " ".join((text or "").split())[:600]
    head = ", ".join(picked)
    if head and text:
        return f"{head} — {text}"
    return head or text


def lesson_text(reason: str, work_item_id: int) -> str:
    """The line a 👎 adds to the repo's lessons — empty when there is nothing to learn."""
    meaningful = reason.replace(_OTHER, "").strip(" ,—-")
    if not meaningful:
        return ""
    return f"Người review: {reason} (#{work_item_id})"


def _repo_of(run) -> str:
    parsed = parse_pr_url(getattr(run, "pr_url", None) or "")
    return parsed[0] if parsed else ""


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/feedback/item/{work_item_id}")
    async def feedback_for_item(request: Request, work_item_id: int, v: str = "", at: str = ""):
        """Entry point from a completion notice.

        The notice knows the work item, not the run row, so it links here with its own
        timestamp; the run that finished closest to it is the one the reader saw.
        """
        c: Container = request.app.state.container
        near = None
        with contextlib.suppress(ValueError, OverflowError, OSError):
            near = datetime.fromtimestamp(int(at), UTC) if at else None
        run = await c.execution_repo.latest_finished_for_item(work_item_id, near=near)
        if run is None:
            return PlainTextResponse(
                f"Không tìm thấy run đã hoàn tất nào của #{work_item_id}.", status_code=404
            )
        vote = "up" if _vote(v) > 0 else ("down" if _vote(v) < 0 else "")
        return RedirectResponse(
            f"/dashboard/feedback/{run.id}" + (f"?v={vote}" if vote else ""), status_code=303
        )

    @router.get("/feedback/{execution_id}", response_class=HTMLResponse)
    async def feedback_page(request: Request, execution_id: int, v: str = ""):
        c: Container = request.app.state.container
        run = await c.execution_repo.get_by_id(execution_id)
        if run is None:
            return PlainTextResponse("Không tìm thấy run này.", status_code=404)
        current = await _current_vote(c, run)
        wanted = _vote(v) or (current.value if current else 0)
        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request, "feedback.html",
            _ctx(request, "task", run=run, vote=wanted, current=current,
                 reasons=REASONS, flash=flash,
                 status=str(getattr(run.status, "value", run.status) or "")),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/feedback/{execution_id}")
    async def feedback_submit(request: Request, execution_id: int):
        c: Container = request.app.state.container
        run = await c.execution_repo.get_by_id(execution_id)
        if run is None:
            return PlainTextResponse("Không tìm thấy run này.", status_code=404)
        form = await request.form()
        vote = _vote(str(form.get("v") or ""))
        if not vote:
            return RedirectResponse(f"/dashboard/feedback/{execution_id}", status_code=303)
        reason = compose_reason([str(x) for x in form.getlist("chip")],
                                str(form.get("reason") or ""))
        parsed = parse_pr_url(run.pr_url or "")
        await c.quality_events.replace_feedback(
            work_item_id=run.work_item_id, execution_id=run.id, value=vote,
            actor="dashboard", pr_id=parsed[1] if parsed else 0, detail=reason,
        )
        # Same convention as the rest of the audit log: the dashboard authenticates with
        # a shared password, so "who" is the surface, not a person.
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="run.feedback",
            target=f"#{run.work_item_id}",
            detail=f"run {run.id}: {'👍' if vote > 0 else '👎'}"
            + (f" — {reason}" if reason else ""),
        )
        if vote < 0:
            _learn(c, run, reason)
        return _flash(f"/dashboard/feedback/{execution_id}",
                      "feedback_up" if vote > 0 else "feedback_down")

    return router


async def _current_vote(c: Container, run):
    rows = await c.quality_events.recent(
        limit=50, kind=HUMAN_FEEDBACK, work_item_id=run.work_item_id)
    mine = [r for r in rows if feedback_execution_id(r) == run.id]
    return mine[0] if mine else None   # newest first


def _learn(c: Container, run, reason: str) -> None:
    """A 👎 with a reason becomes a lesson for the repo the PR was in.

    Filed under the shared bucket when the run's repo cannot be told (no PR): the
    shared bucket is read into every brief, so the complaint is not lost — merely
    broader than it should be, which is the better failure.
    """
    text = lesson_text(reason, run.work_item_id)
    workspace = getattr(c.config, "workspace_directory", "") or ""
    # Switching the learning loop off means "write nothing into the agent's memory" —
    # a reviewer's reason included. The vote itself is still recorded.
    if not text or not workspace or not getattr(c.config, "learning_loop_enabled", False):
        return
    repo = _repo_of(run) or lessons.SHARED_BUCKET
    try:
        lessons.record_lessons(workspace, repo, [text], now=datetime.now(UTC),
                               source=lessons.SOURCE_LEARNED)
    except Exception as exc:  # noqa: BLE001 — the vote is stored; the lesson is a bonus
        _log.warning("feedback lesson not recorded", run=run.id, error=describe_exc(exc))
