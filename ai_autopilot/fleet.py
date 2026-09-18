"""Fleet mode — one central VM holds the shared configuration, worker machines pull it.

Every machine already runs a complete autopilot: its own trigger tag (``<hostname>-
autopilot``), its own role, its own assignee. What it did not have was a centre. Shared
settings — trigger states, the role relay, alert thresholds — had to be edited by hand on
each host or passed around as an exported YAML, and nobody could say which host was
running which version of it, or what any of them were doing right now.

Two rules shape everything here:

**Connectivity goes one way.** The worker calls the central; the central never calls the
worker. A developer's machine sits behind NAT, sleeps, and changes IP — anything that
needs to dial INTO it works in a demo and not in an office.

**The central never sends secrets, and the worker never accepts machine-specific keys.**
The document is built by ``settings_form.export_settings`` (which already strips the ADO
PAT, SMTP/Zalo tokens, ``trigger_tag``, ``workspaces``, ``repos``, ``database_url``), and
the worker strips them AGAIN on arrival along with whatever it declared in
``fleet_local_keys``. Filtering on both ends is the point: a compromised or misconfigured
centre still cannot write a PAT, a filesystem path, or another machine's tag onto a host.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

import yaml
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ai_autopilot.dashboard import settings_form
from ai_autopilot.logging_config import get_logger

_log = get_logger("fleet")

# The header a worker authenticates with. A header rather than a query parameter so the
# token does not land in access logs or browser history.
TOKEN_HEADER = "x-fleet-token"

ROLE_CENTRAL = "central"
ROLE_WORKER = "worker"


class RunningRun(BaseModel):
    """One run the worker has in flight right now — what "in flight" means on /now."""

    id: int = 0
    title: str = ""
    role: str = ""
    skill: str = ""
    elapsed: int = 0        # seconds since it started


class WorkerReport(BaseModel):
    """What a worker says about itself on every heartbeat.

    Deliberately small and derived: everything here can be recomputed on the next beat,
    so a lost report costs nothing and the central holds no state the worker cannot
    restate. ``config_hash`` is what turns the heartbeat into a sync — see
    :func:`config_hash`.
    """

    name: str = ""
    hostname: str = ""
    version: str = ""
    profile: str = ""                       # the role this machine runs
    tags: list[str] = Field(default_factory=list)   # its OWN tags (trigger, run-now…)
    config_hash: str = ""                   # hash of the central document it last applied
    running: list[RunningRun] = Field(default_factory=list)
    done_today: int = 0
    failed_today: int = 0
    # "Prove this URL and token, do not enrol me." The setup wizard's "test it now"
    # button has to send a REAL heartbeat — reachability proves nothing about whether
    # the token matches — but the machine pressing it is usually not configured yet, so
    # the central filed a worker named after the probe itself. Every install left a
    # phantom `setup-check` machine on the fleet page, permanently offline and
    # permanently "config lệch", which somebody then had to work out and delete.
    probe: bool = False


class KnowledgeLine(BaseModel):
    """One piece of knowledge crossing between a worker and the centre."""

    key: str = ""        # normalised text — the identity two machines agree on
    text: str = ""
    repo: str = ""
    source: str = "learned"
    count: int = 1


class KnowledgeExchange(BaseModel):
    """A worker's contribution going up. The reply carries what may come back down."""

    worker: str = ""
    items: list[KnowledgeLine] = Field(default_factory=list)


class KnowledgeResponse(BaseModel):
    """What the centre is willing to hand back — approved lines only.

    Deliberately NOT part of the config document. Three reasons, and each of them
    alone would be enough: the document is a whole-snapshot with one hash while
    knowledge is a growing set; the document travels one way while knowledge has to
    come back UP from the machines that learn it; and a lesson can quote code or a
    customer's name, so it answers to a different disclosure rule than a setting does.
    """

    items: list[KnowledgeLine] = Field(default_factory=list)
    accepted: int = 0        # how many of the contribution were new to the centre
    pending: int = 0         # drafts waiting for a human there — shown on the worker


