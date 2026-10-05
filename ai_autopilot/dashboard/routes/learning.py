"""Retrospective lessons: review, add, edit, prune."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ai_autopilot import fleet as fleet_mod
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _json_list,
    _log,
    _take_flash,
)
from ai_autopilot.dashboard.routes._shared import _ctx


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/learning", response_class=HTMLResponse)
    async def learning_page(request: Request, days: int = 30):
        """The retrospective learning loop, made visible: what the autopilot remembers
        per repo, which of it feeds the next brief, and how often it is being used.

        Without this page the loop is invisible — lessons live in files nobody opens, so
        a wrong lesson keeps poisoning every future brief with no way to notice.
        """
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        cfg = c.config
        workspace = cfg.workspace_directory
        days = max(1, min(days, 180))
        repos = lessons_mod.list_repos(workspace)
        limit = max(0, cfg.lessons_max_injected)
        # The exact lines the next brief will carry, from the SAME function the
        # executor calls. A page that recomputes its own idea of "what gets injected"
        # is a page that will eventually disagree with the agent and be believed.
        preview = {
            repo: lessons_mod.recent(workspace, [repo], limit=limit) for repo in repos
        }
        # Which ROWS those lines are. This used to be "the newest N of this repo", which
        # is not the rule `recent()` follows: it puts authored/fleet lines first and only
        # then the newest learned ones. Over the cap the two disagreed in the worst
        # possible direction — measured on a 13-line file: the standing rule a human had
        # typed WAS injected and showed no marker, while a machine line that was NOT
        # injected showed one. The page's whole claim is that it shows what the agent is
        # told, so it has to ask the same function, then match on meaning (normalize())
        # rather than on the exact string, which is how `recent()` dedups across repos.
        injected_keys = {
            repo: {lessons_mod.normalize(line) for line in lines}
            for repo, lines in preview.items()
        }
        groups = [
            (
                repo,
                [
                    (le, lessons_mod.normalize(le.text) in injected_keys[repo])
                    for le in lessons_mod.entries(workspace, repo)
                ],
            )
            for repo in repos
        ]
        # How many of THIS repo's own rows feed its brief (the rest of a brief's budget
        # is filled from the shared bucket, which is a different card on the page).
        injected_from = {
            repo: sum(1 for _, live in rows if live) for repo, rows in groups
        }
        # The shared bucket rides along in EVERY repo's brief, so rendering each brief
        # whole printed the same three lines once per repo — on a two-repo workspace the
        # biggest block on the page was half duplicates, and the reader had to diff three
        # near-identical code blocks by eye to find the part that actually differed.
        # Split once here: what every brief carries, then what each repo adds on top.
        # What the agent actually reads. The lessons are written into the workspace's
        # Claude memory, which the run loads by itself via setting_sources — so THIS is
        # the primary channel now, and the brief injection below is the escape hatch.
        memory_file = str(lessons_mod.memory_path(workspace)) if workspace else ""
        memory_body = lessons_mod.render_rules(workspace) if workspace else ""
        # What the brief will actually say, from the same function the executor
        # calls — so the page cannot describe a pointer the agent never gets.
        memory_pointer = (
            lessons_mod.lessons_pointer(workspace, repos).strip() if workspace else ""
        )
        memory_live = lessons_mod.memory_is_live(workspace)
        shared_lines = preview.get(lessons_mod.SHARED_BUCKET, [])
        shared_keys = {lessons_mod.normalize(line) for line in shared_lines}
        preview_own = {
            repo: [
                line for line in lines
                if lessons_mod.normalize(line) not in shared_keys
            ]
            for repo, lines in preview.items()
            if repo != lessons_mod.SHARED_BUCKET
        }
        flash = _take_flash(request)
        series = lessons_mod.per_day(workspace)[-days:]
        today = datetime.now().date().isoformat()
        try:
            dfrom = (datetime.now() - timedelta(days=days - 1)).date().isoformat()
            records, _ = await c.execution_repo.search(dfrom=dfrom, limit=5000)
        except Exception:  # noqa: BLE001 — the page must render without history
            records = []
        authored = sum(
            1 for _, rows in groups for le, _ in rows if le.authored
        )
        # The central's review queue. Only a central has one: a worker contributes and
        # receives, but it is not the place decisions get made — one machine approving
        # for the whole fleet from wherever it happens to be is how a wrong lesson
        # spreads before anyone with context sees it.
        # What the centre has approved and this machine has not answered. Only a
        # worker in manual mode ever has any: on auto they are applied on arrival.
        pending_offers = lessons_mod.offers(workspace) if workspace else []
        declined_count = len(lessons_mod.declined_keys(workspace)) if workspace else 0
        pooled: list[dict] = []
        is_central = (cfg.fleet_role or "") == fleet_mod.ROLE_CENTRAL
        if is_central:
            repo_store = getattr(c, "fleet_knowledge_repo", None)
            if repo_store is not None:
                with contextlib.suppress(Exception):   # the page renders without it
                    pooled = [
                        {
                            "key": row.key, "text": row.text, "repo": row.repo,
                            "status": row.status, "occurrences": row.occurrences,
                            "origins": _json_list(row.origins),
                            "source": row.source,
                        }
                        for row in await repo_store.list_all()
                    ]
        response = _TEMPLATES.TemplateResponse(
            request, "learning.html",
            _ctx(
                request, "learning", flash=flash,
                enabled=cfg.learning_loop_enabled, workspace=workspace,
                repos=repos, groups=groups, injected_from=injected_from,
                preview=preview, shared_bucket=lessons_mod.SHARED_BUCKET,
                shared_lines=shared_lines, preview_own=preview_own,
                memory_file=memory_file, memory_body=memory_body,
                memory_pointer=memory_pointer,
                memory_live=memory_live,
                total=sum(len(rows) for _, rows in groups),
                authored=authored,
                # Repeats are collapsed now, so this counts the occurrences BEHIND the
                # lines — "11 lessons" hid the fact that five of them were one event
                # five times, which is the number that actually says how bad it is.
                # Learned lines only. Counting the authored ones in made the number
                # say "gộp từ 10 lần xảy ra" about a tile labelled "máy tự học", when
                # three of those ten were rules somebody typed once.
                occurrences=sum(
                    le.count for _, rows in groups for le, _ in rows if not le.authored
                ),
                pooled=pooled, is_central=is_central,
                pending_offers=pending_offers, declined_count=declined_count,
                auto_promote=cfg.fleet_knowledge_auto_promote,
                max_injected=limit, series=series,
                peak=max((n for _, n in series), default=0),
                new_today=sum(n for day, n in series if day == today),
                days=days,
                injected_total=sum(r.lessons_injected or 0 for r in records),
                injected_runs=sum(1 for r in records if r.lessons_injected),
            ),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/learning/add")
    async def learning_add(request: Request):
        """Put knowledge a HUMAN wrote in front of the agent.

        The page could delete and forget but never add, so the only thing that ever
        reached a brief was the machine's own post-mortem of its mistakes — the team's
        actual conventions, the ones that stop the mistake happening at all, had no
        door in. One line per line pasted, so a conventions document can be dropped in
        whole rather than retyped a sentence at a time.
        """
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        workspace = c.config.workspace_directory
        form = await request.form()
        repo = str(form.get("repo") or "").strip() or lessons_mod.SHARED_BUCKET
        blob = str(form.get("text") or "")
        if not workspace:
            return _flash("/dashboard/learning", "lesson_no_workspace")
        added = lessons_mod.add_many(workspace, repo, blob)
        if not added:
            return _flash("/dashboard/learning", "lesson_none_added")
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="knowledge.added",
            target=f"{repo}: {added} dòng", detail=blob[:300],
        )
        _log.info("knowledge added via dashboard", repo=repo, lines=added)
        return _flash("/dashboard/learning", "lesson_added")

    @router.post("/learning/offer/{decision}")
    async def learning_offer_decide(request: Request, decision: str):
        """Take, or refuse, one line the centre approved.

        A refusal is permanent and is recorded HERE. Before this, deleting a line the
        centre had sent lasted until the next beat put it back — so the only machine
        that had no say about what it was told was the one that had to live with it.
        """
        if decision not in ("accept", "decline"):
            raise HTTPException(status_code=404, detail="unknown decision")
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        workspace = c.config.workspace_directory
        form = await request.form()
        text = str(form.get("text", "")).strip()
        if not workspace or not text:
            return _flash("/dashboard/learning", "lesson_none_added")
        if decision == "accept":
            ok = lessons_mod.accept_offer(workspace, text)
        else:
            ok = lessons_mod.decline_fleet(workspace, text)
        await c.audit_repo.record(
            actor="dashboard", source="dashboard",
            action=f"knowledge.{decision}ed", target=text[:300],
        )
        return _flash(
            "/dashboard/learning",
            f"offer_{decision}ed" if ok else "lesson_none_added",
        )

    @router.post("/learning/pool/{decision}")
    async def learning_pool_decide(request: Request, decision: str):
        """Approve or reject one line the fleet contributed.

        ``rejected`` is kept rather than deleted: the machines that still hold the bad
        lesson locally re-contribute it on every beat, so a deleted row would come
        straight back and the same line would face the reviewer forever.
        """
        if decision not in ("approved", "rejected"):
            raise HTTPException(status_code=404, detail="unknown decision")
        c: Container = request.app.state.container
        if (c.config.fleet_role or "") != fleet_mod.ROLE_CENTRAL:
            raise HTTPException(status_code=404, detail="only a central curates")
        store = getattr(c, "fleet_knowledge_repo", None)
        if store is None:
            raise HTTPException(status_code=404, detail="no knowledge store")
        form = await request.form()
        key = str(form.get("key") or "")
        if await store.set_status(key, decision):
            await c.audit_repo.record(
                actor="dashboard", source="dashboard",
                action=f"knowledge.{decision}", target=key[:300],
            )
        return _flash("/dashboard/learning",
                      "pool_approved" if decision == "approved" else "pool_rejected")

    @router.post("/learning/compact")
    async def learning_compact(request: Request):
        """Fold duplicates and drop dead lines in files an older build wrote.

        Runs once at startup too; this is the button for when somebody wants it now,
        or after hand-editing the files.
        """
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        merged, dropped = lessons_mod.compact_all(c.config.workspace_directory)
        if merged or dropped:
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="knowledge.compacted",
                target=f"gộp {merged}, bỏ {dropped}",
            )
            _log.info("knowledge compacted via dashboard", merged=merged, dropped=dropped)
        return _flash("/dashboard/learning",
                      "compacted" if (merged or dropped) else "compact_clean")

    @router.post("/learning/edit")
    async def learning_edit(request: Request):
        """Reword one line in place, keeping its date, source and recurrence count.

        Deleting and retyping was the only correction available, and it threw away
        exactly the two facts that make a line worth reading: when it was learned and
        how many times it has come back.
        """
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        form = await request.form()
        repo = str(form.get("repo") or "")
        old, new = str(form.get("old") or ""), str(form.get("text") or "")
        if lessons_mod.edit(c.config.workspace_directory, repo, old, new):
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="knowledge.edited",
                target=f"{repo}: {new}"[:300], detail=old[:300],
            )
            return _flash("/dashboard/learning", "lesson_edited")
        return _flash("/dashboard/learning", "lesson_none_added")

    @router.post("/learning/delete")
    async def learning_delete(request: Request):
        """Prune ONE lesson. A wrong lesson is worse than none — it is re-taught every run."""
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        form = await request.form()
        repo, text = str(form.get("repo") or ""), str(form.get("text") or "")
        if lessons_mod.delete(c.config.workspace_directory, repo, text):
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="lesson.deleted",
                target=f"{repo}: {text}"[:300],
            )
        return RedirectResponse("/dashboard/learning", status_code=303)

    @router.post("/learning/clear")
    async def learning_clear(request: Request):
        """Forget everything learned about one repo (e.g. after a rewrite)."""
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        form = await request.form()
        repo = str(form.get("repo") or "")
        if lessons_mod.clear(c.config.workspace_directory, repo):
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="lesson.cleared", target=repo[:300],
            )
        return RedirectResponse("/dashboard/learning", status_code=303)

    return router
