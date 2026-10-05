"""Overview, the Board and its saved views, and the board's run/move actions."""

from __future__ import annotations

import contextlib
from datetime import datetime
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ai_autopilot import lenses as lenses_mod
from ai_autopilot.board import (
    COL_READY_DEPLOY,
    COL_READY_REVIEW,
    COL_READY_TESTING,
    board_columns,
    build_board,
    handoff_states,
    latest_pr_records,
    latest_records,
    parse_drop_map,
)
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.dashboard.common import (
    _BOARD_ITEMS,
    _FLASH_COOKIE,
    _LENS_ERROR_COOKIE,
    _TEMPLATES,
    _flash,
    _lens_reject,
    _log,
    _pr_outcomes,
    _take_flash,
    _take_lens_reject,
    scope_of,
    work_item_link_base,
)
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.execution import sdlc_plan
from ai_autopilot.lenses import (
    board_view,
    board_views,
    group_lanes,
    lens_tag_matches,
    my_turn_count,
    render_lanes,
)
from ai_autopilot.services import planning_analyzer

_BOARD_CATS = {
    "BE": "[BE]", "FE": "[FE]", "DB": "[DB]", "QC": "[QC]", "TEST": "[TEST]",
}


def _filter_board_items(items: list, q: str, cat: str, dfrom: str, dto: str) -> list:
    """Apply the board's search / category / changed-date filters to work items."""
    out = items
    if q:
        ql = q.lower()
        out = [i for i in out if ql in str(i.id) or ql in (i.title or "").lower()]
    if cat and cat != "all":
        marker = _BOARD_CATS.get(cat.upper())
        if marker:
            out = [i for i in out if (i.title or "").upper().startswith(marker)]
    if dfrom or dto:
        def _in_range(i) -> bool:
            cd = getattr(i, "changed_date", None)
            if cd is None:
                return False               # can't verify a date → exclude when filtering
            d = cd.date().isoformat()
            if dfrom and d < dfrom:
                return False
            if dto and d > dto:
                return False
            return True
        out = [i for i in out if _in_range(i)]
    return out


