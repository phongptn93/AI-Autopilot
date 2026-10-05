"""Settings, roles, config view, capabilities, reset, import/export."""

from __future__ import annotations

import contextlib

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ai_autopilot import fleet as fleet_mod
from ai_autopilot import security
from ai_autopilot.config import SdlcRole, config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _TEMPLATES,
    _flash,
    _log,
    _take_flash,
    forget_scans,
)
from ai_autopilot.dashboard.routes._shared import _ctx, _setup_flow, _setup_role, _setup_source
from ai_autopilot.execution import sdlc_plan
from ai_autopilot.logging_config import describe_exc
from ai_autopilot.skills_catalog import discover_skills
from ai_autopilot.workspace import discover_repos


def _reset_display(spec, value) -> str:
    """One value as the preview shows it. A secret is never printed — only whether
    there IS one, which is the part that decides whether you care."""
    if spec is not None and spec.kind == "password":
        return "••• đã đặt" if value else "(trống)"
    if isinstance(value, bool):
        return "bật" if value else "tắt"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "(trống)"
    if isinstance(value, dict):
        return ", ".join(f"{k} => {v}" for k, v in value.items()) if value else "(trống)"
    text = str(value if value is not None else "")
    return text if text.strip() else "(trống)"


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/roles", response_class=HTMLResponse)
    async def roles_page(request: Request):
        """One row per role: the stages it runs, and its way in and out.

        Everything a role needs used to be spread over five places — the stage set in
        YAML only, the door on the stage-keyed wiring, the hand-off in Settings under
        a heading about a loop that does not gate it, the fallbacks elsewhere again.
        Nothing on screen showed that one role's way OUT is the next role's way IN,
        which is exactly the fact that, unseen, let a hand-off state be used as a
        trigger and rework finished work (#8526).
        """
        c: Container = request.app.state.container
        cfg = c.config
        try:
            states_by_type = await c.ado.get_states_by_type()
        except Exception:  # noqa: BLE001 — ADO down costs the picker, not the page
            states_by_type = {}
        known = sorted({(n or "").strip() for names in states_by_type.values() for n in names if n})

        roles = sdlc_plan.effective_roles(cfg)
        catalog = sdlc_plan.stage_catalog(cfg)
        doors = [d.lower() for r in roles.values() for d in sdlc_plan.role_doors(r)]
        # A door that is ALSO a trigger state is the one place this page silently
        # rewrites a setting made on another page: the role's own autonomy wins, so
        # leaving `auto` off REMOVES that state from the poll query. Deliberate — the
        # dial belongs next to the state it governs — but invisible until now, and an
        # operator who unticks a box does not expect the autopilot to stop picking up
        # a state that Settings still lists.
        triggers = {(t or "").strip().lower() for t in cfg.trigger_states if (t or "").strip()}
        rows = []
        for name, role in sorted(roles.items()):
            own_doors = sdlc_plan.role_doors(role)
            # What actually gets applied, not what was typed: a blank `done` still
            # falls back to resolved_state, so a chain drawn from the raw field would
            # claim the item stops dead when the runtime is about to move it.
            out_state = sdlc_plan.handoff_state(name, cfg).strip()
            rows.append({
                "name": name,
                "stages": list(role.stages),
                "waits_in": role.waits_in, "shows": role.shows, "entry_tag": role.entry_tag,
                "done": role.done, "done_tag": role.done_tag, "auto": role.auto,
                # Does it open a PR? Two facts, because the control is an override of an
                # inference and showing only one of them is what let a QC role file a PR
                # of test-case files: what the STAGES say, and whether this role overrides
                # it. Blank choice = follow the stages, which is the default.
                "derived_pr": any(
                    getattr(catalog.get(s), "produces_pr", False) for s in role.stages
                ),
                "pr_choice": (
                    "" if role.opens_pr is None else ("yes" if role.opens_pr else "no")
                ),
                # The state the runtime will really set — role.done, or the fallback.
                "effective_done": out_state,
                # What each stage is for, so picking a stage set is not guesswork.
                "goals": [
                    {"name": s, "goal": getattr(catalog.get(s), "goal", "")}
                    for s in role.stages
                ],
                # Two roles behind one door: the state cannot say which is due, so the
                # runtime refuses it. Shown on both rows rather than only in a log.
                "doors": own_doors,
                "clash": any(doors.count(d.lower()) > 1 for d in own_doors),
                # "This door is also a trigger state" — with `auto` off the state is
                # dropped from the poll query, with it on the state is added.
                "is_trigger": [d for d in own_doors if d.lower() in triggers],
                # Where the item goes next — blank is a real answer (it stops).
                "lands_on": next(
                    (n for n, r in sorted(roles.items())
                     if out_state and any(d.lower() == out_state.lower()
                                          for d in sdlc_plan.role_doors(r))),
                    "",
                ),
                "dead_end": bool(out_state) and not any(
                    d.lower() == out_state.lower()
                    for r in roles.values() for d in sdlc_plan.role_doors(r)
                ),
            })

        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request, "roles.html",
            _ctx(request, "roles", rows=rows, known_states=known, flash=flash,
                 all_stages=[
                     {"name": s.name, "role": s.role, "goal": s.goal}
                     for s in catalog.values()
                 ],
                 trigger_states=cfg.trigger_states,
                 effective_states=cfg.effective_trigger_states,
                 # What the wiring actually CHANGED. Two identical rows of chips
                 # answer "did my wiring do anything?" with a shrug; the difference
                 # is the whole reason both rows are on the page.
                 added_states=[
                     s for s in cfg.effective_trigger_states
                     if s.strip().lower() not in {t.strip().lower() for t in cfg.trigger_states}
                 ],
                 removed_states=[
                     s for s in cfg.trigger_states
                     if s.strip().lower()
                     not in {t.strip().lower() for t in cfg.effective_trigger_states}
                 ],
                 entry_tag=cfg.stage_entry_tag,
                 collisions=sdlc_plan.handoff_collisions(cfg),
                 resolved_state=cfg.resolved_state,
                 in_progress=cfg.state_in_progress),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/roles")
    async def save_roles(request: Request):
        """Save the role definitions. Every field of a row belongs to that role, so a
        row is saved whenever it says anything at all — there is no half-row to drop."""
        c: Container = request.app.state.container
        form = await request.form()
        if form.get("reset"):
            # Clearing writes an EMPTY map rather than removing the key: absent means
            # "derive from the deprecated keys", which would resurrect the very wiring
            # the operator just asked to be rid of.
            updates = {"sdlc_roles": {}}
            settings_form.save_to_yaml(config_file_path(), updates)
            settings_form.apply_to_config(c.config, updates)
            _log.info("roles cleared via dashboard")
            return _flash("/dashboard/roles", "roles_cleared")

        roles: dict[str, dict] = {}
        for key in form:
            if not key.startswith("role_") or not key.endswith("_waits"):
                continue
            name = key[len("role_"):-len("_waits")]
            roles[name] = {
                "stages": form.getlist(f"role_{name}_stages"),
                "waits_in": str(form.get(f"role_{name}_waits", "")).strip(),
                "shows": str(form.get(f"role_{name}_shows", "")).strip(),
                "entry_tag": str(form.get(f"role_{name}_tag", "")).strip(),
                "done": str(form.get(f"role_{name}_done", "")).strip(),
                "done_tag": str(form.get(f"role_{name}_done_tag", "")).strip(),
                "auto": bool(form.get(f"role_{name}_auto")),
            }
            # Tri-state, and the third state is absence: a row saved without the key
            # means "follow the stages", so it is LEFT OUT rather than written as null.
            # This save replaces `sdlc_roles` wholesale, so a field the form did not
            # round-trip was a field this page silently cleared — which is what it did
            # to `opens_pr` until it appeared here.
            pr_choice = str(form.get(f"role_{name}_pr", "")).strip()
            if pr_choice in ("yes", "no"):
                roles[name]["opens_pr"] = pr_choice == "yes"
        # Same destructive collision as the shared tag, reachable through a different
        # page: a role's run-now tag equal to a trigger tag makes the sweep strip
        # ownership off every item. Settings refuses it; this door has to as well.
        for name, row in sorted(roles.items()):
            why = settings_form.run_now_tag_conflict(row["entry_tag"], c.config)
            if why:
                _log.error(
                    "roles rejected: run-now tag collides", role=name,
                    tag=row["entry_tag"], reason=why,
                    hint="name it after the trigger tag, e.g. '<trigger-tag>-run-" + name + "'",
                )
                return _flash("/dashboard/roles", "err_role_tag_clash")

        settings_form.save_to_yaml(config_file_path(), {"sdlc_roles": roles})
        # YAML takes plain dicts; the live config must get validated objects, or the
        # very next poll reads a raw dict where a model is expected.
        settings_form.apply_to_config(c.config, {
            "sdlc_roles": {k: SdlcRole(**v) for k, v in roles.items()}
        })
        _log.info("roles updated via dashboard", roles=sorted(roles))
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.roles_updated",
            target=", ".join(
                f"{k}: {v['waits_in'] or '—'}→{v['done'] or 'stop'}"
                for k, v in sorted(roles.items()) if v["waits_in"] or v["done"]
            )[:300],
        )
        return _flash("/dashboard/roles", "roles_saved")

    @router.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        """Every live setting, read-only, generated from the SAME registry Settings edits.

        It used to be a hand-written page listing the rows somebody thought were worth
        showing, which meant it could only ever be a stale copy: measured at 61 of 180
        fields, with whole sections — alerts, PR review & feedback, the Teams bot, fleet,
        quality gates — absent entirely, because each was added to Settings and nobody
        remembered to add it here too. Deriving it from ``settings_form.FIELDS`` makes
        that drift impossible rather than merely fixed once.

        The page also answers the question people actually open it for. "What is this
        machine set to" is 180 rows nobody reads; "what does this machine do DIFFERENTLY
        from a fresh install" is usually a dozen, and it is the list you send someone
        when a machine misbehaves. So every row is marked against its default and the
        page opens on the changed ones.
        """
        c: Container = request.app.state.container
        cfg = c.config
        defaults = settings_form.model_defaults(cfg)
        secrets_set = {
            key: bool(getattr(cfg, key, "")) for key in settings_form.SECRET_KEYS
        }
        secrets_set["dashboard_auth_password"] = bool(
            getattr(cfg, "dashboard_auth_password_hash", "")
        )
        current = {
            f.key: getattr(cfg, f.key, None)
            for f in settings_form.FIELDS
            if f.key not in settings_form.SECRET_KEYS
        }

        def _display(f) -> tuple[str, str]:
            """(text, kind-hint) for one field — never the secret itself."""
            if f.kind == "password":
                return ("đã đặt", "set") if secrets_set.get(f.key) else ("chưa đặt", "unset")
            value = getattr(cfg, f.key, None)
            if isinstance(value, bool):
                return ("bật", "on") if value else ("tắt", "off")
            if isinstance(value, (list, tuple)):
                return (", ".join(str(v) for v in value), "chips") if value else ("—", "empty")
            if isinstance(value, dict):
                return (
                    (" · ".join(f"{k} → {v}" for k, v in value.items()), "chips")
                    if value else ("—", "empty")
                )
            text = "" if value is None else str(value)
            return (text, "text") if text.strip() else ("—", "empty")

        groups = []
        changed_total = 0
        for section, fields in settings_form.sections():
            rows = []
            for f in fields:
                text, hint = _display(f)
                # A secret has no readable default to compare against, so "changed"
                # means "somebody set one" — which is the only fact the page can honestly
                # report about it anyway.
                if f.kind == "password":
                    changed = bool(secrets_set.get(f.key))
                else:
                    changed = f.key in defaults and getattr(cfg, f.key, None) != defaults[f.key]
                rows.append({
                    "key": f.key, "label": f.label, "help": f.help,
                    "text": text, "hint": hint, "changed": changed,
                    # A setting that does nothing on a machine configured like this one
                    # is noise on a page whose job is to say what this machine does.
                    "applies": settings_form.applies(f, current),
                })
                changed_total += bool(changed)
            groups.append({
                "section": section, "rows": rows,
                "changed": sum(1 for r in rows if r["changed"]),
            })
        return _TEMPLATES.TemplateResponse(
            request, "config.html",
            _ctx(
                request, "config", cfg=cfg, groups=groups,
                total=sum(len(g["rows"]) for g in groups),
                changed_total=changed_total,
            ),
        )

    @router.get("/capabilities", response_class=HTMLResponse)
    async def capabilities(request: Request):
        c: Container = request.app.state.container
        skills = discover_skills(c.config.workspace_directory)
        return _TEMPLATES.TemplateResponse(
            request,
            "capabilities.html",
            _ctx(
                request,
                "capabilities",
                skills=skills,
                workspace=c.config.workspace_directory,
                ai_native=bool(c.config.workspace_directory),
            ),
        )

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        c: Container = request.app.state.container
        current = {
            f.key: getattr(c.config, f.key, "")
            for f in settings_form.FIELDS
            if f.key not in settings_form.SECRET_KEYS
        }
        has_pat = bool(getattr(c.config, "ado_pat", ""))
        secrets_set = {
            key: bool(getattr(c.config, key, ""))
            for key in settings_form.SECRET_KEYS
        }
        # dashboard_auth_password is entered raw but stored as a hash — reflect
        # "set" from the hash field, since there is no attr of the raw name.
        secrets_set["dashboard_auth_password"] = bool(
            getattr(c.config, "dashboard_auth_password_hash", "")
        )
        cfg = c.config
        # Out-of-the-box values, so "is this configured" can mean "did somebody decide
        # it" rather than "is it non-empty" — see settings_form.has_value.
        defaults = settings_form.model_defaults(cfg)
        # Which settings this machine cannot run without, and which of them are still
        # blank — both taken from the SETUP WIZARD's own step list rather than from a
        # second hand-written list here. The wizard already answers "which eight fields
        # does this role need, in what order" (_setup_flow), so deriving from it means
        # the two surfaces cannot drift: a step added there shows up here by itself.
        #
        # Why the page needs this at all: 180 fields across 23 sections is the right
        # shape for changing ONE thing and the wrong shape for the first hour. Opening
        # Settings on a fresh machine said nothing about where to start.
        by_key = {f.key: f for f in settings_form.FIELDS}
        setup_role = _setup_role(cfg)
        setup_steps = _setup_flow(setup_role, _setup_source(request, cfg))
        essential_keys = [
            key for _sid, _title, keys in setup_steps for key in keys if key in by_key
        ]
        # What "not configured yet" means, and what it does NOT mean.
        #
        # This used to ask settings_form.has_value() over every key in every wizard
        # step, which was wrong twice over. has_value answers "did somebody DECIDE this,
        # or is it sitting at its default" — it exists to hide fields that do not apply,
        # and a setting resting on a perfectly good default reads as unset. And the
        # wizard's key lists are DISPLAY groupings, not requirements: they say which
        # fields a step shows, not which a machine cannot run without.
        #
        # Between them, a fully configured central was told it had two steps left
        # because `fleet_offline_after_minutes` was 30 — the default, and the right
        # value. A setup banner that will not go away teaches people to ignore banners.
        #
        # So: a short, explicit list of what genuinely has no working default, checked
        # for being BLANK rather than for being unchanged.
        required: list[str] = ["workspace_directory"]
        if (_setup_source(request, cfg) or "ado") != "jira":
            # A Jira team configures its tracker on the workspace, not here; doctor's
            # own check_workspaces covers that, and guessing at it from root settings is
            # how this banner would start lying in the other direction.
            required += ["ado_organization", "ado_project", "ado_pat"]
        if setup_role == "worker":
            required += ["fleet_central_url", "fleet_token"]

        def _blank(key: str) -> bool:
            if key in settings_form.SECRET_KEYS:
                return not secrets_set.get(key)      # secrets never reach `current`
            value = getattr(cfg, key, None)
            if isinstance(value, (bool, int, float)):
                return False                          # a number or a switch IS an answer
            return not str(value or "").strip() if not isinstance(value, (list, dict, tuple)) \
                else not value

        setup_todo = [
            (by_key[k].label if k in by_key else k) for k in required if _blank(k)
        ]
        # Read-only overview of every ADO tag the autopilot writes/reads — so the
        # whole tag vocabulary is visible in one place (not scattered across fields).
        tag_overview = [
            {"label": "Trigger", "cls": "chip-accent",
             "tags": cfg.effective_trigger_tags, "hint": "items with these get processed"},
            {"label": "Review", "cls": "chip-amber",
             "tags": [cfg.review_tag], "hint": "draft PR opened, awaiting review"},
            {"label": "Done", "cls": "chip-green",
             "tags": [cfg.processed_tag], "hint": "handled (also report/failed unless overridden)"},
            {"label": "Needs human", "cls": "chip-red",
             "tags": [cfg.escalation_tag], "hint": "escalated & held"},
            {"label": "Live", "cls": "chip-blue",
             "tags": [cfg.live_tag], "hint": "interactive session running"},
            {"label": "Failed", "cls": "chip",
             "tags": [cfg.failed_tag or f"{cfg.processed_tag} (Done tag)"],
             "hint": "gave up after retries"},
        ]
        discovered = discover_repos(c.config.workspace_directory)
        allowed = {r.lower() for r in c.config.allowed_repos}
        try:
            ado_states = await c.ado.get_states()
        except Exception:  # noqa: BLE001 — Settings must render even if ADO is down
            ado_states = []
        # Rows for the Teams channel card — the ONE editor for Teams notifications.
        channels = [
            {"name": str(e.get("name") or ""), "url": str(e.get("url") or ""),
             "active": bool(e.get("active", True))}
            for e in (cfg.teams_webhook_channels or []) if isinstance(e, dict) and e.get("url")
        ] + [
            {"name": "", "url": str(e), "active": True}
            for e in (cfg.teams_webhook_channels or []) if isinstance(e, str) and e.strip()
        ]
        if not channels:
            # Seed from the two legacy settings so an existing setup appears in the editor
            # rather than being invisible in it. `teams_webhook_url` becomes an ordinary row
            # named "primary": a row that behaved differently from its neighbours would be
            # the same inconsistency this card was meant to remove.
            legacy_urls = [
                (name, url) for name, url in
                [("primary", cfg.teams_webhook_url), *(("", u) for u in cfg.teams_webhook_urls)]
                if (url or "").strip()
            ]
            channels = [
                {"name": name, "url": url.strip(), "active": True}
                for name, url in legacy_urls
            ]

        flash = _take_flash(request)
        # One-shot: shown once after the button was pressed, then cleared, so a stale
        # result cannot be mistaken for the state of the channels right now.
        probe = getattr(request.app.state, "notify_probe", None)
        request.app.state.notify_probe = None
        # Delivery log + what quiet hours is holding: "did the card go out?" answered on
        # the page, not in the log of the machine that sent it.
        notify_log, notify_held = [], None
        c_ = request.app.state.container
        with contextlib.suppress(Exception):
            notify_log = await c_.notification_log_repo.recent(30)
        with contextlib.suppress(Exception):
            notify_held = await c_.notification_hold_repo.count()
        response = _TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                "settings",
                sections=settings_form.sections(),
                current=current,
                has_pat=has_pat,
                secrets_set=secrets_set,
                # Which fields do anything on THIS machine, and which of the rest are
                # nonetheless configured. The page hides an inapplicable field that is
                # empty and dims one that is set — computed here so the first paint
                # already agrees with what the browser re-computes on every change.
                applicable={
                    f.key: settings_form.applies(f, current) for f in settings_form.FIELDS
                },
                filled={
                    f.key: settings_form.has_value(f, current, secrets_set, defaults)
                    for f in settings_form.FIELDS
                },
                # "chỉ khi «Vai của máy này» = worker" beats "chỉ dùng khi fleet role
                # = worker": the badge names the control the reader can actually see.
                field_labels={f.key: f.label for f in settings_form.FIELDS},
                # Who decides each setting HERE. On a standalone or central machine this
                # is "machine" for everything and the page looks exactly as it did.
                owners={
                    f.key: settings_form.owner_of(f.key, cfg) for f in settings_form.FIELDS
                },
                # The split, stated once at the top. On a worker most of this page is put
                # away behind per-section toggles — measured at 131 of 180 — and until the
                # page says so, opening Settings on a worker looks like a page that lost
                # its content. One sentence turns "where did everything go" into "this is
                # the part this machine decides".
                owned_here=sum(
                    1 for f in settings_form.FIELDS
                    if settings_form.owner_of(f.key, cfg) != settings_form.OWNER_CENTRAL
                ),
                owned_central=sum(
                    1 for f in settings_form.FIELDS
                    if settings_form.owner_of(f.key, cfg) == settings_form.OWNER_CENTRAL
                ),
                essential_keys=essential_keys,
                setup_todo=setup_todo,
                changed_keys=[
                    f.key for f in settings_form.FIELDS
                    if f.key in defaults and getattr(cfg, f.key, None) != defaults[f.key]
                ],
                is_worker=(cfg.fleet_role or "") == fleet_mod.ROLE_WORKER,
                restart_keys=settings_form.RESTART_REQUIRED,
                flash=flash,
                webhook_channels=channels,
                notify_probe=probe,
                notify_log=notify_log, notify_held=notify_held,
                # Named on the ADO section so "where do I put Jira?" is answered on the
                # page people look at first, not only on the one that owns the setting.
                jira_workspaces=[
                    (ws.name or (ws.ado_projects or ["(chưa đặt tên)"])[0])
                    for ws in (cfg.workspaces or [])
                    if (getattr(ws, "provider", "") or "").strip().lower() == "jira"
                ],
                # What the fleet block states about THIS machine. Counted from the real
                # export filter rather than written out by hand, so the number cannot
                # drift from what the central actually serves.
                fleet={
                    "role": cfg.fleet_role or "",
                    "shared_count": len(settings_form.fleet_settings(cfg)),
                    "interval": cfg.fleet_sync_interval_minutes,
                    "local_keys": [str(k) for k in (cfg.fleet_local_keys or []) if str(k).strip()],
                },
                webhook_active_count=len(cfg.teams_webhook_targets),
                muted_channels=cfg.muted_teams_channels,
                config_path=str(config_file_path()),
                # Drives the full-export panel: without it the download is refused, so the
                # UI says why up front instead of after a click that produces nothing.
                has_export_password=bool(c.config.config_export_password),
                repos=discovered,
                allowed_repos=allowed,
                ado_states=ado_states,
                tag_overview=tag_overview,
            ),
        )
        if flash is not None:
            # One-shot: clear it now so a refresh shows the page without the banner.
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/settings")
    async def save_settings(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        updates = settings_form.parse_form(form)
        updates["allowed_repos"] = settings_form.parse_repos(form)
        updates["teams_webhook_channels"] = settings_form.parse_webhook_channels(form)
        # The card is seeded from both legacy settings, so once it has been saved they have
        # been absorbed — clearing them stops the same channel existing in two places.
        # Delivery would be unaffected either way (targets dedup by URL), but editing would
        # not: removing a row would not remove the channel.
        if updates["teams_webhook_channels"]:
            if c.config.teams_webhook_urls:
                updates["teams_webhook_urls"] = []
            if c.config.teams_webhook_url:
                updates["teams_webhook_url"] = ""

        # The dashboard password is entered raw but stored ONLY as a PBKDF2 hash.
        # Pop the raw value so it never reaches config.yaml or the live config.
        raw_password = updates.pop("dashboard_auth_password", None)
        if raw_password:
            updates["dashboard_auth_password_hash"] = security.hash_password(raw_password)

        # Refuse the whole save rather than dropping the one bad field: a partial save
        # is how you end up believing a setting took. The destructive case earns it —
        # a run-now tag equal to the trigger tag strips ownership off the entire board
        # on the next sweep, and no amount of waiting undoes that.
        if "stage_entry_tag" in updates:
            why = settings_form.run_now_tag_conflict(updates["stage_entry_tag"], c.config)
            if why:
                _log.error(
                    "settings rejected: run-now tag collides", reason=why,
                    tag=updates["stage_entry_tag"],
                    hint="name it after the trigger tag, e.g. '<trigger-tag>-run'",
                )
                return _flash("/dashboard/settings", "err_run_tag_clash")

        # A worker may not write what the central serves. Not a UI nicety: the next
        # heartbeat puts the central's value back, so accepting the write means showing
        # a green "đã lưu" for a change that disappears within the sync interval —
        # which is worse than refusing it. Enforced here rather than only in the
        # template because a disabled input is a suggestion, and the same POST can
        # arrive from curl.
        #
        # Counted, not listed: `parse_form` emits a value for EVERY field on the page,
        # so on a worker this is ~105 keys on every save no matter what was touched.
        # Logging the names would bury the one line that matters under a paragraph
        # nobody reads, and it would describe form shape rather than anyone's intent.
        refused = [k for k in updates if not settings_form.writable_here(k, c.config)]
        for key in refused:
            updates.pop(key, None)
        if refused:
            _log.debug("settings: central-managed keys skipped on this worker",
                       count=len(refused))

        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()  # re-read org URL if it changed
        forget_scans()   # cached scans describe the OLD config
        _log.info("settings updated via dashboard", keys=sorted(updates.keys()))
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.updated",
            target=", ".join(sorted(updates.keys()))[:300],
        )

        return _flash("/dashboard/settings", "saved")

    @router.get("/settings/reset", response_class=HTMLResponse)
    async def reset_preview(request: Request):
        """Show exactly what a reset would change, before anything is touched."""
        c: Container = request.app.state.container
        section = (request.query_params.get("section") or "").strip()
        plan = settings_form.reset_plan(c.config, section)
        by_key = {f.key: f for f in settings_form.FIELDS}
        rows = [
            {
                "key": key,
                "label": by_key[key].label if key in by_key else key,
                "section": by_key[key].section if key in by_key else "",
                "now": _reset_display(by_key.get(key), getattr(c.config, key, None)),
                "after": _reset_display(by_key.get(key), value),
            }
            for key, value in plan.items()
        ]
        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request, "settings_reset.html",
            _ctx(request, "settings", rows=rows, section=section, flash=flash,
                 sections=[s for s, _ in settings_form.sections()],
                 kept=sorted(settings_form.RESET_KEEP)),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/settings/reset")
    async def reset_settings(request: Request):
        """Apply the reset the preview just described."""
        c: Container = request.app.state.container
        form = await request.form()
        section = str(form.get("section", "")).strip()
        plan = settings_form.reset_plan(c.config, section)
        # Recomputed here rather than carried through the form: what the page listed a
        # minute ago is a description, and the only thing safe to apply is what is
        # different NOW. A worker also cannot reset what the central will hand straight
        # back — that would be a reset the next heartbeat undoes.
        plan = {k: v for k, v in plan.items() if settings_form.writable_here(k, c.config)}
        if not plan:
            return _flash("/dashboard/settings/reset", "reset_nothing")
        settings_form.save_to_yaml(config_file_path(), plan)
        settings_form.apply_to_config(c.config, plan)
        with contextlib.suppress(Exception):
            c.ado.refresh()
            forget_scans()   # cached scans describe the OLD config
        _log.warning("settings reset via dashboard", count=len(plan),
                     section=section or "(all)")
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.reset",
            target=section or "(toàn bộ)",
            detail=f"{len(plan)} thiết lập: " + ", ".join(sorted(plan))[:260],
        )
        return _flash("/dashboard/settings", "reset_done")

    @router.post("/settings/claim")
    async def claim_setting(request: Request):
        """Take a centrally-served setting over on THIS machine, or hand it back.

        ``fleet_local_keys`` has always been able to do this, by typing an exact
        Settings key into a textarea three sections away from the field it protects —
        so in practice nobody did it until a sync had already overwritten something.
        The decision belongs at the field, at the moment you want to change it, which
        is the only moment you know you want it.
        """
        c: Container = request.app.state.container
        cfg = c.config
        if (cfg.fleet_role or "") != fleet_mod.ROLE_WORKER:
            raise HTTPException(status_code=404, detail="only a worker claims settings")
        form = await request.form()
        key = str(form.get("key", "")).strip()
        release = bool(form.get("release"))
        if key not in {f.key for f in settings_form.FIELDS}:
            raise HTTPException(status_code=422, detail="unknown setting")
        keys = [str(k).strip() for k in (cfg.fleet_local_keys or []) if str(k).strip()]
        if release:
            keys = [k for k in keys if k != key]
        elif key not in keys:
            keys.append(key)
        settings_form.save_to_yaml(config_file_path(), {"fleet_local_keys": keys})
        settings_form.apply_to_config(cfg, {"fleet_local_keys": keys})
        await c.audit_repo.record(
            actor="dashboard", source="dashboard",
            action="fleet.setting_released" if release else "fleet.setting_claimed",
            target=key,
        )
        return _flash("/dashboard/settings",
                      "setting_released" if release else "setting_claimed")

    @router.get("/settings/reveal/{key}")
    async def reveal_secret(request: Request, key: str):
        """Hand back a stored secret so the 👁 button can actually show it.

        Only for fields that declare ``reveal`` — today that is the fleet token alone.
        A shared secret exists to be copied onto every other machine, so "type it again
        or rotate it across the fleet" is the wrong answer to "what is it". Everything
        else (the PAT, SMTP, the dashboard password) stays write-only: those are typed
        in once and nobody needs them back, so there is no reason to build a way out.

        Fetched on demand rather than rendered into the page, so the value is not
        sitting in the HTML of a tab left open, and each look is one audited event.
        """
        c: Container = request.app.state.container
        spec = next(
            (f for f in settings_form.FIELDS if f.key == key and f.reveal), None
        )
        if spec is None:
            raise HTTPException(status_code=404, detail="not revealable")
        value = str(getattr(c.config, key, "") or "")
        with contextlib.suppress(Exception):
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="settings.secret_revealed",
                target=key, detail="" if value else "(chưa đặt)",
            )
        return JSONResponse({"key": key, "value": value})

    @router.post("/settings/reload")
    async def reload_settings(request: Request):
        """Re-read config.yaml from disk into the running app (no restart)."""
        c: Container = request.app.state.container
        changed = settings_form.reload_from_file(c.config)
        c.ado.refresh()  # re-read org URL if it changed
        forget_scans()   # cached scans describe the OLD config
        _log.info("config reloaded from file via dashboard", changed=changed)
        return _flash("/dashboard/settings", "reloaded")

    @router.post("/settings/test-notification")
    async def test_notification(request: Request):
        """Send a probe card to every chat channel and show what each one did.

        The question "why did the notifications stop" had no answer short of reading
        the log of the machine the autopilot runs on — which an operator often cannot
        reach — because every failure mode is silent by design: a revoked Workflows URL
        is a log line, a switched-off event is a log line, quiet hours is a log line.
        """
        c: Container = request.app.state.container
        result = await c.notifier.send_test()
        # Held in app state rather than a flash cookie: the answer is a table, and the
        # flash mechanism carries a fixed message code by design.
        request.app.state.notify_probe = result
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="notification.tested",
            detail=f"{sum(1 for ch in result['channels'] if ch['ok'])} of "
                   f"{len(result['channels'])} channel(s) accepted",
        )
        return RedirectResponse("/dashboard/settings", status_code=303)

    @router.get("/settings/export")
    async def export_config(request: Request):
        """Download the current config as YAML, minus secrets + machine-specific keys."""
        c: Container = request.app.state.container
        body = settings_form.export_yaml(c.config)
        _log.info("config exported via dashboard")
        return Response(
            content=body,
            media_type="application/x-yaml",
            headers={"Content-Disposition": 'attachment; filename="autopilot-config.yaml"'},
        )

    @router.get("/settings/export-full")
    async def export_config_full(request: Request):
        """Download the FULL config (INCLUDING secrets), encrypted with the
        configured full-export password. Decrypt with ai_autopilot.security."""
        c: Container = request.app.state.container
        if not c.config.config_export_password:
            # Refusing is the only safe answer. Encrypting under "" still produces a
            # valid-looking .enc file, but the key derives from an empty password that
            # anyone can reproduce — so the download would carry the ADO PAT and every
            # token with no real protection, while looking protected.
            _log.warning("full export refused — config_export_password is not set")
            return _flash("/dashboard/settings", "err_no_export_password")
        blob = settings_form.export_full_encrypted(c.config, c.config.config_export_password)
        # Audit only the event — never the secret payload or the password.
        _log.warning("FULL config (with secrets) exported via dashboard — encrypted download")
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.exported_full",
            detail="encrypted download incl. secrets",
        )
        return Response(
            content=blob,
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="autopilot-config-full.enc"'},
        )

    @router.post("/settings/import-full")
    async def import_config_full(request: Request):
        """Restore a FULL encrypted config (.enc from export-full), INCLUDING secrets.

        The password is entered on the form (a restore often lands on a fresh host
        whose own config_export_password differs from the source). Simple fields
        (incl. secrets) apply live and are persisted to config.yaml; nested structures
        (tenants, repos) are fully typed after a restart — same as the shareable
        import."""
        c: Container = request.app.state.container
        form = await request.form()
        upload = form.get("file")
        password = str(form.get("password", ""))
        if upload is None or not hasattr(upload, "read"):
            return _flash("/dashboard/settings", "err_no_file")
        if not password:
            return _flash("/dashboard/settings", "err_password")
        blob = await upload.read()
        try:
            updates = settings_form.import_full_settings(
                blob, password, set(type(c.config).model_fields)
            )
        except (ValueError, yaml.YAMLError) as exc:
            _log.warning("full config import failed", error=describe_exc(exc))
            return _flash("/dashboard/settings", "err_wrong_password")
        if not updates:
            return _flash("/dashboard/settings", "err_nothing")
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()
        forget_scans()   # cached scans describe the OLD config
        _log.warning("FULL config restored via dashboard", keys=sorted(updates.keys()))
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.imported_full",
            detail=f"{len(updates)} keys restored (incl. secrets)",
        )
        return _flash("/dashboard/settings", "imported_full")

    @router.post("/settings/import")
    async def import_config(request: Request):
        """Apply an uploaded YAML config (shared by a teammate). PAT is never imported."""
        c: Container = request.app.state.container
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return _flash("/dashboard/settings", "err_no_file")
        raw = (await upload.read()).decode("utf-8", errors="replace")
        try:
            updates = settings_form.import_settings(raw, set(type(c.config).model_fields))
        except (ValueError, yaml.YAMLError) as exc:
            _log.warning("config import failed", error=describe_exc(exc))
            return _flash("/dashboard/settings", "err_invalid")
        if not updates:
            return _flash("/dashboard/settings", "err_nothing")
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()
        forget_scans()   # cached scans describe the OLD config
        _log.info("config imported via dashboard", keys=sorted(updates.keys()))
        return _flash("/dashboard/settings", "imported")

    return router
