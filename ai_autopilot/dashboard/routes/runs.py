"""What ran and what is running: activity, history, now, queue, audit, quality."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ai_autopilot import activity
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _QUIET_STUCK_SECONDS,
    _QUIET_WARN_SECONDS,
    _TEMPLATES,
    _feed_key,
    _flash,
    _live_session_activity,
    _log,
    _take_flash,
    scope_of,
    work_item_link_base,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.data.entities import PipelineState, QualityKind
from ai_autopilot.execution import sdlc_plan
from ai_autopilot.logging_config import describe_exc
from ai_autopilot.services import planning_analyzer


async def _scope_rows(request: Request, c: Container, rows: list) -> list:
    """Narrow DB rows keyed only by ``work_item_id`` to the selected workspace.

    The pipeline tables never recorded a project, so the mapping comes from the
    state history. An item the history has never seen has an UNKNOWN project, and
    unknown is excluded from a scoped view rather than shown everywhere: leaking
    another workspace's item into this one is the error that misleads, while a
    missing row is visible the moment the operator switches back to "all"."""
    _, in_scope = scope_of(request, c.config)
    if in_scope is None or not rows:
        return rows
    allowed = {p.lower() for p in in_scope}
    try:
        projects = await c.state_history.known_projects([r.work_item_id for r in rows])
    except Exception as exc:  # noqa: BLE001 — never blank a page over a filter
        _log.warning("workspace scoping unavailable", error=describe_exc(exc))
        return rows
    return [r for r in rows if projects.get(r.work_item_id, "").lower() in allowed]


# ``item_id`` is a str, not an int: PR-level runs (auto-review, comment commands)
# are keyed "pr-<id>" so they can't collide with a work item of the same number —
# see ``activity.pr_key``.
def _feed_for(c: Container, item_id: str) -> str:
    """The live feed for an item, whichever mode is producing it.

    A headless run streams its events here through the SDK. An interactive one
    cannot — it is a separate console — so this page was permanently empty for the
    DEFAULT execution mode, and "Watch live" led to a page that never said anything.
    Its own transcript is read instead when the activity log has nothing.
    """
    feed = activity.read(c.config.workspace_directory, _feed_key(item_id))
    if feed.strip() or item_id.startswith("pr-"):
        return feed
    try:
        item_num = int(item_id)
    except ValueError:
        return feed
    for cwd in (c.executor.interactive_scratch_dir(item_num),
                c.config.workspace_directory):
        if not cwd:
            continue
        live = c.executor.interactive_feed(cwd)
        if live.strip():
            return live
    return feed


_HISTORY_STATUSES = ("Success", "Failed", "Running", "Retrying", "Pending")


_QUALITY_PER = (25, 50, 100)


def _pager(total: int, page: int, per: int) -> dict:
    """Page maths for one table: clamped page, the slice, and a short window of page
    numbers (1 ... 4 5 [6] 7 8 ... 20) so a long table never renders a wall of links."""
    pages = max(1, -(-total // per))
    page = min(max(1, page), pages)
    start = (page - 1) * per
    window = sorted({1, pages, *range(max(1, page - 2), min(pages, page + 2) + 1)})
    nums: list[int | None] = []
    for n in window:
        if nums and n - (nums[-1] or 0) > 1:
            nums.append(None)          # the gap, rendered as an ellipsis
        nums.append(n)
    return {"page": page, "pages": pages, "per": per, "total": total,
            "start": start, "end": min(total, start + per), "nums": nums}


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/activity/{item_id}", response_class=HTMLResponse)
    async def activity_view(request: Request, item_id: str):
        c: Container = request.app.state.container
        return _TEMPLATES.TemplateResponse(
            request, "activity.html",
            _ctx(request, "board", item_id=item_id, feed=_feed_for(c, item_id),
                 is_pr=item_id.startswith("pr-")),
        )

    @router.get("/activity/{item_id}/partial", response_class=PlainTextResponse)
    async def activity_partial(request: Request, item_id: str):
        c: Container = request.app.state.container
        return PlainTextResponse(
            _feed_for(c, item_id) or "(no activity yet — waiting for the agent…)"
        )

    @router.get("/history", response_class=HTMLResponse)
    async def history(request: Request):
        c: Container = request.app.state.container
        qp = request.query_params
        status = (qp.get("status") or "").strip()
        cat = (qp.get("cat") or "").strip()
        q = (qp.get("q") or "").strip()
        dfrom = (qp.get("from") or "").strip()
        dto = (qp.get("to") or "").strip()
        try:
            per = int(qp.get("per") or 25)
        except ValueError:
            per = 25
        per = per if per in _QUALITY_PER else 25
        try:
            page = max(1, int(qp.get("page") or 1))
        except ValueError:
            page = 1

        _, in_scope = scope_of(request, c.config)

        async def _run(pg: int, st: str | None = None, limit: int | None = None):
            return await c.execution_repo.search(
                status=(status if st is None else st) or None, category=cat or None,
                q=q or None, dfrom=dfrom or None, dto=dto or None, projects=in_scope,
                offset=(pg - 1) * per, limit=limit or per,
            )

        rows, total = await _run(page)
        pager = _pager(total, page, per)
        if pager["page"] != page:                       # out of range → clamp + re-query
            page = pager["page"]
            rows, total = await _run(page)
        # Counts per status under the OTHER filters, for the chips — "Failed 3" says
        # where to look before anyone opens the list.
        status_counts = {}
        for st in _HISTORY_STATUSES:
            _, n = await _run(1, st=st, limit=1)
            status_counts[st] = n
        all_count = sum(status_counts.values())

        params = {"status": status, "cat": cat, "q": q, "from": dfrom, "to": dto,
                  "per": per, "page": page}

        def url(**over) -> str:
            merged = {**params, **over}
            keep = {k: v for k, v in merged.items()
                    if v not in ("", None, 0) and not (k == "page" and v == 1)
                    and not (k == "per" and v == 25)}
            return "/dashboard/history" + (("?" + urlencode(keep)) if keep else "")

        ctx = _ctx(
            request, "history",
            records=rows, total=total, pager=pager, per=per, per_options=_QUALITY_PER,
            status=status, cat=cat, q=q, date_from=dfrom, date_to=dto, url=url,
            status_counts=status_counts, all_count=all_count,
            filtered=bool(q or status or cat or dfrom or dto),
        )
        return _TEMPLATES.TemplateResponse(request, "history.html", ctx)

    @router.post("/history/{record_id}/delete")
    async def delete_history(request: Request, record_id: int):
        c: Container = request.app.state.container
        await c.execution_repo.delete(record_id)
        _log.info("execution record deleted via dashboard", record_id=record_id)
        return RedirectResponse(url="/dashboard/history", status_code=303)

    @router.post("/history/clear")
    async def clear_history(request: Request):
        c: Container = request.app.state.container
        removed = await c.execution_repo.clear_all()
        _log.info("execution history cleared via dashboard", removed=removed)
        return RedirectResponse(url="/dashboard/history", status_code=303)

    @router.get("/now", response_class=HTMLResponse)
    async def now_page(request: Request):
        """What is running RIGHT NOW — the question the dashboard could not answer.

        History is a record of runs that finished and the Board is a record of where
        work stands; between "running claude" and its result, minutes later, there was
        nowhere to look. The operator's two real questions are "what is it working on"
        and "is it stuck", and the second is answered by how long the run has been
        QUIET, not by how long it has been going.
        """
        c: Container = request.app.state.container
        cfg = c.config
        rows, _ = await c.execution_repo.search(status="Running", limit=50)
        rows = await _scope_rows(request, c, rows)
        link_base = work_item_link_base(cfg)
        now = datetime.now(UTC)
        runs = []
        for r in rows:
            # A PR-level run files its feed under "pr-<id>"; a work-item run under the
            # bare id. Reading the wrong key would show an empty feed for half of them.
            key = (activity.pr_key(r.work_item_id)
                   if (r.skill_used or "").startswith("pr-")
                   else r.work_item_id)
            line, quiet = activity.last_event(cfg.workspace_directory, key)
            # An INTERACTIVE run writes no activity feed — it is a separate console, and
            # the feed is written by the SDK stream a headless run has. So this page,
            # built to answer "is it stuck", reported "no feed" for every run in the
            # DEFAULT execution mode. Claude Code's own transcript answers the same two
            # questions for those, from the session's own cwd.
            if quiet is None and (r.skill_used or "").startswith("interactive"):
                line, quiet = _live_session_activity(c, r.work_item_id)
            started = r.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            # WHICH ROLE is running. `skill_used` cannot say: in the default execution
            # mode it reads "interactive:<session id>", which names the console, not the
            # work — so a page built to answer "what is it doing" could not say whether
            # this was a QC check or the entire pipeline. The stages come from the role,
            # because "full" means nothing until you see the six steps it stands for.
            role = (r.profile or "").strip()
            stages = [s.name for s in sdlc_plan.profile_stages(role, cfg)] if role else []
            runs.append({
                "id": r.work_item_id,
                "title": r.title or f"#{r.work_item_id}",
                "skill": r.skill_used or "",
                "role": role,
                "stages": stages,
                "project": r.project or "",
                "elapsed": int((now - started).total_seconds()) if started else None,
                "quiet": int(quiet) if quiet is not None else None,
                "last": line,
                "feed_url": f"/dashboard/activity/{key}",
                "url": f"{link_base}/{r.work_item_id}" if link_base else "",
            })
        runs.sort(key=lambda r: r["quiet"] if r["quiet"] is not None else -1, reverse=True)
        _, in_scope = scope_of(request, cfg)

        # How long a run of this role USUALLY takes (median of recent successes), so
        # "running 14 min" can be read as "about done" or "twice as long as usual".
        typical: dict[str, int] = {}
        with contextlib.suppress(Exception):
            recent_ok, _ = await c.execution_repo.search(
                status="Success", projects=in_scope, limit=400,
                dfrom=(now - timedelta(days=30)).date().isoformat(),
            )
            by_role: dict[str, list[float]] = {}
            for r in recent_ok:
                if r.duration_seconds:
                    by_role.setdefault((r.profile or "").strip(), []).append(r.duration_seconds)
            for role, xs in by_role.items():
                xs.sort()
                typical[role] = int(xs[len(xs) // 2])
        for r in runs:
            t = typical.get(r["role"]) or typical.get("")
            r["typical"] = t
            r["progress"] = (min(100, int(r["elapsed"] / t * 100))
                             if t and r["elapsed"] is not None else None)
            r["overdue"] = bool(t and r["elapsed"] is not None and r["elapsed"] > 2 * t)
            q = r["quiet"]
            r["level"] = ("none" if q is None else "stuck" if q >= _QUIET_STUCK_SECONDS
                          else "warn" if q >= _QUIET_WARN_SECONDS else "ok")
            r["session"] = (f"autopilot-{r['id']}"
                            if (r["skill"] or "").startswith("interactive") else "")

        # Today, in numbers — spend, and how the day's runs ended.
        spend = None
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with contextlib.suppress(Exception):
            spend = await c.execution_repo.spend_since(midnight, projects=in_scope)
        today = {"Success": 0, "Failed": 0, "total": 0}
        with contextlib.suppress(Exception):
            day_rows, _ = await c.execution_repo.search(
                dfrom=midnight.date().isoformat(), projects=in_scope, limit=1000)
            for r in day_rows:
                today["total"] += 1
                if r.status.value in today:
                    today[r.status.value] += 1

        # Interactive PR sessions (conflicts and `/ai`) — work in flight that has no
        # execution row of its own, so without this they were invisible here.
        sessions: list[dict] = []
        with contextlib.suppress(Exception):
            for row in await c.pr_conflict_repo.in_session():
                st = row.session_started
                if st is not None and st.tzinfo is None:
                    st = st.replace(tzinfo=UTC)
                sessions.append({
                    "kind": "conflict", "pr": row.pr_id, "repo": row.repo_name,
                    "branch": row.source_branch, "title": row.title,
                    "session": row.session_name, "url": row.url,
                    "elapsed": int((now - st).total_seconds()) if st else None,
                    "cancel": f"/dashboard/conflicts/{row.id}/cancel",
                })
        with contextlib.suppress(Exception):
            for row in await c.pr_session_repo.open_sessions():
                st = row.started
                if st is not None and st.tzinfo is None:
                    st = st.replace(tzinfo=UTC)
                sessions.append({
                    "kind": "ai", "pr": row.pr_id, "repo": row.repo_name,
                    "branch": row.branch, "title": row.instruction[:140],
                    "session": row.session_name, "url": "",
                    "elapsed": int((now - st).total_seconds()) if st else None,
                    "cancel": f"/dashboard/sessions/{row.id}/cancel",
                })
        session_limit = int(getattr(cfg, "pr_session_hours", 8) or 8) * 3600

        # Up next and just finished: the page is useful when nothing is running too.
        queued: list[dict] = []
        with contextlib.suppress(Exception):
            for st in await c.state_repo.all():
                if st.state == PipelineState.QUEUED:
                    queued.append({"id": st.work_item_id, "title": st.title or "",
                                   "since": st.updated_at})
            queued.sort(key=lambda q: q["since"] or datetime.min)
        recent: list = []
        with contextlib.suppress(Exception):
            rec, _ = await c.execution_repo.search(projects=in_scope, limit=12)
            recent = [r for r in rec if r.status.value != "Running"][:6]

        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request, "now.html",
            _ctx(request, "now", runs=runs, spend=spend, today=today, flash=flash,
                 sessions=sessions, session_limit=session_limit,
                 queued=queued[:8], queued_total=len(queued), recent=recent,
                 link_base=link_base, refreshed=datetime.now().strftime("%H:%M:%S"),
                 quiet_warn_seconds=_QUIET_WARN_SECONDS,
                 quiet_stuck_seconds=_QUIET_STUCK_SECONDS),
        )
        if flash:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/sessions/{session_id}/cancel")
    async def session_cancel(request: Request, session_id: int):
        """✕ Close an `/ai` session — the branch is left exactly as it was."""
        monitor = getattr(request.app.state, "pr_monitor", None)
        if monitor is None or not await monitor.cancel_session(session_id):
            return _flash("/dashboard/now", "session_not_open")
        await request.app.state.container.audit_repo.record(
            actor="dashboard", source="dashboard", action="pr.session_closed",
            target=f"session {session_id}",
        )
        return _flash("/dashboard/now", "session_closed")

    @router.get("/queue", response_class=HTMLResponse)
    async def queue_page(request: Request, resumed: int = 0):
        """Needs human: items the autopilot ESCALATED, with the reason, so a person can
        Resume them (approve/redirect) in one place.

        Nothing to do with reviewing pull requests, which is what the name "Review queue"
        said to anyone reading it directly under "Reviews" — the page next to it that
        really is about PR reviewers. Two adjacent entries, one word apart, unrelated:
        people opened this looking for their PR review, found an empty page, and
        concluded the feature did not work.
        """
        c: Container = request.app.state.container
        link_base = work_item_link_base(c.config)
        held = [s for s in await c.state_repo.all() if s.state == PipelineState.NEEDS_HUMAN]
        held.sort(key=lambda s: s.updated_at or datetime.min, reverse=True)
        held = await _scope_rows(request, c, held)
        items = [
            {
                "id": s.work_item_id, "title": s.title or f"#{s.work_item_id}",
                "detail": s.detail or "", "pr_url": s.pr_url or "",
                "url": f"{link_base}/{s.work_item_id}" if link_base else "",
                "updated": s.updated_at,
            }
            for s in held
        ]
        return _TEMPLATES.TemplateResponse(
            request, "queue.html", _ctx(request, "queue", items=items, resumed=resumed)
        )

    @router.post("/queue/resume")
    async def queue_resume(request: Request):
        """Approve/resume held items: clear the hold tag and hand them back to the
        poller (trigger tag + state), so work continues without a manual restart."""
        c: Container = request.app.state.container
        form = await request.form()
        ids = [int(x) for x in form.getlist("ids") if str(x).strip().isdigit()]
        if not ids:
            return RedirectResponse("/dashboard/queue", status_code=303)
        # EVERY outcome tag, not just the hold: the poller skips an item carrying any of
        # them, so an item held while it also wore "autopilot-done" was set to Queued
        # here and then never picked up — it sat in Queued for good.
        from ai_autopilot.outcomes import all_outcome_tags

        skip_tags = {t.lower() for t in all_outcome_tags(c.config)}
        poller = getattr(request.app.state, "poller", None)
        for iid in ids:
            with contextlib.suppress(Exception):
                item = await c.ado.get_work_item(iid)
                for tag in (item.tags if item else []):
                    if tag.lower() in skip_tags:
                        await c.ado.remove_tag(iid, tag)
            if poller is not None:
                with contextlib.suppress(Exception):
                    poller.forget(iid)       # dedup + retry budget: a fresh run
        started = await planning_analyzer.start_items(c, ids)
        for iid in ids:  # leave the queue immediately; the poller will re-own the state
            await c.state_repo.set(iid, PipelineState.QUEUED)
        _log.info("resumed held items via queue", ids=ids, started=started)
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="item.resumed",
            target=", ".join(f"#{i}" for i in ids)[:300],
        )
        return RedirectResponse(f"/dashboard/queue?resumed={started}", status_code=303)

    @router.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request, action: str = "", limit: int = 100):
        """Append-only audit trail: who did what (config, tickets, resumes, reviews)."""
        c: Container = request.app.state.container
        events = await c.audit_repo.recent(limit=max(1, min(limit, 500)), action=action)
        actions = sorted({e.action.split(".")[0] + "." for e in events})
        return _TEMPLATES.TemplateResponse(
            request, "audit.html",
            _ctx(request, "audit", events=events, action=action, actions=actions),
        )

    @router.get("/quality", response_class=HTMLResponse)
    async def quality_page(
        request: Request, days: int = 30, kind: str = "", view: str = "all", q: str = "",
        sort: str = "rework", page: int = 1, per: int = 25, epage: int = 1, eper: int = 25,
        item: int = 0,
    ):
        """Rework & review quality: how often each item had to be redone, why, and what
        humans voted — read from the append-only log that outlives every budget.

        Both tables page on the server: the per-item table is aggregated in Python and
        sliced, the event log pages in SQL (offset + count), so a year's window stays a
        25-row page rather than a 2 000-row one.
        """
        c: Container = request.app.state.container
        days = max(1, min(days, 365))
        per = per if per in _QUALITY_PER else 25
        eper = eper if eper in _QUALITY_PER else 25
        view = view if view in ("all", "reworked", "blocked", "clean") else "all"
        sort = sort if sort in ("rework", "last", "vote", "id") else "rework"
        since = datetime.now() - timedelta(days=days)
        all_rows = await c.quality_events.rework_rows(since=since)
        totals = await c.quality_events.kind_totals(since=since)
        titles = {s.work_item_id: s.title for s in await c.state_repo.all()}

        # Headline numbers are over the WHOLE window — filters narrow the table, never
        # the answer to "how are we doing".
        items = len(all_rows)
        reworked = sum(1 for r in all_rows if r.rework)
        causes = {
            "retries": sum(r.retries for r in all_rows),
            "pr_revisions": sum(r.pr_revisions for r in all_rows),
            "sdlc_iterations": sum(r.sdlc_iterations for r in all_rows),
            "reopens": sum(r.reopens for r in all_rows),
        }
        total_rework = sum(causes.values())
        kpis = {
            "rework": total_rework, "item_count": items, "reworked": reworked,
            "per_item": (total_rework / items) if items else 0.0,
            "first_pass": ((items - reworked) / items * 100) if items else 0.0,
            "blocked": sum(1 for r in all_rows if r.worst_vote < 0),
            "block_votes": sum(r.rejections for r in all_rows),
            "tests": totals.get("test_failed", 0),
        }
        counts = {
            "all": items, "reworked": reworked, "clean": items - reworked,
            "blocked": kpis["blocked"],
        }

        rows = all_rows
        if view == "reworked":
            rows = [r for r in rows if r.rework]
        elif view == "clean":
            rows = [r for r in rows if not r.rework]
        elif view == "blocked":
            rows = [r for r in rows if r.worst_vote < 0]
        needle = q.strip().lower().lstrip("#")
        if needle:
            rows = [r for r in rows if needle in str(r.work_item_id)
                    or needle in (titles.get(r.work_item_id, "") or "").lower()]
        epoch = datetime.min
        if sort == "last":
            rows = sorted(rows, key=lambda r: r.last_at or epoch, reverse=True)
        elif sort == "vote":
            rows = sorted(rows, key=lambda r: (r.worst_vote, -r.rework))
        elif sort == "id":
            rows = sorted(rows, key=lambda r: -r.work_item_id)
        rp = _pager(len(rows), page, per)
        page_rows = rows[rp["start"]:rp["end"]]

        etotal = await c.quality_events.count(kind=kind, work_item_id=item, since=since)
        ep = _pager(etotal, epage, eper)
        events = await c.quality_events.recent(
            limit=eper, kind=kind, work_item_id=item, since=since, offset=ep["start"],
        )

        params = {"days": days, "kind": kind, "view": view, "q": q, "sort": sort,
                  "page": rp["page"], "per": per, "epage": ep["page"], "eper": eper,
                  "item": item}
        defaults = {"days": 30, "view": "all", "sort": "rework", "page": 1, "epage": 1,
                    "per": 25, "eper": 25}

        def url(anchor: str = "", **over) -> str:
            """This page's URL with some parameters changed — every link on the page is
            built here, so changing one filter never silently drops another."""
            merged = {**params, **over}
            keep = {k: v for k, v in merged.items()
                    if v not in ("", 0, None) and defaults.get(k) != v}
            return "/dashboard/quality" + (("?" + urlencode(keep)) if keep else "") + anchor

        return _TEMPLATES.TemplateResponse(
            request, "quality.html",
            _ctx(
                request, "quality", rows=page_rows, rp=rp, ep=ep, events=events,
                totals=totals, titles=titles, days=days, kind=kind, view=view, q=q,
                sort=sort, item=item, kpis=kpis, causes=causes, counts=counts,
                kinds=sorted(totals), rework_kinds=set(QualityKind.REWORK),
                per_options=_QUALITY_PER, url=url,
            ),
        )

    return router
