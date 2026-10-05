"""PR merge conflicts and the PR review queue."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import pr_conflicts as pr_conflicts_mod
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import (
    _BACKGROUND_RUNS,
    _FLASH_COOKIE,
    _REVIEWS,
    _TEMPLATES,
    _filter_reviews,
    _flash,
    _gather_scan,
    _log,
    _pr_age,
    _pr_status,
    _take_flash,
    work_item_link_base,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.logging_config import describe_exc
from ai_autopilot.services.pr_feedback import parse_work_item_id


async def _scan_reviews(c: Container) -> list[dict]:
    """Every active PR in scope with its reviewers and votes — ADO joined with the
    tracker's memory. Raises on an ADO failure; see :class:`_ScanCache`.

    This used to run INSIDE the request handler, nose-to-tail: the repo list, then
    each repo's active PRs, then one MORE request per PR for its work-item link.
    And whatever it had managed to collect when a request failed was then published
    as though the scan had succeeded — so a throttled ADO showed a reviewer a SHORT
    list of PRs, silently, for the whole TTL. A list that is quietly missing the PR
    waiting on you is worse than a page that admits it could not load.
    """
    from ai_autopilot.services.reviewer_tracker import VOTE_LABELS

    cfg = c.config
    org = cfg.ado_organization.rstrip("/")
    project = quote(cfg.code_project or cfg.ado_project, safe="")
    tracked: dict = {}
    try:
        for snap in await c.pr_reviewer_repo.all_reviewers():
            tracked[(snap.pr_id, snap.reviewer_id)] = snap
    except Exception as exc:  # noqa: BLE001 — the DB half may fail on its own
        _log.warning("reviewer state load failed", error=describe_exc(exc))

    repos = [(r.get("id"), r.get("name") or "")
             for r in await c.ado.get_repositories() if r.get("id")]
    per_repo = await _gather_scan(c.ado.get_active_pull_requests(rid) for rid, _ in repos)

    # Flatten first, THEN ask for every work-item link at once. One request per PR
    # is unavoidable (ADO has no bulk endpoint for it) but paying for them one after
    # another is not.
    flat: list[tuple[str, str, dict]] = [
        (rid, rname, pr)
        for (rid, rname), prs in zip(repos, per_repo, strict=True)
        for pr in prs
        if cfg.target_in_scope(pr.get("targetRefName", ""))
    ]
    links = await _gather_scan(
        c.ado.get_pull_request_work_items(rid, pr.get("pullRequestId") or 0)
        for rid, _, pr in flat
    )

    out: list[dict] = []
    for (_rid, rname, pr), linked_ids in zip(flat, links, strict=True):
        pr_id = pr.get("pullRequestId")
        # ADO's link first, the branch name second — the order the state sync and
        # the PR babysitter use. Showing only what the branch name spelled left the
        # work-item column blank on every PR named without an id, and wrong on any
        # branch whose name merely opens with a number.
        _linked = linked_ids[0] if linked_ids else None
        reviewers = []
        bot_reviewed = False
        for r in pr.get("reviewers") or []:
            if not r.get("id") or r.get("isContainer"):
                continue
            snap = tracked.get((pr_id, str(r["id"])))
            vote = int(r.get("vote") or 0)
            is_bot = bool(snap.is_bot) if snap else False
            if is_bot and snap and snap.reviewed_commit:
                bot_reviewed = True
            reviewers.append({
                "name": r.get("displayName") or r.get("uniqueName") or "?",
                "vote": vote,
                "vote_label": VOTE_LABELS.get(vote, str(vote)),
                "is_bot": is_bot,
                "required": bool(r.get("isRequired")),
                "added_at": snap.added_at if snap else None,
                "reminded": bool(snap.reminded_at) if snap else False,
            })
        approved = sum(1 for r in reviewers if r["vote"] >= 5)
        blocked = sum(1 for r in reviewers if r["vote"] < 0)
        pending = sum(1 for r in reviewers if r["vote"] == 0)
        conflicts = pr_conflicts_mod.is_conflicted(pr)
        out.append({
            "id": pr_id,
            "title": pr.get("title") or "",
            "repo": rname,
            "target": (pr.get("targetRefName") or "").removeprefix("refs/heads/"),
            "source": (pr.get("sourceRefName") or "").removeprefix("refs/heads/"),
            "author": (pr.get("createdBy") or {}).get("displayName") or "",
            "is_draft": bool(pr.get("isDraft")),
            "created": pr.get("creationDate") or "",
            "age": _pr_age(pr.get("creationDate")),
            "conflicts": conflicts,
            "work_item": _linked or parse_work_item_id(pr.get("sourceRefName", "")),
            "url": f"{org}/{project}/_git/{quote(rname, safe='')}/pullrequest/{pr_id}",
            "reviewers": reviewers,
            "approved": approved,
            "pending": pending,
            "blocked": blocked,
            "bot_reviewed": bot_reviewed,
            "status": _pr_status(pr, approved, blocked, pending, conflicts),
        })
    return out


def _render_reviews(request: Request, prs: list[dict], cfg, *,
                    pending: bool = False, failed: bool = False) -> HTMLResponse:
    """Filter the scanned PRs for this request, then group and summarise them.

    ``pending`` / ``failed`` travel to the template because an EMPTY board has three
    different meanings — nothing is awaiting review, we have not scanned yet, or ADO
    would not answer — and rendering all three as "0 PRs" told a reviewer their queue
    was clear when it was not.
    """
    qp = request.query_params
    me = cfg.effective_command_users
    shown = _filter_reviews(prs, qp, me)

    groups: dict[str, list[dict]] = {}
    for pr in shown:
        groups.setdefault(pr["target"] or "(unknown)", []).append(pr)
    # Group by target (merge-into) branch — most PRs first, target name A→Z.
    grouped = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    summary = {
        "total": len(shown),
        "approved": sum(1 for p in shown if p["status"] == "approved"),
        "awaiting": sum(1 for p in shown if p["status"] == "awaiting"),
        "blocked": sum(1 for p in shown if p["status"] in ("blocked", "conflicts")),
        "drafts": sum(1 for p in shown if p["is_draft"]),
    }
    # Options come from EVERY scanned PR, not the filtered set: a dropdown that
    # loses its other choices the moment you pick one is a dead end.
    facets = {
        key: sorted({p[key] for p in prs if p[key]})
        for key in ("repo", "author", "target")
    }
    mine_n = len(_filter_reviews(prs, {**dict(qp), "mine": "1"}, me)) if me else 0
    return _TEMPLATES.TemplateResponse(
        request,
        "reviews.html",
        _ctx(
            request, "reviews", grouped=grouped, summary=summary,
            scanned=len(prs), facets=facets, mine_count=mine_n,
            scan_pending=pending, scan_failed=failed,
            me=", ".join(me),
            f={
                "q": (qp.get("q") or "").strip(),
                "status": (qp.get("status") or "all").strip(),
                "repo": (qp.get("repo") or "all").strip(),
                "author": (qp.get("author") or "all").strip(),
                "target": (qp.get("target") or "all").strip(),
                "mine": (qp.get("mine") or "").strip() in {"1", "true", "yes"},
            },
            targets=cfg.reviewer_target_branches,
            tracking_enabled=cfg.pr_reviewer_tracking_enabled,
            auto_review=cfg.pr_auto_review_on_added,
            reminder_hours=cfg.pr_reviewer_reminder_hours,
        ),
    )


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    # ── PR merge conflicts ───────────────────────────────────────────────────
    @router.get("/conflicts", response_class=HTMLResponse)
    async def conflicts_page(request: Request, status: str = "active"):
        """Every PR that could not merge because of conflicts — open ones first, with
        since-when, files, what was tried and why it stopped; history below."""
        c: Container = request.app.state.container
        cfg = c.config
        status = status if status in ("active", "all", *pr_conflicts_mod.STATUSES) else "active"
        rows = await c.pr_conflict_repo.recent(limit=300, status="" if status == "all" else status)
        every = await c.pr_conflict_repo.recent(limit=2000)
        counts = {s: sum(1 for r in every if r.status == s) for s in pr_conflicts_mod.STATUSES}
        now = datetime.now(UTC).replace(tzinfo=None)
        view = []
        for r in rows:
            since = r.first_seen.replace(tzinfo=None) if r.first_seen else None
            end = (r.resolved_at.replace(tzinfo=None) if r.resolved_at else now)
            view.append({
                "row": r, "files": json.loads(r.files_json or "[]"),
                "checks": json.loads(r.checks_json or "{}"),
                "age_hours": ((end - since).total_seconds() / 3600) if since else 0.0,
            })
        svc = getattr(request.app.state, "pr_conflicts", None)
        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request, "conflicts.html",
            _ctx(request, "conflicts", items=view, status=status, counts=counts, flash=flash,
                 tracking=cfg.pr_conflict_tracking_enabled, service_live=svc is not None,
                 autoresolve=cfg.pr_conflict_autoresolve,
                 interactive=(cfg.execution_mode or "").lower() == "interactive",
                 session_hours=cfg.pr_session_hours,
                 command=cfg.pr_conflict_command, max_files=cfg.pr_conflict_max_files,
                 allow_red_target=cfg.pr_conflict_allow_preexisting_failures,
                 item_link=work_item_link_base(cfg),
                 active_statuses=pr_conflicts_mod.ACTIVE_STATUSES),
        )
        if flash:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/conflicts/{conflict_id}/resolve")
    async def conflicts_resolve(request: Request, conflict_id: int):
        """▶ Resolve — a person asking, so it applies to any PR (not only the bot's)."""
        c: Container = request.app.state.container
        svc = getattr(request.app.state, "pr_conflicts", None)
        row = await c.pr_conflict_repo.get(conflict_id)
        if row is None:
            return _flash("/dashboard/conflicts", "err_conflict_missing")
        if svc is None:
            return _flash("/dashboard/conflicts", "err_conflict_service")
        if (row.status not in pr_conflicts_mod.ACTIVE_STATUSES
                or row.status in pr_conflicts_mod.BUSY_STATUSES):
            return _flash("/dashboard/conflicts", "conflict_not_resolvable")
        task = asyncio.create_task(svc.resolve(conflict_id, requested_by="dashboard"))
        _BACKGROUND_RUNS.add(task)
        task.add_done_callback(_BACKGROUND_RUNS.discard)
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="pr.conflict_resolve_requested",
            target=f"PR !{row.pr_id}",
        )
        return _flash("/dashboard/conflicts", "conflict_resolve_started")

    @router.post("/conflicts/{conflict_id}/cancel")
    async def conflicts_cancel(request: Request, conflict_id: int):
        """✕ Close session — the branch is left exactly as it was."""
        c: Container = request.app.state.container
        svc = getattr(request.app.state, "pr_conflicts", None)
        if svc is None:
            return _flash("/dashboard/conflicts", "err_conflict_service")
        if not await svc.cancel(conflict_id):
            return _flash("/dashboard/conflicts", "conflict_not_in_session")
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="pr.conflict_session_closed",
            target=f"conflict {conflict_id}",
        )
        return _flash("/dashboard/conflicts", "conflict_session_closed")

    @router.post("/conflicts/scan")
    async def conflicts_scan(request: Request):
        """Scan now instead of waiting for the next cycle."""
        svc = getattr(request.app.state, "pr_conflicts", None)
        if svc is None:
            return _flash("/dashboard/conflicts", "err_conflict_service")
        task = asyncio.create_task(svc.scan())
        _BACKGROUND_RUNS.add(task)
        task.add_done_callback(_BACKGROUND_RUNS.discard)
        return _flash("/dashboard/conflicts", "conflict_scan_started")

    @router.get("/reviews", response_class=HTMLResponse)
    async def reviews(request: Request):
        """PR reviewer tracking: every active PR with its reviewers, votes, and
        reminder status — live ADO data joined with the tracker's memory.

        The scan is cached and refreshed BEHIND the page, like the Overview's figures:
        the filters are a question about data we already hold, and re-asking ADO to
        answer a dropdown change made the page unusable.
        """
        c: Container = request.app.state.container
        prs, pending = _REVIEWS.background(lambda: _scan_reviews(c))
        return _render_reviews(request, prs or [], c.config,
                               pending=pending, failed=_REVIEWS.failed and prs is None)

    return router
