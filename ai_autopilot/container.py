"""Composition root / dependency-injection container.

Replaces the ASP.NET ``builder.Services`` registrations. Everything is wired once
at startup and held on a single ``Container`` instance that the FastAPI app stores
on ``app.state`` and the background services consume.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ai_autopilot.ado import AdoAuthService, AdoClient, AdoNotifier
from ai_autopilot.config import BotIdentity, Settings
from ai_autopilot.data import (
    AiConflictRepository,
    AuditRepository,
    ClaudeSessionRepository,
    Database,
    ExecutionRepository,
    NotificationHoldRepository,
    PlannedRunRepository,
    PrCommandRepository,
    PrReviewerRepository,
    QualityRepository,
    SchedulerHistoryRepository,
    SdlcLoopStateRepository,
    SpecDriftRepository,
    StateHistoryRepository,
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
from ai_autopilot.learning import QualityLog
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
        # Append-only ADO state transitions — the clock behind the Delivery page.
        self.state_history = StateHistoryRepository(self.database)
        self.sync_repo = SyncStateRepository(self.database)
        self.planned_run_repo = PlannedRunRepository(self.database)
        self.ai_conflict_repo = AiConflictRepository(self.database)
        self.scheduler_history_repo = SchedulerHistoryRepository(self.database)
        self.pr_command_repo = PrCommandRepository(self.database)
        self.spec_drift_repo = SpecDriftRepository(self.database)
        self.pr_reviewer_repo = PrReviewerRepository(self.database)
        self.claude_session_repo = ClaudeSessionRepository(self.database)
        self.audit_repo = AuditRepository(self.database)
        # The append-only table, wrapped by the funnel that also feeds the learning
        # loop — every call site records through the funnel, never the bare repo.
        self.quality_events = QualityRepository(self.database)
        self.quality_repo = QualityLog(
            self.quality_events, config,
            repos_provider=lambda: self.executor._allowed_repos(config.workspace_directory),
        )

        # ADO.
        self.auth = AdoAuthService(config)
        self.ado = AdoClient(self.http, self.auth, config)

        # Notifications.
        self.channels: list[NotificationChannel] = [
            TeamsNotifier(config, self.http),
            ZaloNotifier(config, self.http),
            EmailNotifier(config),
        ]
        self.notification_hold_repo = NotificationHoldRepository(self.database)
        self.notifier = AdoNotifier(
            self.ado, config, self.channels, self.notification_hold_repo
        )

        # Execution.
        self.reviewer = AutoReviewer(config)
        self.executor = ClaudeExecutor(config, self.reviewer, self.claude_session_repo)
        self.feedback = FeedbackHandler(self.executor, config)
        self.retry_policy = RetryPolicy(config.max_retries, config.retry_backoff_seconds)

        # Routing & policy.
        self.router = TaskRouter()
        self.decomposer = RequirementDecomposer(self.ado, config)

        # Closed-loop SDLC engine (opt-in via sdlc_loop_enabled).
        self.sdlc_engine = SdlcLoopEngine(
            self.executor, self.reviewer, self.ado, self.router, config, self.sdlc_state_repo,
            self.quality_repo,
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
        self._bot_identity: dict | None = None  # cached connectionData (see bot_identity)

    async def bot_identity(self) -> dict:
        """Who "the bot" is on ADO — the identity behind our credentials, auto-detected
        once via ``connectionData``. Shared here (rather than cached per service) because
        the reviewer tracker, the PR babysitter and the work-item poller all need it: the
        tracker to recognise "the bot was added as a reviewer", the other two to recognise
        an @mention of the bot in a comment.

        Always returns a dict (blank fields when detection failed) so callers never have to
        None-check; ``pr_bot_identity`` remains the manual override for that case."""
        if self._bot_identity is None:
            detected = None
            try:
                detected = await self.ado.get_connection_data()
            except Exception as exc:  # noqa: BLE001 — never block startup on this
                self.log.warning("bot identity lookup failed", error=str(exc))
            self._bot_identity = detected or {"id": "", "display_name": "", "unique_name": ""}
            if detected:
                self.log.info(
                    "bot identity resolved", id=detected["id"],
                    name=detected["display_name"], unique=detected["unique_name"],
                )
            else:
                self.log.warning(
                    "could not resolve bot identity from connectionData — @mention "
                    "detection falls back to display name / pr_bot_identity",
                    override=self.config.pr_bot_identity,
                )
        return self._bot_identity

    async def mention_identity(self) -> BotIdentity | None:
        """``BotIdentity`` for @mention matching, or ``None`` when the feature is off."""
        if not self.config.comment_mention_enabled:
            return None
        bot = await self.bot_identity()
        return BotIdentity(
            identity_id=bot.get("id") or "",
            display_name=(
                bot.get("display_name") or self.config.pr_bot_identity or ""
            ),
            claimed=self.config.command_user,
        )

    async def startup(self) -> None:
        await self.database.create_all()
        # Plugins run as fully-trusted code with Container access — load only when
        # explicitly enabled, so a file dropped into ./plugins can't silently run.
        if self.config.plugins_enabled:
            await self.plugins.load_and_init(self.config.plugins_directory, self)
        elif self.config.plugins_directory and Path(self.config.plugins_directory).is_dir():
            self.log.warning(
                "plugins directory present but plugins_enabled=false — skipping load",
                dir=self.config.plugins_directory,
            )
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
