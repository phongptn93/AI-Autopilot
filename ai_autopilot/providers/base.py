"""The work-item surface a tracker must provide, and nothing more.

Deliberately the smallest set the pipeline actually calls — poll, read, comment, tag,
move — rather than everything ``AdoClient`` can do. A protocol that mirrored one
vendor's whole API would make the second implementation a stub farm, and the methods
nobody calls are exactly the ones that would be wrong.

``AdoClient`` already satisfies this; it is not modified. A structural (non-runtime)
Protocol is used so neither implementation has to inherit from anything, and so an
adapter can be a plain object in a test.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ai_autopilot.models import WorkItemInfo

PROVIDER_ADO = "ado"
PROVIDER_JIRA = "jira"

# What a tracker's own state categories are reduced to. ADO names them exactly this way
# (``System.StateCategory``); Jira's three (``new`` / ``indeterminate`` / ``done``) map
# onto them, so everything downstream — the board, the delivery report, "is it done?" —
# keeps asking one question in one vocabulary.
CAT_PROPOSED = "Proposed"
CAT_IN_PROGRESS = "InProgress"
CAT_COMPLETED = "Completed"


@runtime_checkable
class WorkItemProvider(Protocol):
    """Where work items come from, and how they are moved.

    Every method is best-effort in the same way the ADO client is: network failures are
    logged and turned into an empty result or ``False``, never raised into the poller —
    a tracker being briefly unreachable must not end the run loop.
    """

    # ── discovery ────────────────────────────────────────────────────────────
    async def get_pending_work_items(self) -> list[WorkItemInfo]:
        """Items the autopilot may pick up: trigger tag AND a trigger state."""
        ...

    async def get_all_tagged_work_items(self) -> list[WorkItemInfo]:
        """Every item carrying a trigger tag, whatever state it is in."""
        ...

    async def get_work_items_tagged_any(self, tags: list[str]) -> list[WorkItemInfo]:
        """Items carrying any of ``tags`` — the run-now sweep, which must find an item
        wherever it stands, so this one does NOT filter by state."""
        ...

    # ── reading ──────────────────────────────────────────────────────────────
    async def get_work_item(self, work_item_id: int) -> WorkItemInfo | None: ...

    async def get_work_items_by_ids(self, ids: list[int]) -> list[WorkItemInfo]: ...

    async def get_work_item_comments(self, work_item_id: int) -> list[dict[str, Any]]:
        """Newest-first comments as ``{"text": ..., "created_by": ..., "created_date": ...}``."""
        ...

    async def get_children(self, parent_id: int) -> list[WorkItemInfo]: ...

    async def get_state_categories(self) -> dict[str, str]:
        """``state name (lower) -> CAT_*``. Used to ask "is this done?" without
        hard-coding one tracker's vocabulary."""
        ...

    # ── writing ──────────────────────────────────────────────────────────────
    async def add_comment(self, work_item_id: int, comment: str) -> bool:
        """Post a comment. ``comment`` is the HTML the ADO client takes; a provider
        whose API wants something else converts it."""
        ...

    async def add_tag(self, work_item_id: int, tag: str) -> bool: ...

    async def remove_tag(self, work_item_id: int, tag: str) -> bool: ...

    async def update_state(self, work_item_id: int, new_state: str) -> bool:
        """Move the item. False when the tracker refused — which is a real answer, not
        an error: a state may not exist for this type, or the workflow may not allow
        the jump from where the item stands."""
        ...

    def refresh(self) -> None:
        """Re-read anything cached from config (called after a settings save)."""
        ...
