"""Work item domain model (ported from ``WorkItemInfo`` + ``TaskCategory``)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class TaskCategory(enum.Enum):
    """Detected category of a work item, used for routing and prioritisation."""

    UNKNOWN = "Unknown"
    BACKEND_TASK = "BackendTask"  # [BE] in title or tag
    FRONTEND_TASK = "FrontendTask"  # [FE] in title or tag
    BUG = "Bug"  # work item type = Bug
    DATABASE_TASK = "DatabaseTask"  # [DB] in title or tag
    TEST_TASK = "TestTask"  # [QC]/[Test] in title or tag
    REQUIREMENT = "Requirement"  # type = User Story / Requirement

    def __str__(self) -> str:  # parity with C# enum.ToString()
        return self.value


@dataclass
class WorkItemInfo:
    """A single Azure DevOps work item."""

    id: int
    title: str = ""
    work_item_type: str = ""
    state: str = ""
    assigned_to: str | None = None          # display name
    assigned_to_email: str | None = None    # uniqueName / email
    description: str | None = None
    acceptance_criteria: str | None = None
    parent_id: int | None = None
    # Dependency-aware scheduling (P1): work items this one depends on (must finish
    # first) and items merely Related (soft conflict — don't run concurrently).
    # Populated only when the link graph is fetched (``$expand=relations``).
    predecessor_ids: list[int] = field(default_factory=list)
    related_ids: list[int] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    area_path: str | None = None
    iteration_path: str | None = None
    changed_date: datetime | None = None
    created_by: str | None = None
    # ADO Priority field (1=Critical, 2=High, 3=Normal, 4=Low)
    priority: int = 3
    category: TaskCategory = TaskCategory.UNKNOWN
    # A human "discussion" comment picked up by the poller's comment-reaction loop
    # (AdoPollerService._reconcile_human_replies) and injected into the agent brief as
    # the top-priority instruction. None outside that path — not populated by _map.
    pending_comment: str | None = None

    def __str__(self) -> str:
        return f"#{self.id} [{self.work_item_type}] {self.title}"
