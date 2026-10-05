"""Test cases that wait for a deploy, and the hand-off to QC once it happens.

A dev run can only execute what its own environment can reach. A case like "verify
against the tenant DB after enabling the setting" needs an environment that runs THIS
build — which does not exist until the change is deployed. Reporting such a case as
"could not run" (``blocked``) was wrong twice over: it read as a partial failure of a
run whose every runnable case passed, and it held the item as "no verdict" for a step
that cannot happen before the deploy anyway. Handing it to QC right away was wrong too:
QC would test the old build and report a failure that is not one.

So the case is recorded as waiting (``pending_deploy``) and handed to QC at the moment
it becomes possible — when the item enters a state that says the build is out:

* its flow's deploy state (``on_deploy_state``, set by the deploy monitor or a person);
* any of the testing hand-off states (``board_testing_state``).

One rule covers the pipeline-driven deploy, a manual one, and an item dragged straight
to Testing, because it reads the item's CURRENT state each cycle instead of hooking one
transition. An item that reaches Done without passing through either gets a warning
instead — a change nobody verified on a real environment must not close in silence.
"""

from __future__ import annotations

from ai_autopilot import test_report
from ai_autopilot.board import handoff_states
from ai_autopilot.execution.result_contract import CaseOutcome
from ai_autopilot.flows import resolve_state, stage_configured
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.models import WorkItemInfo

_log = get_logger("services.deferred_verification")

HANDOFF = "handoff"
UNVERIFIED = "unverified"


def release_states(cfg, work_item_type: str = "") -> set[str]:
    """Lower-cased states that mean "this build is on an environment QC can test"."""
    states = set(handoff_states(getattr(cfg, "board_testing_state", None)))
    deployed = resolve_state(cfg, "on_deploy", work_item_type)
    if deployed:
        states.add(deployed.strip().lower())
    return states


def decide(cfg, item: WorkItemInfo) -> str | None:
    """What a pending item's current state calls for: hand off, warn, or wait."""
    state = (item.state or "").strip().lower()
    if not state:
        return None
    if state in release_states(cfg, item.work_item_type):
        return HANDOFF
    if state in {s.strip().lower() for s in (getattr(cfg, "done_states", None) or [])}:
        return UNVERIFIED
    return None


def has_trigger(cfg) -> bool:
    """Whether any state at all can release a deferred case (for any item type)."""
    testing = handoff_states(getattr(cfg, "board_testing_state", None))
    return bool(testing) or stage_configured(cfg, "on_deploy")


class DeferredVerificationService:
    """Record deferred cases from a run; hand them off when the item is deployed."""

    def __init__(self, repo, config, provider_for) -> None:
        self._repo = repo
        self._config = config
        self._provider_for = provider_for   # project -> tracker client

    async def record(self, item: WorkItemInfo, results: list) -> int:
        """Persist this run's ``pending_deploy`` cases; returns how many are new."""
        cases = [r for r in results or [] if getattr(r, "outcome", "") == test_report.PENDING]
        if not cases or self._repo is None:
            return 0
        try:
            added = await self._repo.add(item, cases)
        except Exception as exc:  # noqa: BLE001 — bookkeeping must not sink the run
            _log.warning("deferred cases not recorded", id=item.id, error=describe_exc(exc))
            return 0
        _log.info("cases deferred until deploy", id=item.id, cases=len(cases), new=added)
        return added

    async def reconcile(self) -> int:
        """Hand off / warn for every pending item whose state now calls for it.

        Returns how many items were released. One batched read per project per cycle,
        and nothing at all while no case is waiting — the steady state costs a single
        indexed query.
        """
        if self._repo is None:
            return 0
        try:
            pending = await self._repo.pending_items()
        except Exception as exc:  # noqa: BLE001 — a DB blip must not stop the poll cycle
            _log.warning("deferred cases unreadable", error=describe_exc(exc))
            return 0
        by_project: dict[str, list[int]] = {}
        for item_id, project in pending.items():
            by_project.setdefault(project, []).append(item_id)

        released = 0
        for project, ids in by_project.items():
            try:
                items = await self._provider_for(project).get_work_items_by_ids(ids)
            except Exception as exc:  # noqa: BLE001 — try again next cycle
                _log.warning("deferred items unreadable", project=project, ids=ids,
                             error=describe_exc(exc))
                continue
            for item in items:
                if await self._release(item, project):
                    released += 1
        return released

    async def _release(self, item: WorkItemInfo, project: str) -> bool:
        cfg = self._config.scoped_for_project(item.project or project)
        verdict = decide(cfg, item)
        if verdict is None:
            return False
        rows = await self._repo.pending_for(item.id)
        if not rows:
            return False
        # The renderer speaks CaseOutcome, the run's own shape — not the table's.
        cases = [CaseOutcome(title=r.case_title, outcome=test_report.PENDING, note=r.note)
                 for r in rows]
        if verdict == HANDOFF:
            body = test_report.render_handoff(cases, state=item.state)
            status = self._repo.HANDED_OFF
        else:
            body = test_report.render_unverified(cases, state=item.state)
            status = self._repo.UNVERIFIED
        if self._config.dry_run:
            _log.info("[DRY-RUN] would release deferred cases", id=item.id,
                      verdict=verdict, cases=len(cases), state=item.state)
            return False
        # Comment FIRST, then mark: a comment that fails to post leaves the cases
        # pending, so the next cycle tries again instead of losing the hand-off.
        try:
            posted = await self._provider_for(item.project or project).add_comment(
                item.id, body
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("deferred hand-off not posted", id=item.id, error=describe_exc(exc))
            return False
        if posted is False:
            return False
        await self._repo.release(item.id, status, item.state)
        log = _log.info if verdict == HANDOFF else _log.warning
        log("deferred cases released", id=item.id, verdict=verdict, cases=len(cases),
            state=item.state)
        return True