class SyncResponse(BaseModel):
    """The central's answer: the shared configuration, when it differs.

    ``config`` is omitted when the worker's hash already matches — the common case, every
    beat of every hour. Sending it anyway would work, but it turns an idle fleet into a
    stream of full configuration documents and makes "did anything change?" unanswerable
    from a log.
    """

    config_hash: str = ""
    config: dict[str, Any] | None = None
    # Echoed back so a worker pointed at the wrong host (or at a machine that is not a
    # central at all) fails loudly instead of quietly syncing with nothing.
    central_version: str = ""


def version_tuple(version: str) -> tuple[int, ...]:
    """A version string as comparable numbers. Unparseable → ``()``, which sorts first.

    Deliberately forgiving: a build string this does not understand must degrade to
    "cannot tell", never to an exception on a page or in a heartbeat.
    """
    parts: list[int] = []
    for chunk in (version or "").strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_behind(theirs: str, ours: str) -> bool:
    """Is ``theirs`` an OLDER build than ``ours``?

    "Different" was the old test, and it answered the wrong question twice over: a
    worker running AHEAD of the central (mid-rollout, which is normal for an hour) was
    flagged as a problem, and nothing anywhere said which side needed the update. The
    only version drift that costs anything is a machine running code that predates the
    settings being sent to it — so that is the one worth a warning.
    """
    mine, other = version_tuple(ours), version_tuple(theirs)
    if not mine or not other:
        return False                       # cannot tell — say nothing rather than cry wolf
    return other < mine


