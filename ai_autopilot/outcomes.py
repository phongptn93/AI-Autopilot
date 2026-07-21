"""The single, configurable source of truth for the ADO tag + state of each pipeline
outcome. Kept in its own leaf module (no package imports) so both the poller and the PR
babysitter can apply outcomes without a circular import."""

from __future__ import annotations


def outcome_policy(cfg: object, outcome: str) -> tuple[str, str]:
    """Return ``(tag_to_add, ado_state_to_set)`` for a pipeline ``outcome`` — either may be
    blank (= skip). Edit the underlying fields in Settings ("Outcomes → tag + state").

    Outcomes:
      in_progress  – the autopilot starts working the item
      review       – a draft PR opened, awaiting human review
      done         – completed with a (real) PR
      report       – a plan was commented, no code change (report mode)
      needs_human  – the agent escalated and held the item
      failed       – gave up after exhausting retries
    """
    return {
        "in_progress": ("", cfg.state_in_progress),
        "review": (cfg.review_tag, cfg.state_in_review),
        "done": (cfg.processed_tag, cfg.resolved_state),
        "report": (cfg.processed_tag, cfg.state_report),
        "needs_human": (cfg.escalation_tag, cfg.state_needs_human),
        "failed": (cfg.failed_tag or cfg.processed_tag, cfg.state_failed),
    }.get(outcome, ("", ""))


def all_outcome_tags(cfg: object) -> set[str]:
    """Every tag the outcome policy can apply — these are MUTUALLY EXCLUSIVE (an item is in
    exactly one outcome), so applying one clears the others."""
    return {t for t in (
        cfg.processed_tag, cfg.review_tag, cfg.escalation_tag, cfg.failed_tag,
    ) if t}


async def apply_outcome(ado: object, cfg: object, work_item_id: int, outcome: str) -> None:
    """Apply an outcome's tag + ADO state to a work item. Shared by the poller and the PR
    babysitter. Blank tag/state or ``dry_run`` → skipped for that part.

    When the outcome HAS a tag, the OTHER outcome tags are cleared first so the board never
    shows a stale one (e.g. ``autopilot-done`` left on after moving back to review). A
    TAGLESS outcome (e.g. ``in_progress``) does NOT clear tags — otherwise it would strip
    the item's skip tag and the main poll would re-grab and reprocess it, opening duplicate
    PRs."""
    if cfg.dry_run:
        return
    tag, state = outcome_policy(cfg, outcome)
    if tag:
        for other in all_outcome_tags(cfg) - {tag}:
            await ado.remove_tag(work_item_id, other)
        await ado.add_tag(work_item_id, tag)
    if state:
        await ado.update_state(work_item_id, state)
