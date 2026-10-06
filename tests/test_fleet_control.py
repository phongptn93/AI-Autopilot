"""Fleet remote control: commands, health, dispatch, offline alerts.

The failures worth pinning are the quiet ones: a command delivered twice, a worker
closing another worker's command, a late report resurrecting an expired command, a
paused machine still picked for dispatch, an offline alert repeated every minute.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ai_autopilot import fleet
from ai_autopilot.app import create_app
from ai_autopilot.config import Settings
from ai_autopilot.data import Database, FleetCommandRepository, FleetWorkerRepository

TOKEN = "s3cret-fleet"
H = {fleet.TOKEN_HEADER: TOKEN}


def _settings(tmp_path, **over) -> Settings:
    return Settings(dry_run=True, database_url=f"sqlite+aiosqlite:///{tmp_path / 'f.db'}", **over)


def _report(**over) -> fleet.WorkerReport:
    base = dict(name="dev-01", hostname="dev-01", version="2.39.0", profile="dev")
    base.update(over)
    return fleet.WorkerReport(**base)


async def _db(tmp_path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    await db.create_all()
    return db


# ── the queue ────────────────────────────────────────────────────────────────


async def test_a_command_is_delivered_once(tmp_path):
    repo = FleetCommandRepository(await _db(tmp_path))
    cid = await repo.enqueue("dev-01", fleet.CMD_PAUSE)
    first = await repo.take_pending("dev-01")
    assert [c.id for c in first] == [cid]
    assert await repo.take_pending("dev-01") == []


async def test_only_the_addressed_worker_can_close_a_command(tmp_path):
    repo = FleetCommandRepository(await _db(tmp_path))
    cid = await repo.enqueue("dev-01", fleet.CMD_PAUSE)
    await repo.take_pending("dev-01")
    assert await repo.finish("dev-02", cid, True) is False
    assert await repo.finish("dev-01", cid, True, "ok") is True
    assert (await repo.recent("dev-01"))[0].status == "done"


async def test_a_late_report_does_not_resurrect_an_expired_command(tmp_path):
    repo = FleetCommandRepository(await _db(tmp_path))
    old = datetime.now(UTC) - timedelta(hours=3)
    cid = await repo.enqueue("dev-01", fleet.CMD_SYNC, now=old)
    assert await repo.expire(60) == 1
    assert await repo.finish("dev-01", cid, True) is False
    assert (await repo.recent("dev-01"))[0].status == "expired"


async def test_a_delivered_command_cannot_be_cancelled(tmp_path):
    repo = FleetCommandRepository(await _db(tmp_path))
    cid = await repo.enqueue("dev-01", fleet.CMD_PAUSE)
    await repo.take_pending("dev-01")
    assert await repo.cancel(cid) is False


# ── the endpoint ─────────────────────────────────────────────────────────────


@pytest.fixture
def central(tmp_path):
    cfg = _settings(tmp_path, fleet_role="central", fleet_token=TOKEN)
    with TestClient(create_app(cfg)) as client:
        yield client


def test_the_command_endpoint_needs_the_token(central):
    assert central.post("/api/fleet/commands", json={"worker": "dev-01"}).status_code == 401


def test_a_worker_collects_its_commands_and_reports_back(central):
    central.post("/api/fleet/heartbeat", json=_report().model_dump(mode="json"), headers=H)
    page = central.post("/dashboard/fleet/command", data={"name": "dev-01", "kind": "pause"},
                        follow_redirects=False)
    assert page.status_code == 303
    got = central.post("/api/fleet/commands", json={"worker": "dev-01"}, headers=H).json()
    assert [c["kind"] for c in got["commands"]] == ["pause"]
    cid = got["commands"][0]["id"]
    again = central.post("/api/fleet/commands", headers=H, json={
        "worker": "dev-01", "results": [{"id": cid, "ok": True, "detail": "paused"}],
    }).json()
    assert again["commands"] == []
    html = central.get("/dashboard/fleet").text
    assert "Lệnh gần đây" in html and "paused" in html


def test_pressing_a_button_twice_queues_one_command(central):
    central.post("/api/fleet/heartbeat", json=_report().model_dump(mode="json"), headers=H)
    for _ in range(2):
        central.post("/dashboard/fleet/command", data={"name": "dev-01", "kind": "sync"},
                     follow_redirects=False)
    got = central.post("/api/fleet/commands", json={"worker": "dev-01"}, headers=H).json()
    assert len(got["commands"]) == 1


def test_a_command_for_an_unknown_machine_is_refused(central):
    central.post("/dashboard/fleet/command", data={"name": "ghost", "kind": "pause"},
                 follow_redirects=False)
    got = central.post("/api/fleet/commands", json={"worker": "ghost"}, headers=H).json()
    assert got["commands"] == []


def test_dispatch_picks_the_freest_machine(central):
    busy = _report(name="busy", running=[fleet.RunningRun(id=1)],
                   health=fleet.WorkerHealth(poller="running", capacity=1))
    free = _report(name="free", health=fleet.WorkerHealth(poller="running", capacity=2))
    for r in (busy, free):
        central.post("/api/fleet/heartbeat", json=r.model_dump(mode="json"), headers=H)
    central.post("/dashboard/fleet/dispatch", data={"item_id": "#4242", "name": "auto"},
                 follow_redirects=False)
    got = central.post("/api/fleet/commands", json={"worker": "free"}, headers=H).json()
    assert got["commands"] == [{"id": got["commands"][0]["id"], "kind": "run_item",
                                "args": {"id": 4242}}]


def test_the_page_shows_health_and_the_overview(central):
    rep = _report(health=fleet.WorkerHealth(poller="paused", disk_free_gb=1.5, capacity=2,
                                             last_error="boom", fail_streak=3))
    central.post("/api/fleet/heartbeat", json=rep.model_dump(mode="json"), headers=H)
    html = central.get("/dashboard/fleet").text
    for needle in ("Online", "Tạm dừng", "1.5 GB", "boom", "3 run lỗi liên tiếp", "Giao việc"):
        assert needle in html


# ── dispatch choice ──────────────────────────────────────────────────────────


def _w(name, *, online=True, poller="running", running=0, cap=1, profile="dev", accepts=True):
    return {"name": name, "online": online, "profile": profile, "running": [{}] * running,
            "health": {"poller": poller, "capacity": cap, "accepts_commands": accepts}}


def test_pick_skips_machines_that_cannot_act():
    workers = [_w("off", online=False), _w("paused", poller="paused"),
               _w("draining", poller="draining"), _w("deaf", accepts=False)]
    assert fleet.pick_worker(workers) is None


def test_pick_prefers_the_asked_role_then_the_lowest_load():
    workers = [_w("dev-idle", profile="dev"), _w("qc-busy", profile="qc", running=1, cap=2),
               _w("qc-idle", profile="qc", cap=2)]
    assert fleet.pick_worker(workers, "qc")["name"] == "qc-idle"
    assert fleet.pick_worker(workers)["name"] in ("dev-idle", "qc-idle")


def test_load_is_relative_to_capacity():
    workers = [_w("small", running=1, cap=1), _w("big", running=1, cap=4)]
    assert fleet.pick_worker(workers)["name"] == "big"


# ── offline alerts ───────────────────────────────────────────────────────────


class _Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, message):
        self.sent.append(message.heading)


async def test_an_offline_machine_is_announced_once_and_its_return_too(tmp_path):
    from ai_autopilot.services.fleet_watch import FleetWatchService

    db = await _db(tmp_path)
    workers = FleetWorkerRepository(db)
    notifier = _Notifier()
    cfg = Settings(fleet_role="central", fleet_offline_after_minutes=30)
    c = SimpleNamespace(config=cfg, fleet_repo=workers,
                        fleet_command_repo=FleetCommandRepository(db), notifier=notifier)
    old = datetime.now(UTC) - timedelta(hours=2)
    await workers.upsert(_report(), now=old)
    watch = FleetWatchService(c)

    assert await watch.tick() == ["dev-01"]
    assert await watch.tick() == []                     # once per episode
    assert len(notifier.sent) == 1 and "offline" in notifier.sent[0]
    assert await workers.upsert(_report()) is True      # the beat that ends it
    assert await workers.upsert(_report()) is False     # …and only that one


# ── the worker side ──────────────────────────────────────────────────────────


def _agent(cfg, poller=None):
    from ai_autopilot.services.fleet_agent import FleetAgentService

    svc = FleetAgentService.__new__(FleetAgentService)
    svc._config = cfg
    svc._log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                               debug=lambda *a, **k: None, error=lambda *a, **k: None)
    svc._task = svc._cmd_task = None
    svc._poller = poller
    return svc


async def test_pause_and_resume_act_on_the_poller():
    poller = SimpleNamespace(paused=False, paused_reason="", draining=False)
    svc = _agent(Settings(fleet_role="worker"), poller)
    ok, _ = await svc.execute(fleet.Command(id=1, kind="pause", args={"reason": "deploy"}))
    assert ok and poller.paused and poller.paused_reason == "deploy"
    assert (await svc.build_health()).poller == fleet.POLLER_PAUSED
    ok, _ = await svc.execute(fleet.Command(id=2, kind="resume"))
    assert ok and not poller.paused


async def test_a_worker_that_refuses_commands_refuses_them_all():
    poller = SimpleNamespace(paused=False, paused_reason="", draining=False)
    svc = _agent(Settings(fleet_role="worker", fleet_accept_commands=False), poller)
    ok, detail = await svc.execute(fleet.Command(id=1, kind="pause"))
    assert not ok and not poller.paused and "Nhận lệnh" in detail


async def test_an_unknown_command_is_refused_not_guessed():
    svc = _agent(Settings(fleet_role="worker"))
    ok, detail = await svc.execute(fleet.Command(id=1, kind="format_disk"))
    assert not ok and "không được" in detail


async def test_a_remote_update_respects_the_local_switch():
    svc = _agent(Settings(fleet_role="worker", fleet_accept_remote_update=False))
    ok, _ = await svc.execute(fleet.Command(id=1, kind="update", args={"version": "9.9.9"}))
    assert not ok


async def test_dispatch_on_a_dry_run_worker_writes_nothing():
    svc = _agent(Settings(fleet_role="worker", dry_run=True))
    ok, detail = await svc.execute(fleet.Command(id=1, kind="run_item", args={"id": 5}))
    assert not ok and "dry_run" in detail


def test_a_worker_can_pause_and_resume_itself_from_its_page(tmp_path):
    cfg = _settings(tmp_path, fleet_role="worker", fleet_token=TOKEN,
                    fleet_central_url="http://127.0.0.1:9")
    with TestClient(create_app(cfg)) as client:
        poller = client.app.state.poller
        client.post("/dashboard/fleet/local", data={"action": "pause"}, follow_redirects=False)
        assert poller.paused
        assert "TẠM DỪNG" in client.get("/dashboard/fleet").text
        client.post("/dashboard/fleet/local", data={"action": "resume"}, follow_redirects=False)
        assert not poller.paused


def test_new_fleet_keys_never_travel_in_the_shared_document():
    cfg = Settings(fleet_role="central", fleet_accept_commands=False, fleet_disk_warn_gb=1)
    document, _ = fleet.config_document(cfg)
    assert [k for k in document if k.startswith("fleet_")] == []
