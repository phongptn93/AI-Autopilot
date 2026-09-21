"""FastAPI application factory + lifespan (replaces ``Program.cs``)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import secrets
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ai_autopilot import fleet, health, security
from ai_autopilot.config import Settings, load_settings
from ai_autopilot.container import Container
from ai_autopilot.dashboard import create_dashboard_router
from ai_autopilot.logging_config import configure_logging, get_logger
from ai_autopilot.services import (
    AdoPollerService,
    DeliveryTrackerService,
    FleetAgentService,
    LoopScheduler,
    PrMonitorService,
    ProcessHealthService,
    ReviewerTrackerService,
    StateSyncService,
)
from ai_autopilot.teams_agent import build_agent as build_teams_agent
from ai_autopilot.teams_agent import cancel_background_work as cancel_teams_background_work


def _dashboard_auth_ok(header: str | None, config: Settings) -> bool:
    """Validate an HTTP Basic ``Authorization`` header against the dashboard password.

    Any username is accepted. The password is checked against
    ``dashboard_auth_password_hash`` (PBKDF2, preferred) and, for backward
    compatibility, against the legacy plaintext ``dashboard_auth_token``. Either
    matching passes; both comparisons are constant-time.
    """
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return False
    _, _, password = decoded.partition(":")
    if config.dashboard_auth_password_hash and security.verify_password(
        password, config.dashboard_auth_password_hash
    ):
        return True
    return bool(config.dashboard_auth_token) and secrets.compare_digest(
        password, config.dashboard_auth_token
    )


def _dashboard_authenticated(request: Request, config: Settings) -> bool:
    """Either a valid session cookie (browsers) or HTTP Basic (scripts, curl, probes).

    Basic stays supported deliberately: the login page is for people, but automation that
    already sends `-u :password` should not break because the UI got nicer.
    """
    if security.verify_session_token(request.cookies.get(security.SESSION_COOKIE), config):
        return True
    return _dashboard_auth_ok(request.headers.get("authorization"), config)


def _dashboard_challenge(request: Request) -> Response:
    """Send a browser to the login page; answer everything else with a plain 401.

    A `WWW-Authenticate: Basic` response is what makes the browser throw up its native
    credential popup — so returning it here would defeat the login page entirely. It is
    only sent when the caller did not ask for HTML, where the popup cannot appear and a
    machine-readable 401 is the useful answer.
    """
    wants_html = "text/html" in (request.headers.get("accept") or "")
    if request.method == "GET" and wants_html:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(
            f"/dashboard/login?next={quote(target, safe='')}", status_code=303
        )
    return Response(
        status_code=401,
        content="authentication required",
        headers={"WWW-Authenticate": 'Basic realm="AI Autopilot"'},
    )


# Windows socket errors that mean "the peer went away", nothing more. None of them is
# actionable and none says anything about our own state, so they belong at debug level.
#   10054 ECONNRESET   — connection forcibly closed by the remote host
#   10053 ECONNABORTED — the local stack aborted a half-open connection
#      64 ERROR_NETNAME_DELETED — the network name is no longer available, i.e. the
#         client vanished between the SYN and the completion of AcceptEx
#    1236 ERROR_CONNECTION_ABORTED
_PEER_GONE_WINERRORS = frozenset({64, 1236, 10053, 10054})


def _quiet_proactor_connection_reset(log) -> None:
    """Downgrade the harmless Windows "peer went away" Proactor noise.

    On Windows the Proactor event loop logs an *unhandled*
    ``ConnectionResetError: [WinError 10054] An existing connection was forcibly
    closed by the remote host`` from ``_ProactorBasePipeTransport._call_connection_lost``
    whenever a subprocess pipe / TLS socket is torn down after the peer already
    closed it. It does not affect the run (``claude_client`` retries the real
    failure) — it just spams the log. The same is true of the ``Task exception was
    never retrieved`` report for the ``accept_coro`` task that
    :func:`_keep_listener_alive_on_accept_error` leaves behind when a client drops
    mid-accept. Route only these to debug and delegate everything else to the loop's
    default handler.
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(loop, context):
        exc = context.get("exception")
        if (
            isinstance(exc, OSError)
            and getattr(exc, "winerror", None) in _PEER_GONE_WINERRORS
        ):
            log.debug("ignored proactor socket error", winerror=exc.winerror)
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


