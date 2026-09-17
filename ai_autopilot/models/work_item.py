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
    # The tracker's own human-facing id, when it differs from the numeric one: Jira
    # issues are "DXF-123" while their API id is 10042. The numeric id stays the key
    # everywhere (database, branch names, every dashboard page); this rides alongside
    # for display, for the tracker's own API calls, and for branch names a Jira smart
    # commit can link back to. Blank for ADO, where the number IS the name.
    key: str = ""
    # Which tracker this item came from ("ado" | "jira"). Set by the provider, read by
    # anything that has to speak the tracker's language — the agent brief's "use the X
    # MCP" line, a link back to the item, a page that wants to show where it lives.
    provider: str = ""
    title: str = ""
    # ADO ``System.TeamProject`` — which work-item project this item lives in. One
    # autopilot connection can poll several projects, and an id alone does not say
    # which: the comments API, work-item creation and the type/state map are all
    # project-scoped, so an item that did not carry its own project would have its
    # comments posted against the DEFAULT project and silently 404.
    project: str = ""
    work_item_type: str = ""
    state: str = ""
    assigned_to: str | None = None          # display name
    assigned_to_email: str | None = None    # uniqueName / email
    # ADO identity GUID of the assignee. Needed to add them as a PR reviewer: the
    # reviewers endpoint is keyed on the identity id, and an email is not one.
    assigned_to_id: str | None = None
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
    # When the item was CREATED. Unlike changed_date (bumped by any edit, including a
    # comment) this never moves, which makes it the only trustworthy start point for
    # lead time — see the Delivery page.
    created_date: datetime | None = None
    created_by: str | None = None
    # ADO Priority field (1=Critical, 2=High, 3=Normal, 4=Low)
    priority: int = 3
    category: TaskCategory = TaskCategory.UNKNOWN
    # A human "discussion" comment picked up by the poller's comment-reaction loop
    # (AdoPollerService._reconcile_human_replies) and injected into the agent brief as
    # the top-priority instruction. None outside that path — not populated by _map.
    pending_comment: str | None = None

    @property
    def ref(self) -> str:
        """How a human refers to this item: "DXF-123" on Jira, "#9083" on ADO."""
        return self.key or f"#{self.id}"

    def __str__(self) -> str:
        return f"{self.ref} [{self.work_item_type}] {self.title}"
