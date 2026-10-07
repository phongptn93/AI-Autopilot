"""The ADO state-flow editor."""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import flows as flows_mod
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.dashboard.common import (
    _FLASH_COOKIE,
    _FLOW_ERROR_COOKIE,
    _TEMPLATES,
    _flash,
    _flow_reject,
    _log,
    _take_flash,
    _take_flow_reject,
)
from ai_autopilot.dashboard.routes._shared import _ctx

# Roll-up ranks a child by how far through the workflow its state sits, and the
# editor's row order is what the engine reads back as that ranking. Sorting the rows
# alphabetically therefore made the ALPHABET the workflow: on a real board "Deferred"
# came last and so counted as the most advanced state a child could reach, and
# "Awaiting Clarification" outranked "Approved". Nobody decided that; `sorted()` did.
_CAT_ORDER = {"proposed": 0, "inprogress": 1, "resolved": 2, "completed": 3,
              "removed": 4}

# Section headings of the editor, in the dashboard's language. Mapped here rather than
# in ``flows.STAGE_GROUPS`` because that tuple is engine data other code reads; only this
# page shows the words. An unknown heading falls through unchanged.
_GROUP_TITLES = {
    "While working": "Trong lúc làm",
    "Outcome": "Kết quả",
    "After the PR lands": "Sau khi PR vào",
}


def _by_workflow(states: set[str], categories: dict[str, str],
                 states_by_type: dict[str, list[str]]) -> list[str]:
    """Child states in workflow order: state category first, then the board order the
    process template itself defines, and only then the name.

    Falls back to alphabetical when ADO cannot be reached — same as before, but now
    that is the degraded path rather than the design.
    """
    # Board position of each state, from the first type that defines it.
    board: dict[str, int] = {}
    for names in states_by_type.values():
        for index, name in enumerate(names):
            board.setdefault(name, index)

    def key(state: str) -> tuple[int, int, str]:
        cat = (categories.get(state, "") or "").strip().lower()
        # Unknown category sorts with Proposed rather than last: a state we cannot
        # classify is more likely early work than finished work, and guessing
        # "finished" is the guess that lets a parent close too soon.
        return (_CAT_ORDER.get(cat, 0), board.get(state, 9_999), state)

    return sorted(states, key=key)


