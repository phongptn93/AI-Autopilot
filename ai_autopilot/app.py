"""FastAPI application factory + lifespan (replaces ``Program.cs``)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ai_autopilot import health
from ai_autopilot.config import Settings, load_settings
from ai_autopilot.container import Container
from ai_autopilot.dashboard import create_dashboard_router
from ai_autopilot.logging_config import configure_logging, get_logger
from ai_autopilot.services import (
    AdoPollerService,
    LoopScheduler,
    PrMonitorService,
    ReviewerTrackerService,
    StateSyncService,
)
from ai_autopilot.teams_agent import build_agent as build_teams_agent


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or load_settings()
    configure_logging(level="INFO")
    log = get_logger("app")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container = Container(config)
        app.state.container = container
        await container.startup()

        poller = AdoPollerService(container)
        pr_monitor = PrMonitorService(container)
        app.state.pr_monitor = pr_monitor  # webhook fast-path targets it directly
        state_sync = StateSyncService(container)
        reviewer_tracker = ReviewerTrackerService(container)
        loops = LoopScheduler(container)
        poller.start()
        pr_monitor.start()
        state_sync.start()
        reviewer_tracker.start()
        loops.start()

        teams_agent = build_teams_agent(config, container, reviewer_tracker)
        if teams_agent is not None:
            app.state.teams_agent, app.state.teams_adapter = teams_agent
            log.info("Teams bot enabled — /api/messages live")
        else:
            app.state.teams_agent = None

        log.info("autopilot online", health_port=config.health_port)
        try:
            yield
        finally:
            await poller.stop()
            await pr_monitor.stop()
            await state_sync.stop()
            await reviewer_tracker.stop()
            await loops.stop()
            await container.shutdown()
            log.info("autopilot stopped")

    app = FastAPI(title="AI Autopilot", version="2.0.0", lifespan=lifespan)

    @app.get("/health")
    async def health_endpoint(response: Response) -> dict:
        c: Container = app.state.container
        results = [
            await health.check_ado(c.auth, c.http),
            await health.check_claude(),
            health.check_disk(),
        ]
        overall = health.aggregate(results)
        if overall is health.HealthStatus.UNHEALTHY:
            response.status_code = 503
        return {
            "status": overall.value,
            "checks": [
                {
                    "name": r.name,
                    "status": r.status.value,
                    "description": r.description,
                    "duration_ms": round(r.duration_ms, 1),
                    "data": r.data,
                }
                for r in results
            ],
        }

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/api/messages")
    async def teams_messages(request: Request) -> Response:
        """Microsoft Teams bot endpoint (Azure Bot Service messaging endpoint).

        No-op (404) unless the Teams bot is configured — see
        ``ai_autopilot/teams_agent.py`` for the enable conditions."""
        teams_agent = getattr(request.app.state, "teams_agent", None)
        if teams_agent is None:
            return Response(status_code=404)
        from microsoft_agents.hosting.fastapi import start_agent_process

        adapter = request.app.state.teams_adapter
        result = await start_agent_process(request, teams_agent, adapter)
        return result if result is not None else Response(status_code=200)

    @app.post("/api/webhook/ado")
    async def ado_webhook(request: Request) -> dict:
        """ADO Service Hook receiver.

        Two event families:
        - PR comment (``ms.vss-code.git-pullrequest-comment-event``) → kick the PR
          babysitter to inspect that PR NOW, so a ``/command`` reply is acked in ~1s
          instead of waiting out the poll interval. Polling stays on as the fallback
          for missed/undelivered hooks.
        - Work-item events → enqueue the id for the poller to drain (as before).
        """
        c: Container = app.state.container
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return {"error": "Invalid JSON"}
        if not isinstance(payload, dict):
            return {"error": "Invalid JSON"}
        resource = payload.get("resource", {}) or {}

        if payload.get("eventType") == "ms.vss-code.git-pullrequest-comment-event":
            from ai_autopilot.config import is_bot_signed, match_command

            pr = resource.get("pullRequest") or {}
            repo = pr.get("repository") or {}
            repo_id, pr_id = repo.get("id"), pr.get("pullRequestId")
            monitor = getattr(app.state, "pr_monitor", None)
            if not (repo_id and pr_id and monitor):
                return {"error": "No pullRequest in payload"}
            content = (resource.get("comment") or {}).get("content") or ""
            if is_bot_signed(content):
                return {"ignored": "bot comment"}  # our own reply — never self-trigger
            # Only a /command warrants an immediate inspection; plain chatter waits
            # for the regular poll (which ignores it anyway).
            if content and match_command(content, c.config.comment_commands) is None:
                return {"ignored": "not a command"}
            monitor.kick(repo_id, repo.get("name") or "", pr)
            log.info("webhook kicked PR inspection", pr=pr_id)
            return {"kicked": pr_id}

        work_item_id = resource.get("workItemId") or resource.get("id")
        if work_item_id is None:
            log.warning("webhook received but no workItemId found")
            return {"error": "No workItemId in payload"}
        c.webhook_queue.enqueue(int(work_item_id))
        log.info("webhook queued work item", id=work_item_id)
        return {"queued": int(work_item_id)}

    app.include_router(create_dashboard_router())
    return app
