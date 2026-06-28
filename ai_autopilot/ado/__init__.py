"""Azure DevOps integration (auth, REST client, notifier)."""

from ai_autopilot.ado.auth import AdoAuthService
from ai_autopilot.ado.client import AdoClient
from ai_autopilot.ado.notifier import AdoNotifier

__all__ = ["AdoAuthService", "AdoClient", "AdoNotifier"]