def config_hash(document: dict[str, Any]) -> str:
    """A stable fingerprint of a configuration document.

    Hashed from YAML with sorted keys rather than from ``str(dict)``: dict ordering
    reflects insertion history, so the same settings saved twice would fingerprint
    differently and every worker would "resync" a document it already had.
    """
    body = yaml.safe_dump(document, sort_keys=True, allow_unicode=True, default_flow_style=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def config_document(config: Any) -> tuple[dict[str, Any], str]:
    """The shared document a central hands out, and its hash.

    Built from ``fleet_settings``, which is the export filter minus the notification
    channels: a central may hand its OWN workers the webhook it posts to, over an
    authenticated endpoint, to machines that already hold the shared token. The
    downloadable export still excludes them, because that file leaves the building.
    A key that must never travel at all belongs in ``settings_form.NEVER_SHARED``, not
    in a second list here that would drift from it.
    """
    document = settings_form.fleet_settings(config)
    return document, config_hash(document)


def strip_local(updates: dict[str, Any], local_keys: list[str] | None = None) -> dict[str, Any]:
    """Filter a document the worker received down to what it may actually apply.

    Three things are dropped: keys that are not Settings fields at all, keys the fleet
    filter considers unsendable (the ADO PAT, ``trigger_tag``, ``workspaces``,
    ``repos``, ``database_url``, and everything each machine answers for itself), and
    the keys this machine declared as its own in ``fleet_local_keys``.

    The second of those is the load-bearing one. The central strips them too, so this
    looks redundant — it is not: it is what makes a hostile or simply misconfigured
    central unable to write a PAT or a filesystem path onto a worker. A filter that runs
    only on the sending side protects nobody.
    """
    from ai_autopilot.config import Settings

    valid = set(Settings.model_fields)
    mine = {str(k).strip() for k in (local_keys or []) if str(k).strip()}
    return {
        k: v for k, v in (updates or {}).items()
        if k in valid and k not in settings_form.FLEET_EXCLUDE and k not in mine
    }


def create_fleet_router() -> APIRouter:
    """The central's side: one endpoint, mounted only when ``fleet_role`` is central.

    Mounted conditionally rather than always-with-a-guard so a standalone or worker
    install does not present a fleet API at all — a route that exists and answers 401 is
    still a route to attack, and it tells a scanner what this host is.
    """
    router = APIRouter(prefix="/api/fleet", tags=["fleet"])

    @router.post("/heartbeat", response_model=SyncResponse)
    async def heartbeat(
        request: Request,
        report: WorkerReport,
        x_fleet_token: str | None = Header(default=None),
    ) -> SyncResponse:
        """Record what a worker is doing, and answer with the shared configuration.

        One call does both on purpose: a worker that can report is a worker that can
        sync, so there is no state where the centre sees a machine it cannot configure.
        """
        c = request.app.state.container
        cfg = c.config
        token = (cfg.fleet_token or "").strip()
        if not token or not secrets.compare_digest((x_fleet_token or "").strip(), token):
            # Same answer for "no token configured" and "wrong token": a different one
            # would tell an unauthenticated caller whether this central is armed.
            raise HTTPException(status_code=401, detail="unauthorized")
        if not (report.name or "").strip():
            raise HTTPException(status_code=422, detail="worker name is required")

        document, digest = config_document(cfg)
        if report.probe:
            # Answered in full — the caller is checking that this host is a central and
            # that the token matches, and both of those are in the reply — but nothing
            # is recorded. A machine joins the fleet by running, not by being tested.
            _log.info("fleet heartbeat (probe, not enrolled)", worker=report.name)
            from ai_autopilot import __version__ as central_version

            return SyncResponse(config_hash=digest, central_version=central_version)
        await c.fleet_repo.upsert(report, config_hash=digest)
        _log.info(
            "fleet heartbeat", worker=report.name, version=report.version,
            running=len(report.running), in_sync=report.config_hash == digest,
        )
        from ai_autopilot import __version__ as central_version

        return SyncResponse(
            config_hash=digest,
            config=None if report.config_hash == digest else document,
            central_version=central_version,
        )

    @router.post("/knowledge", response_model=KnowledgeResponse)
    async def knowledge(
        request: Request,
        exchange: KnowledgeExchange,
        x_fleet_token: str | None = Header(default=None),
    ) -> KnowledgeResponse:
        """Pool what one machine has learned, and hand back what the fleet may know.

        Contribution and distribution in one call, like the heartbeat: a machine that
        can contribute is a machine that can be told, so there is no state where the
        centre has knowledge it cannot deliver.

        Nothing crosses back until a human at the centre approved it — or until
        ``fleet_knowledge_auto_promote`` separate machines independently reported the
        same thing, which is corroboration rather than a guess. Auto-merging in both
        directions would let one machine's wrong lesson reach every other machine on
        the next beat, and a wrong lesson is re-taught on every single run.
        """
        c = request.app.state.container
        cfg = c.config
        token = (cfg.fleet_token or "").strip()
        if not token or not secrets.compare_digest((x_fleet_token or "").strip(), token):
            raise HTTPException(status_code=401, detail="unauthorized")
        if not (exchange.worker or "").strip():
            raise HTTPException(status_code=422, detail="worker name is required")
        repo = getattr(c, "fleet_knowledge_repo", None)
        if repo is None:                       # central without the store — nothing to do
            return KnowledgeResponse()

        accepted = await repo.contribute(
            [item.model_dump() for item in exchange.items],
            origin=exchange.worker.strip(),
            auto_promote=max(0, int(cfg.fleet_knowledge_auto_promote or 0)),
        )
        approved = await repo.approved()
        drafts = await repo.list_all(status="draft")
        _log.info(
            "fleet knowledge exchange", worker=exchange.worker,
            received=len(exchange.items), new=accepted,
            serving=len(approved), pending=len(drafts),
        )
        return KnowledgeResponse(
            items=[
                KnowledgeLine(key=row.key, text=row.text, repo=row.repo,
                              source="fleet", count=row.occurrences)
                for row in approved
            ],
            accepted=accepted, pending=len(drafts),
        )

    return router