async def _board_ctx(request: Request) -> dict:
    c: Container = request.app.state.container
    qp = request.query_params
    try:
        # Concurrent tabs and users join one scan instead of each starting their own.
        items = await _BOARD_ITEMS.coalesced(c.ado.get_all_tagged_work_items) or []
        error = None
    except Exception as exc:  # noqa: BLE001
        items, error = [], str(exc)
    tags = c.config.effective_trigger_tags
    selected_tag = qp.get("tag") or "all"
    if selected_tag != "all":
        sel = selected_tag.lower()
        items = [i for i in items if any(t.lower() == sel for t in i.tags)]

    # The sidebar's workspace choice narrows the board first; the project dropdown
    # then narrows within it. Two levels because a workspace can hold several
    # projects — picking the workspace is "whose board is this", picking the
    # project is "which stream inside it".
    _, in_scope = scope_of(request, c.config)
    if in_scope is not None:
        allowed = {p.lower() for p in in_scope}
        items = [i for i in items if (i.project or "").lower() in allowed]
    projects = in_scope if in_scope is not None else c.config.effective_ado_projects
    selected_project = qp.get("project") or "all"
    if selected_project != "all":
        sel_proj = selected_project.lower()
        items = [i for i in items if (i.project or "").lower() == sel_proj]

    # Filters: search (id/title), category, changed-date range.
    q = (qp.get("q") or "").strip()
    cat = (qp.get("cat") or "all").strip()
    dfrom = (qp.get("from") or "").strip()
    dto = (qp.get("to") or "").strip()
    items = _filter_board_items(items, q, cat, dfrom, dto)

    records = await c.execution_repo.get_recent(200)
    states = {s.work_item_id: s.state.value for s in await c.state_repo.all()}
    cols = build_board(
        items, latest_records(records), c.config, states,
        # The PR link comes from the run that OPENED one, not from whatever ran
        # last — in review, what ran last IS the review. See `latest_pr_records`.
        pr_records_by_id=latest_pr_records(records),
    )

    # Eight columns is the pipeline's shape, not a person's question. The lens
    # folds them into the few lanes one role reads; no card is dropped, so the
    # totals match whichever lens is on.
    view = board_view(c.config, qp.get("view"))
    views = board_views(c.config)
    parked = lenses_mod.parked_states(views)
    parked_tags = lenses_mod.parked_tags(views)
    lane_cards = group_lanes(cols, view, parked, parked_tags)
    # "Only my turn": the relay's default question. Work passes BA → Dev → QC,
    # so a role mostly wants the lanes where the ball is in ITS court; the rest
    # stay one click away as upstream/downstream context.
    # Ignored for a view that claims no lane (the raw pipeline, or a process
    # still being configured) — filtering to nothing would render a blank board
    # and read as "no work", which is the one answer it must never give.
    only_mine = (qp.get("mine") or "").strip() in {"1", "true", "yes"} and any(
        lane.mine for lane in view.lanes
    )

    # Per-column display cap + "load more".
    cap = max(0, getattr(c.config, "board_max_per_column", 20))
    try:
        limit = int(qp.get("limit") or cap)
    except ValueError:
        limit = cap
    limit = max(0, limit)

    base = {}
    if view.key != "pipeline":
        base["view"] = view.key
    if only_mine:
        base["mine"] = "1"
    if selected_tag != "all":
        base["tag"] = selected_tag
    if selected_project != "all":
        base["project"] = selected_project
    if q:
        base["q"] = q
    if cat and cat != "all":
        base["cat"] = cat
    if dfrom:
        base["from"] = dfrom
    if dto:
        base["to"] = dto
    filter_qs = urlencode(base)
    step = cap or 20
    more_qs = urlencode({**base, "limit": (limit or step) + step})

    org = c.config.ado_organization.rstrip("/")
    proj = c.config.ado_project
    ado_item_base = work_item_link_base(c.config)
    # Each card links into ITS OWN project — see BoardCard.url.
    if org:
        for column_cards in cols.values():
            for card in column_cards:
                card_project = card.project or proj
                if card_project:
                    card.url = (
                        f"{org}/{quote(card_project)}/_workitems/edit/{card.id}"
                    )
    return _ctx(
        request,
        "board",
        board=lane_cards,
        columns=[lane.name for lane in view.lanes],
        lanes=[
            row for row in render_lanes(view, lane_cards, limit)
            if row.lane.mine or not only_mine
        ],
        # Which lanes a card can actually be dropped on. Without a rule the drop
        # endpoint returns 204 and nothing happens — a card that looks draggable
        # and then silently snaps back is worse than one that is plainly not.
        drop_targets=sorted(parse_drop_map(c.config.board_drop_map)),
        # A Run button only makes sense where a person can actually start work:
        # a process view (not the raw pipeline) on a machine that writes.
        can_run=bool(view.key != "pipeline" and not c.config.dry_run),
        only_mine=only_mine,
        my_turn=my_turn_count(lane_cards, view),
        # Each tab carries the count that decides whether it is worth opening:
        # how many items are waiting on THAT role right now.
        turn_counts={
            v.key: my_turn_count(group_lanes(cols, v, parked, parked_tags), v)
            for v in views
        },
        mine_url="/dashboard/board?" + urlencode(
            {**base, "mine": "1"} if not only_mine
            else {k: val for k, val in base.items() if k != "mine"}
        ),
        views=views,
        view=view,
        # A lens shows only the items its process owns, so the header has to say
        # how many of the board's items that is — otherwise switching lens looks
        # like work disappeared.
        in_view=sum(len(v) for v in lane_cards.values()),
        role_chips={
            card.id: [v.label for v in lens_tag_matches(card.tags, views)]
            for column_cards in cols.values()
            for card in column_cards
        },
        lens_urls={
            v.key: "/dashboard/board?"
            + urlencode({**{k: val for k, val in base.items() if k != "view"},
                         **({} if v.key == "pipeline" else {"view": v.key})})
            for v in views
        },
        total=len(items),
        error=error,
        project=c.config.ado_project,
        projects=projects,
        selected_project=selected_project,
        interactive=c.config.execution_mode == "interactive",
        tags=tags,
        selected_tag=selected_tag,
        ado_item_base=ado_item_base,
        board_limit=limit,
        filter_qs=filter_qs,
        more_url="/dashboard/board?" + more_qs,
        q=q, cat=cat, date_from=dfrom, date_to=dto,
    )


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        c: Container = request.app.state.container
        selected_tag = request.query_params.get("tag") or "all"
        tag_filter = None if selected_tag == "all" else selected_tag
        # Scoped to the selected workspace. Runs recorded before the project column
        # existed carry no project, so they show only in the unscoped view — better a
        # visibly missing row than another workspace's run counted here.
        _, in_scope = scope_of(request, c.config)
        stats = await c.execution_repo.get_stats(trigger_tag=tag_filter, projects=in_scope)
        recent = await c.execution_repo.get_recent(
            20, trigger_tag=tag_filter, projects=in_scope
        )
        efficiency = await c.execution_repo.get_efficiency(
            trigger_tag=tag_filter, projects=in_scope
        )
        prs = _pr_outcomes(c)   # cached; refreshes behind the page, never blocks it
        tokens_per_merged = (
            efficiency.total_tokens // prs["merged"] if prs["merged"] else None
        )
        # "Needs attention now" — all local DB reads (this page never waits on ADO).
        # Each figure degrades to None alone: one broken source must not blank the rest.
        running = sec_open = sec_kev = conflicts_active = conflicts_escalated = None
        spend_today = None
        with contextlib.suppress(Exception):
            running = await c.execution_repo.count_running(projects=in_scope)
        with contextlib.suppress(Exception):
            sc = await c.security_repo.counts()
            open_by_sev = sc.get("open", {})
            sec_open = open_by_sev.get("critical", 0) + open_by_sev.get("high", 0)
            sec_kev = await c.security_repo.open_kev_count()
        with contextlib.suppress(Exception):
            active_rows = await c.pr_conflict_repo.active()
            conflicts_active = len(active_rows)
            conflicts_escalated = sum(1 for r in active_rows if r.status == "escalated")
        with contextlib.suppress(Exception):
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            spend_today = await c.execution_repo.spend_since(midnight, projects=in_scope)
        return _TEMPLATES.TemplateResponse(
            request,
            "overview.html",
            _ctx(
                request,
                "overview",
                stats=stats,
                recent=recent,
                efficiency=efficiency,
                prs=prs,
                tokens_per_merged=tokens_per_merged,
                tags=c.config.effective_trigger_tags,
                selected_tag=selected_tag,
                # Outstanding spec drift belongs on the landing page: it is work owed to
                # a HUMAN, so it has to be visible without navigating to find it.
                spec_drift_open=await c.spec_drift_repo.open_count(),
                running=running,
                sec_open=sec_open, sec_kev=sec_kev,
                conflicts_active=conflicts_active, conflicts_escalated=conflicts_escalated,
                spend_today=spend_today,
            ),
        )

    @router.get("/board", response_class=HTMLResponse)
    async def board(request: Request):
        return _TEMPLATES.TemplateResponse(request, "board.html", await _board_ctx(request))

    @router.get("/board-views", response_class=HTMLResponse)
    async def board_views_page(request: Request):
        c: Container = request.app.state.container
        cols = board_columns(c.config)
        flash = _take_flash(request)
        rejected = _take_lens_reject(request)
        lenses = rejected.get("lenses") or lenses_mod.lens_dicts(c.config)

        # A process is only real if its tags exist on the board. Reading the live
        # items lets the page answer the question an operator actually has — "will
        # this show anything?" — instead of leaving them to guess and find an empty
        # board. Fail-soft: ADO being down costs the counts, not the editor.
        try:
            items = await c.ado.get_all_tagged_work_items()
        except Exception:  # noqa: BLE001
            items = []
        tag_counts: dict[str, int] = {}
        for item in items:
            for tag in item.tags or []:
                name = (tag or "").strip()
                if name:
                    tag_counts[name] = tag_counts.get(name, 0) + 1
        board_tags = sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:40]
        # Every state a lane could claim — from ADO, PER WORK-ITEM TYPE, not from the
        # items that happen to be on the board. A Requirement's states ("Ready for
        # UAT") exist whether or not a requirement is in flight right now, and a
        # picker built from live items alone silently offers only Task/Bug states —
        # so the one hand-off a BA needs most is the one that cannot be picked.
        try:
            states_by_type = await c.ado.get_states_by_type()
        except Exception:  # noqa: BLE001 — ADO down costs the picker, not the editor
            states_by_type = {}
        types_of: dict[str, list[str]] = {}
        for type_name, names in sorted(states_by_type.items()):
            for name in names:
                clean = (name or "").strip()
                if clean:
                    types_of.setdefault(clean, []).append(type_name)
        state_counts: dict[str, int] = {}
        for item in items:
            name = (item.state or "").strip()
            if name:
                state_counts[name] = state_counts.get(name, 0) + 1
        in_use = set(state_counts)
        for name in in_use:  # a state on the board that ADO no longer lists is still real
            types_of.setdefault(name, [])
        # States something is actually sitting in read FIRST. A process is configured by
        # looking for the spot work really parks at; alphabetical order buries those five
        # names among thirty dead ones and makes the picker a memory test.
        board_states = [
            {"name": name, "types": ", ".join(types_of[name]) or "not in the type list",
             "used": name in in_use, "n": state_counts.get(name, 0)}
            for name in sorted(types_of, key=lambda s: (s not in in_use, s.lower()))
        ]

        def _matches(lens: dict) -> int:
            wanted = {str(t).strip().lower() for t in (lens.get("tags") or []) if str(t).strip()}
            if not wanted:
                return len(items)
            return sum(
                1 for i in items if wanted & {(t or "").strip().lower() for t in (i.tags or [])}
            )

        # "Waiting on this process" is the number that actually decides whether a
        # process is configured usefully: how many items sit in a stage it marked as
        # its turn. It needs the real board, so it is derived from the live view.
        recent = await c.execution_repo.get_recent(200)
        pipeline_states = {s.work_item_id: s.state.value for s in await c.state_repo.all()}
        live = build_board(
            items, latest_records(recent), c.config, pipeline_states,
            pr_records_by_id=latest_pr_records(recent),
        )

        all_views = lenses_mod.board_views(c.config)
        parked = lenses_mod.parked_states(all_views)
        parked_tag_set = lenses_mod.parked_tags(all_views)

        def _waiting_on(lens: dict) -> int:
            view = lenses_mod.view_of(lens, cols)
            if view is None:
                return 0
            return lenses_mod.my_turn_count(
                lenses_mod.group_lanes(live, view, parked, parked_tag_set), view
            )

        # Two processes claiming the same column both think the ball is theirs. That
        # is legal (an escalation needs BA and Dev) but it is worth saying out loud,
        # because the usual cause is a missing hand-off state, not a deliberate choice.
        owners: dict[str, list[str]] = {}
        for raw_lens in lenses:
            view = lenses_mod.view_of(raw_lens, cols)
            if view is None:
                continue
            for claim in lenses_mod.my_turn_claims(view):
                if claim not in lenses_mod.SHARED_COLUMNS:
                    owners.setdefault(claim, []).append(view.label)
        shared_turns = {col: names for col, names in owners.items() if len(names) > 1}
        # A process with no turn of its own is a read-only board: it can watch, but the
        # relay never stops at it. Almost always the missing piece is the hand-off state
        # that would give it a column, so it is reported next to that fix.
        no_turn = [
            str(x.get("label") or x.get("key"))
            for x in lenses
            if isinstance(x, dict)
            and (v := lenses_mod.view_of(x, cols)) is not None
            and not lenses_mod.my_turn_claims(v)
        ]

        # Every field the FORM can submit has to survive this copy. It is a whitelist,
        # so a field left out is not merely hidden — the form re-posts without it and
        # the next save DELETES it. `mine` and `profile` were missing: the "your turn"
        # box always drew empty and ▶ Run always read "— off —", and pressing Save on
        # that page wiped both from the config. Add a field to the editor, add it here.
        shown = [
            {
                "key": str(x.get("key") or ""),
                "label": str(x.get("label") or ""),
                "icon": str(x.get("icon") or ""),
                "hint": str(x.get("hint") or ""),
                "profile": str(x.get("profile") or ""),
                "tags": [str(t) for t in (x.get("tags") or [])],
                "stages": [
                    {
                        "name": str(st.get("name") or ""),
                        "columns": [str(cc) for cc in (st.get("columns") or [])],
                        "states": [str(v) for v in (st.get("states") or [])],
                        "tags": [str(v) for v in (st.get("tags") or [])],
                        "tone": str(st.get("tone") or "slate"),
                        "hint": str(st.get("hint") or ""),
                        "drop": str(st.get("drop") or ""),
                        "mine": bool(st.get("mine")),
                    }
                    for st in (x.get("stages") or [])
                    if isinstance(st, dict)
                ],
                "gaps": lenses_mod.coverage_gaps(x, cols),
                "matches": _matches(x),
                "turn": _waiting_on(x),
                "tag_hint": ", ".join(
                    lenses_mod.suggested_role_tags(c.config, str(x.get("key") or ""))[:2]
                ),
            }
            for x in lenses
            if isinstance(x, dict)
        ]
        response = _TEMPLATES.TemplateResponse(
            request, "board_views.html",
            _ctx(request, "board-views", lenses=shown, columns=cols,
                 tones=lenses_mod.TONES, flash=flash,
                 board_tags=board_tags, board_states=board_states,
                 board_total=len(items),
                 profiles=sdlc_plan.profile_names(c.config),
                 done_tag=c.config.processed_tag, review_tag_name=c.config.review_tag,
                 hold_tag=c.config.escalation_tag,
                 sdlc_on=c.config.sdlc_loop_enabled,
                 shared_turns=shared_turns, no_turn=no_turn,
                 review_state=c.config.board_review_state,
                 deploy_state=c.config.board_deploy_state,
                 # Hand-off columns this config has NOT switched on. The page offers
                 # them as the usual cure for "no stage of its own" / "two processes
                 # claim the same hand-off", so it needs the names, not three flags.
                 missing_handoffs=[
                     name for name, states in (
                         (COL_READY_REVIEW, c.config.board_review_state),
                         (COL_READY_DEPLOY, c.config.board_deploy_state),
                         (COL_READY_TESTING, c.config.board_testing_state),
                     ) if not handoff_states(states)
                 ],
                 errors=rejected.get("errors") or []),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        if rejected:
            response.delete_cookie(_LENS_ERROR_COOKIE, path="/dashboard")
        return response

    @router.post("/board-views")
    async def save_board_views(request: Request):
        """Save the per-process lenses, or restore the built-in set."""
        c: Container = request.app.state.container
        form = await request.form()
        if form.get("reset"):
            updates = {"board_lenses": []}   # empty = fall back to DEFAULT_LENSES
            settings_form.save_to_yaml(config_file_path(), updates)
            settings_form.apply_to_config(c.config, updates)
            _log.info("board lenses reset to defaults via dashboard")
            return _flash("/dashboard/board-views", "lens_reset")

        cols = board_columns(c.config)
        parsed = lenses_mod.parse_lens_form(form, cols)
        errors = lenses_mod.validate_lenses(parsed, cols)
        if errors:
            _log.info("board lenses rejected", count=len(errors))
            return _lens_reject(errors, parsed)

        updates = {"board_lenses": parsed}
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        _log.info("board lenses updated via dashboard",
                  lenses=[x.get("key") for x in parsed])
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.board_lenses_updated",
            target=", ".join(str(x.get("label")) for x in parsed)[:300],
        )
        return _flash("/dashboard/board-views", "lens_saved")

    @router.get("/relay")
    async def relay_page_moved():
        """The relay is now edited per ROLE, not per stage. Kept so old links land."""
        return RedirectResponse("/dashboard/roles", status_code=301)

    @router.get("/board/partial", response_class=HTMLResponse)
    async def board_partial(request: Request):
        """Just the columns — fetched by the page's auto-refresh, no full reload."""
        return _TEMPLATES.TemplateResponse(request, "_board_cols.html", await _board_ctx(request))

    @router.post("/board/run")
    async def board_run(request: Request):
        """Trigger the next role's stages on one item — the reviewed hand-off.

        The relay deliberately stops between roles: the previous stage leaves the
        item in an ADO state the poller ignores, so nothing runs until a person has
        read the output. This is the act of pressing Go: it stamps the process's
        SDLC profile on the item (so the machine runs THAT role's stages, not the
        default), then hands it back to the poller the same way the Planning page
        does — trigger tag on, state moved into a trigger state.
        """
        c: Container = request.app.state.container
        form = await request.form()
        try:
            item_id = int(str(form.get("item_id", "")))
        except ValueError:
            return Response(status_code=204)
        view = board_view(c.config, str(form.get("view", "")))
        if not item_id or c.config.dry_run or view.key == "pipeline":
            return Response(status_code=204)

        # The profile tag is how the SDLC engine is told which role is running. Only
        # one may stick: leaving the previous role's tag on would let the engine pick
        # whichever it saw first, which is how an item silently re-runs BA forever.
        # Releasing the brake IS the trigger. A lane that claims parking tags is
        # saying "items with these tags wait here"; pressing Run means "it may go
        # on", so the tag that stopped it comes off. The poller ignores anything
        # carrying autopilot-done / -review / -hold / -live, which is why a stage
        # that finished stays put until a person does this.
        item = await c.ado.get_work_item(item_id)
        held = {(t or "").strip().lower() for t in (item.tags if item else [])}
        for lane in view.lanes:
            claimed = {t.strip().lower(): t for t in lane.tags}
            hit = held & set(claimed)
            if hit:
                for tag in (item.tags if item else []):
                    if tag.strip().lower() in hit:
                        with contextlib.suppress(Exception):  # best-effort release
                            await c.ado.remove_tag(item_id, tag)
                break

        # Pressing Run is a person saying "this is not moving, go" — so it has to
        # release what is holding the item. A run killed with its process leaves the
        # live tag behind and never writes a result, and the poller skips that tag:
        # the card sits there, Run reports started=1, and nothing happens. Only the
        # poller knows whether a session is REALLY running, so ask it — clearing the
        # tag under a live console would dispatch a second one onto the same branch.
        live_tag = (c.config.live_tag or "").strip()
        if live_tag and item is not None:
            poller = getattr(request.app.state, "poller", None)
            running = poller.has_live_session(item_id) if poller is not None else True
            held_live = next(
                (t for t in item.tags if t.strip().lower() == live_tag.lower()), None
            )
            if held_live and not running:
                with contextlib.suppress(Exception):
                    await c.ado.remove_tag(item_id, held_live)
                _log.info("board run released a stranded live tag",
                          id=item_id, tag=held_live)
            elif held_live:
                _log.info("board run: a session is still live — nothing released",
                          id=item_id, tag=held_live)

        profile = (view.profile or "").strip()
        # Stamp the role whatever the run mode. This used to be gated on
        # sdlc_loop_enabled, which is a HEADLESS switch — so on an interactive machine
        # (the default) pressing Run on the QC board said nothing about QC, and the
        # run fell back to whatever the item's state happened to resolve to. A person
        # pressing a role's Run button IS the statement of which role is due.
        if profile:
            # One definition of the pin, shared with the poller's run-now path — and
            # released by the hand-off, so pressing Run names the role for THIS leg
            # rather than owning the item for every leg after it.
            wanted = sdlc_plan.profile_tag(profile, c.config)
            for stale in sdlc_plan.profile_pins(item.tags if item else [], c.config):
                if stale.strip().lower() != wanted.lower():
                    await c.ado.remove_tag(item_id, stale)
            await c.ado.add_tag(item_id, wanted)

        started = await planning_analyzer.start_items(c, [item_id])
        _log.info("board run", id=item_id, view=view.key, profile=profile, started=started)
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="board.run",
            target=f"#{item_id} → {view.label}" + (f" ({profile})" if profile else ""),
        )
        return Response(status_code=204)

    @router.post("/board/move")
    async def board_move(request: Request):
        """Drag & drop: apply the configured tag/state for the target column."""
        c: Container = request.app.state.container
        form = await request.form()
        try:
            item_id = int(str(form.get("item_id", "")))
        except ValueError:
            return Response(status_code=204)
        column = str(form.get("column", "")).strip().lower()
        dmap = parse_drop_map(c.config.board_drop_map)
        action = dmap.get(column)
        if not item_id or action is None or c.config.dry_run:
            return Response(status_code=204)
        kind, value = action
        if kind == "state":
            await c.ado.update_state(item_id, value)
        else:  # tag — set exclusively among the configured drop-tags
            managed = {v for (k, v) in dmap.values() if k == "tag"}
            item = await c.ado.get_work_item(item_id)
            if item is not None:
                for tag in item.tags:
                    if tag in managed and tag != value:
                        await c.ado.remove_tag(item_id, tag)
            await c.ado.add_tag(item_id, value)
        _log.info("board move", id=item_id, column=column, action=action)
        return Response(status_code=204)

    return router
