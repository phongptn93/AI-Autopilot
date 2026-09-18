"""Fleet mode: the central serves the shared config, workers pull it and report in.

The tests that matter most here are the negative ones. Everything this feature does is
invisible when it works — a config arrives, a row updates — so the failures worth pinning
are the silent ones: a secret that travels, a machine-specific key that gets overwritten,
an endpoint that answers without a token.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ai_autopilot import fleet
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.data import Database, FleetWorkerRepository

TOKEN = "s3cret-fleet"


def _settings(tmp_path, **over) -> Settings:
    return Settings(
        dry_run=True,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'fleet.db'}",
        **over,
    )


def _report(**over) -> fleet.WorkerReport:
    base = dict(
        name="dev-01", hostname="dev-01", version="2.39.0", profile="dev",
        tags=["dev-01-autopilot"], config_hash="", running=[], done_today=0, failed_today=0,
    )
    base.update(over)
    return fleet.WorkerReport(**base)


# ── what may travel ──────────────────────────────────────────────────────────


def test_the_shared_document_carries_no_credentials_of_its_own():
    """The document is served to every machine that knows the token, so anything in it
    is distributed by design.

    The ADO PAT, the dashboard hash and the per-tenant credentials never travel: they
    are how a machine proves it is itself. The notification channels are the deliberate
    exception — a central may tell its OWN workers where the team's notices go, which
    is a different decision from putting them in a file somebody downloads (see
    tests/test_setup_and_ownership.py).
    """
    cfg = Settings(ado_pat="PAT-12345", dashboard_auth_password_hash="pbkdf2$x",
                   webhook_secret="s")
    document, _ = fleet.config_document(cfg)
    for leaked in ("ado_pat", "dashboard_auth_password_hash", "dashboard_auth_token",
                   "webhook_secret", "config_export_password", "tenants"):
        assert leaked not in document
    assert "trigger_states" in document      # …but the shared settings ARE there


def test_the_document_never_carries_the_fleet_wiring_itself():
    """A worker that applied fleet_role/fleet_central_url from the central would become a
    second central pointed at itself, and the token would ride along with it."""
    cfg = Settings(fleet_role="central", fleet_token=TOKEN, fleet_central_url="http://vm")
    document, _ = fleet.config_document(cfg)
    assert [k for k in document if k.startswith("fleet_")] == []


def test_the_hash_ignores_key_order():
    """Two saves of the same settings must fingerprint identically, or every worker
    'resyncs' a document it already has, forever."""
    a = fleet.config_hash({"a": 1, "b": [1, 2]})
    b = fleet.config_hash({"b": [1, 2], "a": 1})
    assert a == b and len(a) == 16


def test_a_changed_setting_changes_the_hash():
    assert fleet.config_hash({"a": 1}) != fleet.config_hash({"a": 2})


# ── what a worker will accept ────────────────────────────────────────────────


def test_the_worker_keeps_its_own_tag_even_if_the_central_sends_one():
    """Filtering on the receiving side is the point: a central that is compromised or
    simply misconfigured still cannot rewrite this machine's identity."""
    kept = fleet.strip_local(
        {"trigger_tag": "central-autopilot", "ado_pat": "PAT", "workspace_directory": "C:/x",
         "max_revisions": 6},
        local_keys=[],
    )
    assert kept == {"max_revisions": 6}


def test_a_worker_declared_key_is_left_alone():
    kept = fleet.strip_local(
        {"sdlc_profile": "full", "stage_entry_tag": "run", "max_revisions": 6},
        local_keys=["sdlc_profile", "stage_entry_tag"],
    )
    assert kept == {"max_revisions": 6}


def test_an_unknown_key_is_dropped_rather_than_set():
    """A document from a NEWER central will contain settings this build has never heard
    of. Dropping them keeps an upgrade one-directional instead of crashing the worker."""
    assert fleet.strip_local({"from_the_future": 1, "max_revisions": 6}) == {
        "max_revisions": 6
    }


# ── the endpoint ─────────────────────────────────────────────────────────────


