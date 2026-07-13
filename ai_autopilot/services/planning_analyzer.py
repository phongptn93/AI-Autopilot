"""Planning workbench services (impure): analyse a selection, start items, and
fire scheduled runs.

- :func:`analyze` — link-graph grouping + wave preview + (optional) AI conflict
  judges over the selected items.
- :func:`start_items` — hand a set of items to the autopilot (trigger tag + state).
  Shared by the "Start now" endpoint and the scheduled-run sweeper.
- :func:`run_due_plans` — the sweeper: Start every planned run whose time has come.
"""

from __future__ import annotations

from datetime import datetime

from ai_autopilot.execution.claude_client import run_claude
from ai_autopilot.logging_config import get_logger
from ai_autopilot.routing.conflict_ai import build_judge_prompt, parse_verdict, suspicious_pairs
from ai_autopilot.routing.planning_groups import group_by_links
from ai_autopilot.routing.scheduler import plan_schedule

_log = get_logger("services.planning")


async def analyze(container, ids: list[int]) -> dict:
    """Group the selected items by their link graph, preview the dispatch wave, and
    (if enabled) run bounded AI judges over keyword-overlapping pairs."""
    c = container
    cfg = c.config
    items = await c.ado.get_work_items_by_ids(ids)
    try:
        preds, related = await c.ado.get_work_item_links(ids)
    except Exception as exc:  # noqa: BLE001 — analysis must degrade, not crash
        _log.warning("planning analyze: link fetch failed", error=str(exc))
        preds, related = {}, {}

    groups = group_by_links(items, preds, related)
    plan = plan_schedule(items, predecessors=preds, related=related, active_ids=set(ids))
    wave = {"ready": [i.id for i in plan.ready], "deferred": plan.deferred}

    ai: list[dict] = []
    if cfg.planning_ai_analysis and len(items) >= 2:
        by_id = {i.id: i for i in items}
        pairs = suspicious_pairs(
            items,
            max_pairs=cfg.planning_ai_max_pairs,
            min_token_len=cfg.conflict_ai_min_token_len,
            extra_stopwords=cfg.conflict_ai_extra_stopwords,
        )
        for a_id, b_id, shared in pairs:
            a, b = by_id.get(a_id), by_id.get(b_id)
            if a is None or b is None:
                continue
            try:
                run = await run_claude(
                    build_judge_prompt(a, b, shared),
                    cfg.workspace_directory or ".",
                    timeout_seconds=cfg.planning_ai_timeout_seconds,
                    model=cfg.claude_model or None,
                    max_turns=1,
                    permission_mode="plan",
                )
                verdict = parse_verdict(run.text)
            except Exception as exc:  # noqa: BLE001 — one bad judge must not fail Analyze
                _log.warning("planning analyze: judge failed", a=a_id, b=b_id, error=str(exc))
                verdict = None
            if verdict and verdict["score"] >= cfg.planning_ai_min_score:
                ai.append({"a": a_id, "b": b_id, "shared": shared, **verdict})
                # Persist so the poller can feed this hidden conflict back into
                # scheduling (avoid running the pair concurrently). Best-effort.
                try:
                    await c.ai_conflict_repo.record(
                        a_id, b_id, verdict["score"],
                        modules=verdict.get("modules"), reason=verdict.get("reason", ""),
                    )
                except Exception as exc:  # noqa: BLE001 — persistence must not fail Analyze
                    _log.warning("planning analyze: conflict persist failed",
                                 a=a_id, b=b_id, error=str(exc))

    return {
        "items": items,
        "titles": {i.id: i.title for i in items},
        "groups": groups,
        "wave": wave,
        "ai": ai,
    }


async def start_items(container, ids: list[int]) -> int:
    """Hand items to the autopilot: add the trigger tag, and move an item into a
    trigger state if it isn't already in one. Returns how many were started. Honours
    ``dry_run`` (writes nothing)."""
    c = container
    cfg = c.config
    if cfg.dry_run or not ids:
        return 0
    trigger = cfg.trigger_tag
    start_state = cfg.planning_start_state or (cfg.trigger_states[0] if cfg.trigger_states else "")
    trigger_states = {s.strip().lower() for s in cfg.trigger_states}
    started = 0
    for iid in ids:
        item = await c.ado.get_work_item(iid)
        if item is None:
            continue
        if trigger and trigger.lower() not in {t.lower() for t in item.tags}:
            await c.ado.add_tag(iid, trigger)
        if start_state and (item.state or "").strip().lower() not in trigger_states:
            await c.ado.update_state(iid, start_state)
        started += 1
    return started


async def run_due_plans(container) -> int:
    """Sweeper: Start every scheduled run whose time has come. Returns items started."""
    c = container
    total = 0
    for row in await c.planned_run_repo.due(datetime.now()):  # noqa: DTZ005 — local wall-clock
        ids = c.planned_run_repo.ids_of(row)
        started = await start_items(c, ids)
        await c.planned_run_repo.set_status(row.id, "done")
        total += started
        _log.info("scheduled run fired", plan=row.id, items=len(ids), started=started)
    return total