async def _flow_context(request: Request, flows: list | None = None) -> dict:
    """Everything flow.html renders, built from the project's REAL types + states.

    ``flows`` overrides what is shown, so a rejected POST re-renders the values the
    operator just typed instead of throwing their work away.
    """
    c: Container = request.app.state.container
    cfg = c.config
    try:
        states_by_type = await c.ado.get_states_by_type()
    except Exception:  # noqa: BLE001 — the page must render with ADO down
        states_by_type = {}
    # Roll-up rows are ordered by these, not alphabetically. Reading the rows IS
    # reading the progression, so a table sorted by first letter taught the wrong
    # order to whoever was editing it — and the engine read the same order back.
    state_categories: dict[str, str] = {}
    with contextlib.suppress(Exception):
        state_categories = await c.ado.get_state_categories()
    current = flows if flows is not None else list(cfg.work_item_flows or [])
    groups = [f for f in current if isinstance(f, dict)]
    # Which flow (if any) already claims each type, so a chip can say who holds it
    # rather than letting the operator create the ambiguity validation then rejects.
    claimed_by: dict[str, str] = {}
    for group in groups:
        for type_name in (group.get("types") or []):
            claimed_by.setdefault(str(type_name).strip().lower(),
                                  str(group.get("name") or ""))

    # Per rendered slot (existing groups + one blank), computed HERE rather than in the
    # template: Jinja's `{% set %}` doesn't survive a loop iteration, so intersecting
    # state lists in the markup silently produced the wrong answer.
    by_lower = {t.lower(): t for t in states_by_type}
    choices: list[list[str]] = []
    child_states: list[list[str]] = []
    rollup_rows: list[list[dict]] = []
    for group in [*groups, {}]:
        names = [str(t).strip() for t in (group.get("types") or [])]
        resolved = [by_lower[n.lower()] for n in names if n.lower() in by_lower]
        common: set[str] | None = None
        for type_name in resolved:
            states = set(states_by_type[type_name])
            common = states if common is None else (common & states)
        # Board order of the first ticked type, so the dropdown reads like the board.
        order = states_by_type.get(resolved[0], []) if resolved else []
        picked = common or set()
        choices.append([s for s in order if s in picked])
        # A parent's children are of OTHER types, so a roll-up line keys on their states.
        # Every project type would mean 17 rows here, 10 of them from Test Plan / Shared
        # Steps / Code Review — types that are never a child of anything the autopilot
        # runs. Since a roll-up is HELD until every listed state is mapped, showing those
        # implies they must be mapped, which is both noise and wrong. So the rows the
        # editor leads with are the states of types the autopilot actually manages (the
        # ones in some other flow group); the rest stay reachable behind a toggle.
        others = {t for f in groups for t in (f.get("types") or [])} - set(resolved)
        likely = _by_workflow({
            s for t, st in states_by_type.items() if t in others for s in st
        }, state_categories, states_by_type)
        everything = _by_workflow({
            s for t, st in states_by_type.items() if t not in resolved for s in st
        }, state_categories, states_by_type)
        if not likely:      # only one group configured — nothing to narrow to yet
            likely, everything = everything, []
        child_states.append(likely)

        mapped = dict(
            flows_mod.parse_rollup_entry(line) for line in (group.get("rollup") or [])
        )
        rows = [
            {"child": k, "parent": mapped.get(k, ""), "unknown": False, "secondary": False}
            for k in likely
        ]
        # A mapped state the project no longer has is kept and flagged rather than
        # silently dropped on the next save — that is how the "Ready for Testing" typo
        # survived unnoticed in the first place.
        rows += [
            {"child": k, "parent": v, "unknown": True, "secondary": False}
            for k, v in mapped.items() if k not in likely and k not in everything
        ]
        rows += [
            {"child": k, "parent": mapped.get(k, ""), "unknown": False, "secondary": True}
            for k in everything if k not in likely
        ]
        rollup_rows.append(rows)

    every_state = {s for st in states_by_type.values() for s in st}
    return {
        "types": sorted(states_by_type),
        "groups": groups,
        "claimed_by": claimed_by,
        "choices": choices,
        "child_states": child_states,
        "rollup_rows": rollup_rows,
        "stages": flows_mod.STAGES,
        "stage_groups": [(_GROUP_TITLES.get(title, title), keys)
                         for title, keys in flows_mod.STAGE_GROUPS],
        "stage_labels": flows_mod.STAGE_LABELS,
        "uncovered": flows_mod.uncovered_types(groups, states_by_type),
        "enabled": cfg.auto_transition_enabled,
        "assignee": cfg.auto_transition_assignee,
        "legacy": {
            stage: getattr(cfg, legacy, "") for stage, _, legacy in flows_mod.STAGES
        },
        # Flat states that exist on NO type always fail — the same class of dead
        # config as the roll-up typo, so it gets called out instead of just listed.
        "legacy_bad": (legacy_bad := {
            stage: bool(states_by_type and getattr(cfg, legacy, "")
                        and getattr(cfg, legacy) not in every_state)
            for stage, _, legacy in flows_mod.STAGES
        }),
        # A plain flag rather than `legacy_bad.values()|select|list` in the template —
        # that filter chain does work, it is just harder to read than `any()`.
        "legacy_has_dead": any(legacy_bad.values()),
        "legacy_rollup": list(cfg.parent_rollup_map or []),
        "ado_down": not states_by_type,
    }


def create_router() -> APIRouter:
    router = APIRouter(tags=["dashboard"])

    @router.get("/flow", response_class=HTMLResponse)
    async def flow_page(request: Request):
        flash = _take_flash(request)
        rejected = _take_flow_reject(request)
        response = _TEMPLATES.TemplateResponse(
            request, "flow.html",
            _ctx(request, "flow", flash=flash, errors=rejected.get("errors") or [],
                 **await _flow_context(request, rejected.get("flows"))),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        if rejected:
            response.delete_cookie(_FLOW_ERROR_COOKIE, path="/dashboard")
        return response

    @router.post("/flow")
    async def save_flow(request: Request):
        """Save the per-type flows — refusing anything ADO would reject.

        Validation blocks the save rather than warning, because that is exactly how the
        original bug survived: a state that existed on no work-item type sat in the
        config for months, failing silently on every item it touched.
        """
        c: Container = request.app.state.container
        form = await request.form()
        try:
            states_by_type = await c.ado.get_states_by_type()
        except Exception:  # noqa: BLE001
            states_by_type = {}
        parsed = flows_mod.parse_flow_form(form, sorted(states_by_type))
        errors = flows_mod.validate_flows(parsed, states_by_type)
        if errors:
            _log.info("flow config rejected", count=len(errors))
            return _flow_reject(errors, parsed)

        updates = {"work_item_flows": parsed}
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        _log.info("work-item flows updated via dashboard",
                  groups=[f.get("name") for f in parsed])
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.flows_updated",
            target=", ".join(str(f.get("name")) for f in parsed)[:300],
        )
        return _flash("/dashboard/flow", "flow_saved")

    return router
