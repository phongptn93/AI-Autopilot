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
    AlertStateRepository,
    AuditRepository,
    ClaudeSessionRepository,
    Database,
    ExecutionRepository,
    FleetKnowledgeRepository,
    FleetWorkerRepository,
    LoopReportRepository,
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
from ai_autopilot.execution.sdlc_plan import handoff_collisions
from ai_autopilot.learning import QualityLog
from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.multitenant import TenantManager
from ai_autopilot.notifications import (
    EmailNotifier,
    NotificationChannel,
    TeamsNotifier,
    ZaloNotifier,
)
from ai_autopilot.plugins import PluginManager
from ai_autopilot.providers import WorkItemProvider
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
        # Fleet: populated on a central by worker heartbeats; unused elsewhere.
        self.fleet_repo = FleetWorkerRepository(self.database)
        # Central only in practice, but built unconditionally: a machine's role can be
        # changed on the Settings page without a restart, and a repository that only
        # exists when the process started as a central would not be there afterwards.
        self.fleet_knowledge_repo = FleetKnowledgeRepository(self.database)
        # Scheduled audits. Their own table: a report's deliverable is its text, which
        # does not fit an execution row's summary column — see ``LoopReport``.
        self.loop_report_repo = LoopReportRepository(self.database)
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
        # Work-item providers, keyed by lower-cased ADO/Jira project. Empty on every
        # install that has not declared one, and `provider_for` then answers `self.ado`
        # for everything — so adding this changes no behaviour until a workspace asks
        # for a different tracker. Only WORK ITEMS route here: pull requests, repos and
        # builds stay on `self.ado`, because a Jira team's code lives somewhere else
        # entirely and that is its own provider (and its own release).
        self.providers: dict[str, WorkItemProvider] = {}

        # Notifications.
        self.channels: list[NotificationChannel] = [
            TeamsNotifier(config, self.http),
            ZaloNotifier(config, self.http),
            EmailNotifier(config),
        ]
        self.notification_hold_repo = NotificationHoldRepository(self.database)
        self.alert_state_repo = AlertStateRepository(self.database)
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
        self.cost_tracker = CostTracker(
            self.execution_repo, config, self.channels, self.notifier
        )
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
                self.log.warning("bot identity lookup failed", error=describe_exc(exc))
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

    def provider_for(self, project: str = "") -> WorkItemProvider:
        """The tracker that owns this project's work items.

        Answers ``self.ado`` unless a workspace declared another one, so an install that
        has never heard of Jira behaves exactly as before — and a project nobody claimed
        still lands on ADO rather than failing, which is the same fallback the rest of
        the config uses.
        """
        return self.providers.get((project or "").strip().lower(), self.ado)

    def ado_for(self, project: str = ""):
        """The Azure DevOps client that owns ``project``.

        Distinct from :meth:`provider_for` on purpose, and the difference is not
        cosmetic: ``provider_for`` may hand back a Jira client, which has no
        ``get_repositories`` and no builds. This one only ever returns an ADO client —
        a workspace's own organization when it declared one, the machine's otherwise —
        so it is safe at the call sites that use ADO-specific endpoints.
        """
        from ai_autopilot.ado.client import AdoClient

        found = self.providers.get((project or "").strip().lower())
        return found if isinstance(found, AdoClient) else self.ado

    def build_providers(self) -> None:
        """Instantiate a provider per workspace that asked for a non-ADO tracker.

        Keyed by PROJECT rather than by workspace because that is what callers have: an
        item carries its project, and every other per-workspace lookup in the codebase
        (``scoped_for_project``) is keyed the same way.
        """
        from ai_autopilot.ado.auth import AdoAuthService
        from ai_autopilot.ado.client import AdoClient
        from ai_autopilot.providers import PROVIDER_JIRA
        from ai_autopilot.providers.jira import JiraClient

        self.providers = {}
        for ws in self.config.workspaces or []:
            projects = [p.strip() for p in (ws.ado_projects or []) if p.strip()]
            if not projects:
                continue
            scoped = self.config.scoped_for_project(projects[0])
            if (getattr(ws, "provider", "") or "").strip().lower() == PROVIDER_JIRA:
                client = JiraClient(self.http, scoped, ws)
                self.log.info(
                    "jira provider registered", workspace=ws.name or "(unnamed)",
                    projects=projects, site=ws.jira_url,
                )
            elif (getattr(ws, "ado_organization", "") or "").strip():
                # A SECOND Azure DevOps organization, on the same machine. Its own
                # client because org and credential live on the client, and the scoped
                # settings already carry the workspace's overrides — so this is the
                # same object the root connection is, pointed somewhere else.
                client = AdoClient(self.http, AdoAuthService(scoped), scoped)
                self.log.info(
                    "second ADO organization registered", workspace=ws.name or "(unnamed)",
                    projects=projects, org=scoped.ado_organization,
                )
            else:
                continue                      # shares the machine's own connection
            for project in projects:
                self.providers[project.lower()] = client

    async def startup(self) -> None:
        self.build_providers()
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
        # Only meaningful once this machine actually runs a relay: an install with
        # neither the loop nor any stage wiring has no hand-off to collide with.
        uses_relay = bool(self.config.sdlc_loop_enabled or self.config.sdlc_stage_wiring)
        collisions = handoff_collisions(self.config) if uses_relay else []
        if collisions:
            self.log.error(
                "SDLC handoff state collides with this machine's trigger states — "
                "items will be re-processed forever. Fix sdlc_profile_states / "
                "trigger_states, or mark the stage auto=false.",
                collisions=[f"{name} -> {state}" for name, state in collisions],
                trigger_states=self.config.effective_trigger_states,
            )

    async def shutdown(self) -> None:
        await self.http.aclose()
        await self.database.dispose()
