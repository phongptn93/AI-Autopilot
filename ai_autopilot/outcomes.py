"""The single, configurable source of truth for the ADO tag + state of each pipeline
outcome. Kept in its own leaf module (importing only other leaves) so both the poller
and the PR babysitter can apply outcomes without a circular import."""

from __future__ import annotations

from ai_autopilot.flows import resolve_state, resolve_tag


def outcome_policy(cfg: object, outcome: str, work_item_type: str = "") -> tuple[str, str]:
    """Return ``(tag_to_add, ado_state_to_set)`` for a pipeline ``outcome`` — either may be
    blank (= skip). Edit the underlying fields in Settings ("Outcomes → tag + state"),
    or per work-item type at /dashboard/flow.

    ``work_item_type`` selects the matching flow, because an ADO state belongs to a type:
    a single project-wide state for an outcome is rejected outright for every type that
    doesn't define it. Blank (caller has no item in hand) → the flat settings, which is
    also what happens when no flow claims the type.

    Outcomes:
      in_progress  – the autopilot starts working the item
      review       – a draft PR opened, awaiting human review
      done         – completed with a (real) PR
      report       – a plan was commented, no code change (report mode)
      needs_human  – the agent escalated and held the item
      failed       – gave up after exhausting retries
    """
    default_tag = {
        "in_progress": "",
        "review": cfg.review_tag,
        "done": cfg.processed_tag,
        "report": cfg.processed_tag,
        "needs_human": cfg.escalation_tag,
        "failed": cfg.failed_tag or cfg.processed_tag,
    }.get(outcome)
    if default_tag is None:
        return ("", "")
    return (
        resolve_tag(cfg, outcome, work_item_type, default_tag),
        resolve_state(cfg, outcome, work_item_type),
    )


def all_outcome_tags(cfg: object) -> set[str]:
    """Every tag the outcome policy can apply — these are MUTUALLY EXCLUSIVE (an item is in
    exactly one outcome), so applying one clears the others.

    Per-flow tag overrides are included: without them a flow's custom ``done`` tag would
    never be in the clear-set, so moving that item back to review would leave the stale
    "done" tag sitting on the board next to the review one.
    """
    tags = {cfg.processed_tag, cfg.review_tag, cfg.escalation_tag, cfg.failed_tag}
    for flow in (getattr(cfg, "work_item_flows", None) or []):
        if isinstance(flow, dict) and isinstance(flow.get("tags"), dict):
            tags.update(str(v) for v in flow["tags"].values())
    return {t for t in tags if t}


async def apply_outcome(
    ado: object, cfg: object, work_item_id: int, outcome: str, work_item_type: str = ""
) -> None:
    """Apply an outcome's tag + ADO state to a work item. Shared by the poller and the PR
    babysitter. Blank tag/state or ``dry_run`` → skipped for that part.

    ``work_item_type`` picks the per-type flow (see ``outcome_policy``); blank means the
    flat settings apply.

    When the outcome HAS a tag, the OTHER outcome tags are cleared first so the board never
    shows a stale one (e.g. ``autopilot-done`` left on after moving back to review). A
    TAGLESS outcome (e.g. ``in_progress``) does NOT clear tags — otherwise it would strip
    the item's skip tag and the main poll would re-grab and reprocess it, opening duplicate
    PRs."""
    if cfg.dry_run:
        return
    tag, state = outcome_policy(cfg, outcome, work_item_type)
    if tag:
        for other in all_outcome_tags(cfg) - {tag}:
            await ado.remove_tag(work_item_id, other)
        await ado.add_tag(work_item_id, tag)
    if state:
        await ado.update_state(work_item_id, state)