def _keep_listener_alive_on_accept_error(log, loop=None) -> None:
    """Stop one dead client connection from taking the whole HTTP listener down.

    On Windows, ``AcceptEx`` completes with ``OSError [WinError 64] The specified
    network name is no longer available`` when a client disappears between the SYN
    and the completion — a port scan, a probe from a monitoring tool, a laptop that
    slept, a VPN that flapped. CPython's Proactor accept loop
    (``BaseProactorEventLoop._start_serving``) treats *any* ``OSError`` there as fatal
    for the listening socket:

        except OSError as exc:
            if sock.fileno() != -1:
                self.call_exception_handler({'message': 'Accept failed on a socket', ...})
                sock.close()          # ← the listener is now gone, permanently

    Nothing re-opens it. The process stays up — the poller, the loops and the PR
    babysitter all keep running, so from the outside it looks healthy — but every
    later request to the dashboard hangs at TCP connect with no one left to accept it,
    until someone restarts the process. That is the "dashboard only loads after it
    hangs" symptom.

    The fix is to keep the failure away from that ``except`` clause: wrap the
    proactor's ``accept`` so a failed accept is retried on the same listening socket
    instead of raising into the loop that would close it. Patched on the running
    loop's proactor instance, not the class, so it is scoped to this application and
    inert on every other platform (no ``_proactor``, nothing to wrap).

    ``loop`` defaults to the running loop and exists so a test can hand in a stub
    proactor instead of mutating the loop it is running on.
    """
    loop = loop or asyncio.get_running_loop()
    proactor = getattr(loop, "_proactor", None)
    accept = getattr(proactor, "accept", None)
    if accept is None or getattr(proactor, "_autopilot_accept_guard", False):
        return  # not a Proactor loop (POSIX), or already wrapped

    async def _accept_until_one_succeeds(listener):
        while True:
            try:
                return await accept(listener)
            except OSError as exc:
                if listener.fileno() == -1:
                    raise  # the listener really is closed — we are shutting down
                log.warning(
                    "client dropped during accept; listener kept open",
                    winerror=getattr(exc, "winerror", None), error=str(exc),
                )
                # A storm of failing accepts must not turn into a busy loop.
                await asyncio.sleep(0.05)

    def guarded_accept(listener):
        # The caller stores this in ``_accept_futures`` and cancels it on shutdown, so
        # it has to be a future — a Task is one, and cancelling it cancels the inner
        # accept exactly as before.
        return asyncio.ensure_future(_accept_until_one_succeeds(listener))

    proactor.accept = guarded_accept
    proactor._autopilot_accept_guard = True


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or load_settings()
    configure_logging(level="INFO")
    log = get_logger("app")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _quiet_proactor_connection_reset(log)
        # Before uvicorn calls `loop.create_server` (lifespan startup runs first), so the
        # very first accept is already guarded.
        _keep_listener_alive_on_accept_error(log)
        container = Container(config)
        app.state.container = container
        started: list = []  # services successfully .start()ed — torn down in reverse
        teams_digest_task = None

        async def _teardown() -> None:
            for svc in reversed(started):
                with contextlib.suppress(Exception):
                    await svc.stop()
            if teams_digest_task is not None:
                teams_digest_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await teams_digest_task
            # Deferred Teams replies / background PR reviews are detached on purpose —
            # cancel them explicitly so shutdown is clean.
            with contextlib.suppress(Exception):
                await cancel_teams_background_work()
            with contextlib.suppress(Exception):
                await container.shutdown()

        # Startup is atomic: if anything below raises, tear down whatever already
        # started (services + the container's http client / DB) before propagating,
        # so a failed boot never leaks resources or leaves live background tasks.
        try:
            await container.startup()
            # Fold together what older builds left behind. Dedup-by-meaning and the
            # dead-pointer rule both run on WRITE, so a file already on disk kept
            # everything it had accumulated: five identical reopens and two "re-read
            # the comments on that PR" lines went on eating seven of the eight
            # injection slots after the upgrade that fixed them — which reads, fairly,
            # as the fix having done nothing. Once per process, best-effort.
            if config.workspace_directory:
                with contextlib.suppress(Exception):
                    from ai_autopilot import lessons as _lessons

                    merged, dropped = _lessons.compact_all(config.workspace_directory)
                    if merged or dropped:
                        log.info("knowledge compacted", merged=merged, dropped=dropped)
            reviewer_tracker = ReviewerTrackerService(container)
            for svc in (
                AdoPollerService(container),
                PrMonitorService(container),
                StateSyncService(container),
                DeliveryTrackerService(container),
                reviewer_tracker,
                LoopScheduler(container),
                ProcessHealthService(container),
                # Worker machines only: report in and pull the shared config. A central
                # or standalone install starts nothing, so this is inert by default.
                *(
                    (FleetAgentService(container),)
                    if config.fleet_role == fleet.ROLE_WORKER else ()
                ),
            ):
                svc.start()
                started.append(svc)
                if isinstance(svc, PrMonitorService):
                    app.state.pr_monitor = svc  # webhook fast-path targets it directly
                if isinstance(svc, AdoPollerService):
                    app.state.poller = svc      # the board asks it about live sessions
                if isinstance(svc, LoopScheduler):
                    # The Loops page edits this service's schedule and runs its loops
                    # on demand, so it needs the instance, not another copy: a second
                    # scheduler would double-fire every job it registered.
                    app.state.loop_scheduler = svc
                if isinstance(svc, FleetAgentService):
                    # The Fleet page's "sync now" button beats through THIS instance —
                    # a second agent would report a second machine under the same name.
                    app.state.fleet_agent = svc

            teams_bot = build_teams_agent(config, container, reviewer_tracker)
            if teams_bot is not None:
                app.state.teams_agent, app.state.teams_adapter, teams_digest_task = teams_bot
                log.info("Teams bot enabled — /api/messages live")
            else:
                app.state.teams_agent = None

            dashboard_has_auth = bool(
                config.dashboard_auth_password_hash or config.dashboard_auth_token
            )
            exposed = config.health_host not in ("127.0.0.1", "localhost", "::1")
            if exposed and not dashboard_has_auth:
                log.warning(
                    "dashboard is exposed on a non-loopback host with NO auth — anyone who "
                    "can reach it can rewrite config (incl. the ADO PAT) and trigger runs. "
                    "Set dashboard_auth_token (and webhook_secret), or bind health_host to 127.0.0.1.",
                    health_host=config.health_host, health_port=config.health_port,
                )
            log.info("autopilot online", health_port=config.health_port)
        except Exception:
            log.error("startup failed — tearing down partially-started services")
            await _teardown()
            raise

        try:
            yield
        finally:
            await _teardown()
            log.info("autopilot stopped")

    app = FastAPI(title="AI Autopilot", version="2.49.0", lifespan=lifespan)

    @app.middleware("http")
    async def _security_guard(request: Request, call_next):
        """Opt-in auth for the network-exposed web surface.

        Both gates are no-ops until configured, so existing setups are unchanged —
        but once ``dashboard_auth_token`` / ``webhook_secret`` are set they are
        enforced here, before any handler runs. ``/health`` and ``/metrics`` stay
        open for probes.
        """
        path = request.url.path
        if config.webhook_secret and path.startswith("/api/webhook"):
            got = request.headers.get("x-webhook-secret") or request.query_params.get("secret", "")
            if not secrets.compare_digest(got, config.webhook_secret):
                return Response(status_code=401, content="unauthorized")
        dashboard_locked = bool(config.dashboard_auth_password_hash or config.dashboard_auth_token)
        if dashboard_locked and path.startswith("/dashboard"):
            # The login page itself must stay reachable while locked, or there is no way in.
            if path.rstrip("/") != "/dashboard/login" and not _dashboard_authenticated(
                request, config
            ):
                return _dashboard_challenge(request)
        response = await call_next(request)
        # Dashboard pages are server-rendered snapshots of mutable config, and nothing
        # told the browser so. After a save the 303 lands back on the same URL, which a
        # cache is free to answer from its copy — so the form redraws the values you
        # just replaced, and the operator concludes the save failed. The Board hides
        # this because it re-fetches its columns over XHR; the editors do not.
        if path.startswith("/dashboard") and "html" in (
            response.headers.get("content-type") or ""
        ):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

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
            from ai_autopilot.config import find_bot_mention, is_bot_signed, match_command

            pr = resource.get("pullRequest") or {}
            repo = pr.get("repository") or {}
            repo_id, pr_id = repo.get("id"), pr.get("pullRequestId")
            monitor = getattr(app.state, "pr_monitor", None)
            if not (repo_id and pr_id and monitor):
                return {"error": "No pullRequest in payload"}
            content = (resource.get("comment") or {}).get("content") or ""
            if is_bot_signed(content):
                return {"ignored": "bot comment"}  # our own reply — never self-trigger
            # Only something addressed to us warrants an immediate inspection; plain
            # chatter waits for the regular poll (which ignores it anyway). An @mention
            # counts — otherwise it would sit until the next poll while a /command in the
            # same thread gets answered in a second.
            addressed = content and (
                match_command(content, c.config.comment_commands) is not None
                or find_bot_mention(content, await c.mention_identity()) is not None
            )
            if content and not addressed:
                return {"ignored": "not a command"}
            monitor.kick(repo_id, repo.get("name") or "", pr)
            log.info("webhook kicked PR inspection", pr=pr_id)
            return {"kicked": pr_id}

        work_item_id = resource.get("workItemId") or resource.get("id")
        if work_item_id is None:
            log.warning("webhook received but no workItemId found")
            return {"error": "No workItemId in payload"}
        # Never let a malformed payload (non-numeric id) raise a 500 — validate the cast.
        if not str(work_item_id).strip().isdigit():
            log.warning("webhook workItemId not numeric", value=str(work_item_id)[:64])
            return {"error": "workItemId must be numeric"}
        wid = int(work_item_id)
        c.webhook_queue.enqueue(wid)
        log.info("webhook queued work item", id=wid)
        return {"queued": wid}

    # The fleet API exists only on a central, and only once it has a token. Mounting
    # it conditionally rather than guarding an always-present route means a worker or a
    # standalone host presents no fleet surface at all to a scanner.
    if config.fleet_role == fleet.ROLE_CENTRAL:
        if config.fleet_token:
            app.include_router(fleet.create_fleet_router())
        else:
            log.error(
                "fleet_role=central but fleet_token is empty — the fleet API stays OFF. "
                "An open endpoint would hand the whole shared configuration to anyone "
                "who can reach this host."
            )

    app.include_router(create_dashboard_router())
    return app
