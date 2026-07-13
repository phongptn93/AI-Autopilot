"""Dependency-aware scheduling (P1) — the "làm sao giải quyết xung đột giữa các
req" problem, solved cheaply from the ADO **link graph** (no tokens).

Each poll cycle the poller hands this module the pending candidates plus the link
maps it read off ADO, and gets back a :class:`SchedulePlan`: which items are
*ready* to dispatch now and which are *deferred* (with a human-readable reason).
Deferred items are simply not marked processed, so the next 30 s poll re-evaluates
them — waves emerge across cycles without any persistent scheduler state.

Two link relationships drive it:

* **Predecessor** (ADO ``Dependency-Reverse``) — a hard order: *B depends on A*, so
  B waits until A is done. A predecessor still open (pending, in-flight, or in a
  non-``done`` state) blocks its successor.
* **Related** (ADO ``Related``) — a soft conflict: two related items likely touch
  the same area, so they must not run **concurrently**. The higher-priority one
  goes; the rest defer to a later wave. (The BA's "Related Work" links feed this.)

The core is a **pure function** (like ``pr_scorer``) — the poller gathers the
signals, this only decides. That makes every gating rule trivially testable.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass, field

from ai_autopilot.models import WorkItemInfo
from ai_autopilot.routing.priority import sort_by_priority


@dataclass
class SchedulePlan:
    """Outcome of one scheduling pass over the pending candidates."""

    ready: list[WorkItemInfo] = field(default_factory=list)   # dispatch this cycle
    deferred: dict[int, str] = field(default_factory=dict)    # id → why it waits (VI)


def _fmt(ids: Set[int]) -> str:
    return ", ".join(f"#{i}" for i in sorted(ids))


def plan_schedule(
    items: list[WorkItemInfo],
    *,
    predecessors: Mapping[int, Set[int]] | None = None,
    related: Mapping[int, Set[int]] | None = None,
    active_ids: Set[int] | None = None,
    busy_ids: Set[int] | None = None,
    max_dispatch: int | None = None,
) -> SchedulePlan:
    """Split ``items`` into (ready, deferred) using the link graph.

    Parameters
    ----------
    items:
        Candidate pending work items (any order — sorted by priority internally).
    predecessors:
        ``id → {predecessor ids}``. A predecessor present in ``active_ids`` blocks
        the item (hard dependency order).
    related:
        ``id → {related ids}`` (should be symmetric). Related items form a conflict
        group and are not dispatched together in one wave.
    active_ids:
        Ids that are **not yet done** — pending candidates, in-flight items, and any
        linked item whose ADO state is not in ``done_states``. A predecessor in this
        set is unmet.
    busy_ids:
        Ids currently in-flight (e.g. live sessions). Seeds the conflict set so a
        related item already running blocks its neighbour this cycle.
    max_dispatch:
        Optional cap on how many items go ready per cycle (``None`` = no cap; the
        executor's concurrency semaphore still throttles actual parallelism).

    Returns
    -------
    SchedulePlan
        ``ready`` in priority order, and ``deferred`` mapping id → reason.
    """
    predecessors = predecessors or {}
    related = related or {}
    active = set(active_ids or set())
    # Items already running claim their related neighbourhood immediately.
    claimed: set[int] = set(busy_ids or set())

    plan = SchedulePlan()
    for item in sort_by_priority(items):
        # 1) Hard order: every predecessor must be done.
        unmet = {p for p in predecessors.get(item.id, set()) if p in active}
        if unmet:
            plan.deferred[item.id] = f"chờ tiền nhiệm {_fmt(unmet)} hoàn tất"
            continue

        # 2) Soft conflict: a related item is already claimed this wave / running.
        clash = set(related.get(item.id, set())) & claimed
        if clash:
            plan.deferred[item.id] = f"xung đột (Related) với {_fmt(clash)} — chạy wave sau"
            continue

        # 3) Per-cycle capacity.
        if max_dispatch is not None and len(plan.ready) >= max_dispatch:
            plan.deferred[item.id] = "đủ slot cho chu kỳ này — chờ lượt sau"
            continue

        plan.ready.append(item)
        # Claim this item and its whole related neighbourhood so later items in the
        # same group defer to the next wave.
        claimed.add(item.id)
        claimed |= set(related.get(item.id, set()))

    return plan