@pytest.fixture
def central(tmp_path):
    cfg = _settings(tmp_path, fleet_role="central", fleet_token=TOKEN)
    with TestClient(create_app(cfg)) as client:
        yield client


def test_a_heartbeat_without_the_token_is_refused(central):
    resp = central.post("/api/fleet/heartbeat", json=_report().model_dump(mode="json"))
    assert resp.status_code == 401


def test_a_heartbeat_with_the_wrong_token_is_refused(central):
    resp = central.post(
        "/api/fleet/heartbeat", json=_report().model_dump(mode="json"),
        headers={fleet.TOKEN_HEADER: "not-it"},
    )
    assert resp.status_code == 401


def test_a_good_heartbeat_is_answered_with_the_config(central):
    resp = central.post(
        "/api/fleet/heartbeat", json=_report().model_dump(mode="json"),
        headers={fleet.TOKEN_HEADER: TOKEN},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["config_hash"] and body["config"] is not None
    assert body["central_version"]


def test_a_worker_already_in_sync_is_not_sent_the_document_again(central):
    first = central.post(
        "/api/fleet/heartbeat", json=_report().model_dump(mode="json"),
        headers={fleet.TOKEN_HEADER: TOKEN},
    ).json()
    second = central.post(
        "/api/fleet/heartbeat",
        json=_report(config_hash=first["config_hash"]).model_dump(mode="json"),
        headers={fleet.TOKEN_HEADER: TOKEN},
    ).json()
    assert second["config"] is None and second["config_hash"] == first["config_hash"]


def test_a_nameless_worker_is_rejected(central):
    """The name is the row's key: an unnamed machine would overwrite the last unnamed
    machine, and the page would show one host standing for several."""
    resp = central.post(
        "/api/fleet/heartbeat", json=_report(name="").model_dump(mode="json"),
        headers={fleet.TOKEN_HEADER: TOKEN},
    )
    assert resp.status_code == 422


def test_a_standalone_host_presents_no_fleet_api(tmp_path):
    """Not mounted rather than mounted-and-guarded: a route that answers 401 still tells
    a scanner what this host is."""
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.post(
            "/api/fleet/heartbeat", json=_report().model_dump(mode="json"),
            headers={fleet.TOKEN_HEADER: TOKEN},
        ).status_code == 404


def test_a_central_without_a_token_does_not_serve_the_config(tmp_path):
    """An open endpoint hands the whole shared configuration to anyone who can reach the
    host, so the API stays off rather than starting unprotected."""
    with TestClient(create_app(_settings(tmp_path, fleet_role="central"))) as client:
        assert client.post(
            "/api/fleet/heartbeat", json=_report().model_dump(mode="json"),
        ).status_code == 404


# ── the central's table ──────────────────────────────────────────────────────


async def test_the_sync_clock_moves_only_when_the_worker_really_applied(tmp_path):
    """A machine that can talk but cannot apply looked permanently up to date — the exact
    failure the fleet page exists to catch."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'f.db'}")
    await db.create_all()
    repo = FleetWorkerRepository(db)

    await repo.upsert(_report(config_hash="old"), config_hash="new")
    row = (await repo.list_all())[0]
    assert row.config_synced_at is None and row.config_hash == "old"

    await repo.upsert(_report(config_hash="new"), config_hash="new")
    row = (await repo.list_all())[0]
    assert row.config_synced_at is not None


async def test_a_second_heartbeat_updates_the_row_rather_than_adding_one(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'f.db'}")
    await db.create_all()
    repo = FleetWorkerRepository(db)
    await repo.upsert(_report(version="2.39.0"), config_hash="h")
    await repo.upsert(_report(version="2.40.0"), config_hash="h")
    rows = await repo.list_all()
    assert len(rows) == 1 and rows[0].version == "2.40.0"


async def test_a_machine_is_only_forgotten_when_asked(tmp_path):
    """Silence is the finding — a worker that stops reporting must stay on the page."""
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'f.db'}")
    await db.create_all()
    repo = FleetWorkerRepository(db)
    await repo.upsert(_report(), config_hash="h")
    assert await repo.forget("dev-01") is True
    assert await repo.list_all() == []
    assert await repo.forget("dev-01") is False


# ── the page ─────────────────────────────────────────────────────────────────


async def test_the_fleet_page_marks_a_silent_machine_offline(tmp_path):
    cfg = _settings(tmp_path, fleet_role="central", fleet_token=TOKEN,
                    fleet_offline_after_minutes=30)
    with TestClient(create_app(cfg)) as client:
        container = client.app.state.container
        await container.fleet_repo.upsert(_report(name="quiet-01"), config_hash="h")
        # Backdate the beat past the threshold, the way an hour of silence would.
        async with container.database.session() as session:
            from ai_autopilot.data.entities import FleetWorker
            row = await session.get(FleetWorker, "quiet-01")
            row.last_seen = (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None)
            await session.commit()
        page = client.get("/dashboard/fleet").text
        assert "quiet-01" in page and "is-offline" in page


def test_a_worker_gets_its_own_half_of_the_fleet_page(tmp_path):
    """The central's TABLE is empty on a worker — but the worker's own question is not.

    It used to 404 for exactly that reason, which left the one machine that syncs with
    no way to see whether syncing works, and no way to make it happen: the interval was
    a floor of one minute, so every check cost a restart or a wait.
    """
    cfg = _settings(tmp_path, fleet_role="worker", fleet_token=TOKEN,
                    fleet_central_url="http://central")
    with TestClient(create_app(cfg)) as client:
        page = client.get("/dashboard/fleet")
        assert page.status_code == 200
        assert "Đồng bộ ngay" in page.text          # the button, not just a status page
        assert "http://central" in page.text        # who this machine calls
        assert "/dashboard/fleet" in client.get("/dashboard").text   # and it is linked


def test_the_fleet_page_is_absent_on_a_standalone_machine(tmp_path):
    """Neither half means anything here, and a link to an empty page reads as a bug."""
    cfg = _settings(tmp_path, fleet_role="")
    with TestClient(create_app(cfg)) as client:
        assert client.get("/dashboard/fleet").status_code == 404
        assert client.post("/dashboard/fleet/sync").status_code == 404
        assert "/dashboard/fleet" not in client.get("/dashboard").text


# ── the worker's agent ───────────────────────────────────────────────────────


def _agent(cfg, *, http=None, audit=None):
    from ai_autopilot.services.fleet_agent import FleetAgentService

    svc = FleetAgentService.__new__(FleetAgentService)
    svc._config = cfg
    svc._log = SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None,
    )
    svc._task = None
    svc._c = SimpleNamespace(http=http, audit_repo=audit, ado=SimpleNamespace(refresh=lambda: None))
    return svc


async def test_the_worker_applies_the_shared_config_but_keeps_its_own(tmp_path, monkeypatch):
    path = tmp_path / "worker.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(path))
    recorded: list[dict] = []
    cfg = Settings(
        fleet_role="worker", fleet_central_url="http://central", fleet_token=TOKEN,
        fleet_local_keys=["sdlc_profile"], sdlc_profile="qc", max_revisions=24,
    )
    audit = SimpleNamespace(record=lambda **kw: recorded.append(kw) or _done())
    svc = _agent(cfg, audit=audit)

    await svc._apply({"config": {
        "max_revisions": 6,        # shared → applied
        "sdlc_profile": "full",         # this machine's own → kept
        "ado_pat": "PAT",               # never accepted
        "trigger_tag": "central-tag",   # machine identity → kept
    }})

    assert cfg.max_revisions == 6
    assert cfg.sdlc_profile == "qc"
    assert cfg.ado_pat == ""
    import yaml
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved == {"max_revisions": 6}
    assert recorded and recorded[0]["action"] == "config.synced"


async def test_an_unchanged_document_writes_nothing(tmp_path, monkeypatch):
    """Applying identical values every beat would rewrite config.yaml and fill the audit
    trail with changes that changed nothing — which is how a real change gets lost."""
    path = tmp_path / "worker.yaml"
    monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(path))
    cfg = Settings(fleet_role="worker", max_revisions=6)
    svc = _agent(cfg, audit=SimpleNamespace(record=lambda **kw: _done()))
    await svc._apply({"config": {"max_revisions": 6}})
    assert not path.exists()


async def test_an_unreachable_central_does_not_raise(tmp_path):
    """A network blip must not cost the machine its poller."""
    import httpx

    class _Http:
        async def post(self, *a, **k):
            raise httpx.ConnectError("no route")

    cfg = Settings(fleet_role="worker", fleet_central_url="http://nope", fleet_token=TOKEN)
    svc = _agent(cfg, http=_Http())
    assert await svc.beat() is False


async def test_a_rejected_token_does_not_raise(tmp_path):
    class _Resp:
        status_code = 401

        def json(self):        # pragma: no cover — never reached on 401
            return {}

    class _Http:
        async def post(self, *a, **k):
            return _Resp()

    cfg = Settings(fleet_role="worker", fleet_central_url="http://central", fleet_token="wrong",
                   database_url=f"sqlite+aiosqlite:///{tmp_path / 'w.db'}")
    svc = _agent(cfg, http=_Http())
    svc._c.execution_repo = SimpleNamespace(search=_empty_search)
    assert await svc.beat() is False


async def test_a_worker_with_no_central_configured_stays_quiet(tmp_path):
    """Doctor says this once, loudly; the log must not say it 144 times a day."""
    svc = _agent(Settings(fleet_role="worker"))
    assert await svc.beat() is False


async def _empty_search(**kwargs):
    return [], 0


async def _done():
    return None


async def test_a_worker_and_a_central_complete_a_round_trip(tmp_path, monkeypatch):
    """The whole loop over real HTTP: the agent builds its report, the central records it
    and answers with the document, and the worker's own config.yaml comes out changed.

    Worth its weight because every unit above stubs the piece next to it — this is the
    only test where the payload the agent actually sends is the payload the endpoint
    actually parses, and a field renamed on one side fails here.
    """
    import httpx

    from ai_autopilot.services.fleet_agent import FleetAgentService

    central_cfg = _settings(tmp_path, fleet_role="central", fleet_token=TOKEN)
    central_cfg.max_revisions = 6            # what the centre wants everyone on
    with TestClient(create_app(central_cfg)) as central_client:
        worker_yaml = tmp_path / "worker.yaml"
        monkeypatch.setenv("AUTOPILOT_CONFIG_FILE", str(worker_yaml))
        worker_cfg = Settings(
            fleet_role="worker", fleet_central_url="http://central", fleet_token=TOKEN,
            fleet_worker_name="tram-01", fleet_local_keys=["sdlc_profile"],
            sdlc_profile="qc", max_revisions=24,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}",
        )
        # Speak to the central's real ASGI app over httpx, so routing, JSON encoding and
        # the token header are all exercised rather than assumed.
        transport = httpx.ASGITransport(app=central_client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://central") as http:
            container = central_client.app.state.container
            svc = FleetAgentService.__new__(FleetAgentService)
            svc._config = worker_cfg
            svc._log = SimpleNamespace(
                info=lambda *a, **k: None, warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
            )
            svc._task = None
            svc._c = SimpleNamespace(
                http=http, audit_repo=container.audit_repo,
                ado=SimpleNamespace(refresh=lambda: None),
                execution_repo=SimpleNamespace(search=_empty_search),
            )

            assert await svc.beat() is True

        # The centre now knows the machine…
        rows = await container.fleet_repo.list_all()
        assert [r.name for r in rows] == ["tram-01"]
        # …and the machine took the shared setting while keeping its own role.
        assert worker_cfg.max_revisions == 6
        assert worker_cfg.sdlc_profile == "qc"
        assert worker_cfg.fleet_role == "worker"      # identity never overwritten
        import yaml
        assert yaml.safe_load(worker_yaml.read_text(encoding="utf-8"))["max_revisions"] == 6


# ── "Đồng bộ ngay" ────────────────────────────────────────────────────────────
# The interval is a floor of one minute and usually ten, so "did my change reach the
# centre" cost either a restart or a wait. beat() has always returned whether the round
# trip worked; this is the caller it was written for.


class _StubAgent:
    """Stands in for the running FleetAgentService, with a scripted outcome."""

    def __init__(self, ok=True, applied=(), detail=""):
        self._ok, self.last_applied, self.last_detail = ok, list(applied), detail
        self.last_ok, self.last_beat_at, self.beats = None, None, 0
        self.worker_name = "worker-01"

    async def beat(self):
        self.beats += 1
        self.last_ok = self._ok
        return self._ok


@contextlib.contextmanager
def _worker_client(tmp_path, agent):
    """A worker app whose fleet agent is the stub — patched INSIDE the lifespan.

    Entering the TestClient is what starts the services, and starting them is what sets
    `app.state.fleet_agent`; patching before that (or entering twice) hands the route the
    real agent, which then beats at a central that does not exist.
    """
    cfg = _settings(tmp_path, fleet_role="worker", fleet_token=TOKEN,
                    fleet_central_url="http://central")
    with TestClient(create_app(cfg)) as client:
        client.app.state.fleet_agent = agent
        yield client


def test_pressing_sync_beats_once_and_says_nothing_changed(tmp_path):
    """The common case, and a SUCCESS — reported differently from "six settings moved",
    because one green tick for both teaches people to stop reading the banner."""
    agent = _StubAgent(ok=True, applied=(), detail="Đã khớp với trung tâm.")
    with _worker_client(tmp_path, agent) as client:
        page = client.post("/dashboard/fleet/sync")
    assert agent.beats == 1
    assert "vốn đã khớp" in page.text


def test_pressing_sync_names_what_it_brought_back(tmp_path):
    agent = _StubAgent(ok=True, applied=("max_revisions", "trigger_states"),
                       detail="Đã nhận 2 thiết lập mới.")
    with _worker_client(tmp_path, agent) as client:
        page = client.post("/dashboard/fleet/sync")
    assert "Đã đồng bộ và áp dụng ngay" in page.text
    assert "max_revisions" in page.text and "trigger_states" in page.text


def test_a_failed_sync_says_so_and_why(tmp_path):
    """"Không đồng bộ được" alone sends someone to the server log of the one machine
    they are usually not sitting at — so the reason travels with it."""
    agent = _StubAgent(ok=False, detail="Trung tâm từ chối token (401) — token 2 phía chưa khớp.")
    with _worker_client(tmp_path, agent) as client:
        page = client.post("/dashboard/fleet/sync")
    assert "Không đồng bộ được" in page.text
    assert "từ chối token (401)" in page.text


def test_sync_without_a_running_agent_is_not_silent(tmp_path):
    """fleet_role says worker but no service started: no amount of pressing fixes that,
    so the page must say it rather than report a failed heartbeat."""
    cfg = _settings(tmp_path, fleet_role="worker", fleet_token=TOKEN,
                    fleet_central_url="http://central")
    with TestClient(create_app(cfg)) as client:
        client.app.state.fleet_agent = None
        page = client.post("/dashboard/fleet/sync")
    assert "không chạy fleet agent" in page.text


async def test_beat_records_why_it_could_not_run(tmp_path):
    """The page reads these; without them a red cross has nothing to show."""
    from ai_autopilot.services.fleet_agent import FleetAgentService

    svc = _agent(Settings(fleet_role="worker", fleet_central_url="", fleet_token=""))
    svc.last_beat_at = svc.last_ok = None
    svc.last_detail, svc.last_applied = "", []
    assert await FleetAgentService.beat(svc) is False
    assert svc.last_ok is False
    assert "URL trung tâm" in svc.last_detail
    assert svc.last_beat_at is not None          # we DID try, and when
