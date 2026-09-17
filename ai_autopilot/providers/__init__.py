"""Work-item providers: where the items an autopilot runs on come from.

Azure DevOps was the only source for the life of this project, so ``container.ado`` is
read directly in a dozen services. Some teams track their work in Jira instead, and on
one machine both can be true at once — a workspace is bound to a project, and the
project's tracker is a property of that workspace, not of the machine.

Only the WORK-ITEM half is abstracted here. Pull requests, repositories and builds stay
on ``container.ado``: a Jira team's code lives in Bitbucket or GitHub, which is a
separate provider with its own API surface and its own release.
"""

from ai_autopilot.providers.base import (
    PROVIDER_ADO,
    PROVIDER_JIRA,
    WorkItemProvider,
)

__all__ = ["PROVIDER_ADO", "PROVIDER_JIRA", "WorkItemProvider"]
