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
    StateSyncService,
)


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
        state_sync = StateSyncService(container)
        loops = LoopScheduler(container)
        poller.start()
        pr_monitor.start()
        state_sync.start()
        loops.start()
        log.info("autopilot online", health_port=config.health_port)
        try:
            yield
        finally:
            await poller.stop()
            await pr_monitor.stop()
            await state_sync.stop()
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

    @app.post("/api/webhook/ado")
    async def ado_webhook(request: Request) -> dict:
        """ADO Service Hook → enqueue work item ID for the poller to drain."""
        c: Container = app.state.container
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return {"error": "Invalid JSON"}
        resource = payload.get("resource", {}) if isinstance(payload, dict) else {}
        work_item_id = resource.get("workItemId") or resource.get("id")
        if work_item_id is None:
            log.warning("webhook received but no workItemId found")
            return {"error": "No workItemId in payload"}
        c.webhook_queue.enqueue(int(work_item_id))
        log.info("webhook queued work item", id=work_item_id)
        return {"queued": int(work_item_id)}

    app.include_router(create_dashboard_router())
    return app
