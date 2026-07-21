"""Composition root / dependency-injection container.

Replaces the ASP.NET ``builder.Services`` registrations. Everything is wired once
at startup and held on a single ``Container`` instance that the FastAPI app stores
on ``app.state`` and the background services consume.
"""

from __future__ import annotations

import httpx

from ai_autopilot.ado import AdoAuthService, AdoClient, AdoNotifier
from ai_autopilot.config import Settings
from ai_autopilot.data import (
    AiConflictRepository,
    Database,
    ExecutionRepository,
    PlannedRunRepository,
    PrCommandRepository,
    SchedulerHistoryRepository,
    SdlcLoopStateRepository,
    StateRepository,
    SyncStateRepository,
)
from ai_autopilot.execution import (
    AutoReviewer,
    ClaudeExecutor,
    FeedbackHandler,
    RetryPolicy,
    SdlcLoopEngine,
)
from ai_autopilot.execution.sdlc_plan import handoff_collides
from ai_autopilot.logging_config import get_logger
from ai_autopilot.multitenant import TenantManager
from ai_autopilot.notifications import (
    EmailNotifier,
    NotificationChannel,
    TeamsNotifier,
    ZaloNotifier,
)
from ai_autopilot.plugins import PluginManager
from ai_autopilot.routing import RequirementDecomposer, TaskRouter
from ai_autopilot.scheduling import ScheduleGuard
from ai_autopilot.security import RbacPolicy
from ai_autopilot.tracking import CostTracker
from ai_autopilot.webhook import WebhookQueue


class Container:
    """Holds every singleton service for the lifetime of the process."""

    def __init__(self, config: Settings) -> None:
        self.config = config
        self.log = get_logger("container")

        # Shared async HTTP client (connection pooling).
        self.http = httpx.AsyncClient(timeout=30)

        # Persistence.
        self.database = Database(config.database_url)
        self.execution_repo = ExecutionRepository(self.database)
        self.state_repo = StateRepository(self.database)
        self.sdlc_state_repo = SdlcLoopStateRepository(self.database)
        self.sync_repo = SyncStateRepository(self.database)
        self.planned_run_repo = PlannedRunRepository(self.database)
        self.ai_conflict_repo = AiConflictRepository(self.database)
        self.scheduler_history_repo = SchedulerHistoryRepository(self.database)
        self.pr_command_repo = PrCommandRepository(self.database)

        # ADO.
        self.auth = AdoAuthService(config)
        self.ado = AdoClient(self.http, self.auth, config)

        # Notifications.
        self.channels: list[NotificationChannel] = [
            TeamsNotifier(config, self.http),
            ZaloNotifier(config, self.http),
            EmailNotifier(config),
        ]
        self.notifier = AdoNotifier(self.ado, config, self.channels)

        # Execution.
        self.reviewer = AutoReviewer(config)
        self.executor = ClaudeExecutor(config, self.reviewer)
        self.feedback = FeedbackHandler(self.executor, config)
        self.retry_policy = RetryPolicy(config.max_retries, config.retry_backoff_seconds)

        # Routing & policy.
        self.router = TaskRouter()
        self.decomposer = RequirementDecomposer(self.ado, config)

        # Closed-loop SDLC engine (opt-in via sdlc_loop_enabled).
        self.sdlc_engine = SdlcLoopEngine(
            self.executor, self.reviewer, self.ado, self.router, config, self.sdlc_state_repo
        )
        self.schedule = ScheduleGuard(config)
        self.rbac = RbacPolicy(config)

        # Cross-cutting.
        self.cost_tracker = CostTracker(self.execution_repo, config, self.channels)
        self.tenants = TenantManager(config)
        self.plugins = PluginManager()
        self.webhook_queue = WebhookQueue()
        # Last dependency-scheduling decision (ready vs deferred), for the Planning UI.
        self.scheduler_view: dict | None = None

    async def startup(self) -> None:
        await self.database.create_all()
        await self.plugins.load_and_init(self.config.plugins_directory, self)
        # Fail-fast guard: a machine that hands off to one of its OWN trigger states
        # would re-pick items it just finished (an infinite loop).
        if self.config.sdlc_loop_enabled and handoff_collides(self.config):
            self.log.error(
                "SDLC handoff state collides with this machine's trigger_states — "
                "items will be re-processed forever. Fix sdlc_profile_states / trigger_states.",
                profile=self.config.sdlc_profile,
                trigger_states=self.config.trigger_states,
            )

    async def shutdown(self) -> None:
        await self.http.aclose()
        await self.database.dispose()
