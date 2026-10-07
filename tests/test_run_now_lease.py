"""Lease on the shared run-now tag: two machines, one run."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai_autopilot import claims
from ai_autopilot.config import SdlcRole, Settings
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.poller import AdoPollerService
from tests.test_poller_agent import _FakeAdo

# ── pure helpers ──────────────────────────────────────────────────────────────


def _cm(cid: int, text: str, bot: bool = True) -> dict:
    return {"id": cid, "text": text, "is_bot": bot}


def test_markers_round_trip_and_the_lowest_comment_id_wins():
    comments = [
        _cm(12, f"<sub>{claims.marker('runnow', 'vm-b')}</sub>"),
        _cm(11, f"<div>🔒 <sub>{claims.marker('runnow', 'vm-a')}</sub></div>"),
        _cm(13, f"<sub>{claims.marker('t-abc', 'vm-c')}</sub>"),        # another key
    ]
    found = claims.claims_in_comments(comments, "runnow")
    assert [c.machine for c in found] == ["vm-a", "vm-b"]
    assert claims.winner(found) == "vm-a"
    assert claims.winner([]) is None


def test_an_earlier_run_closes_the_episode():
    """Old claims stay in the history forever; the winner's own run comments mark the
    end of that episode, so the next re-tag starts a fresh race."""
    comments = [
        _cm(1, claims.marker("runnow", "vm-a")),
        _cm(2, "<div>🎮 Live session started</div>"),                     # the run itself
        _cm(3, "please run again", bot=False),                             # humans don't count
        _cm(4, claims.marker("runnow", "vm-b")),
    ]
    episode = claims.current_episode(comments)
    assert [c["id"] for c in episode] == [3, 4]
    assert claims.winner(claims.claims_in_comments(episode, "runnow")) == "vm-b"


def test_machine_names_and_keys_stay_parseable():
    named = SimpleNamespace(fleet_worker_name=" vm-1 ", trigger_tag="t")
    assert claims.machine_name(named) == "vm-1"
    assert claims.machine_name(SimpleNamespace(fleet_worker_name="", trigger_tag="tag")) == "tag"
    assert claims.safe_key("VM Dev 01.local") == "VM-Dev-01-local"
    assert claims.parse(claims.marker("runnow", claims.safe_key("VM Dev 01"))) == (
        "runnow", "VM-Dev-01")


# ── poller ────────────────────────────────────────────────────────────────────


class _SharedAdo(_FakeAdo):
    """One work item's comment thread shared by every machine in the test."""

    def __init__(self, thread: list[dict]):
        super().__init__()
        self.thread = thread

    async def add_comment(self, work_item_id, text):
        await super().add_comment(work_item_id, text)
        self.thread.append({"id": len(self.thread) + 100, "text": text, "is_bot": True})

    async def get_work_item_comments(self, work_item_id):
        return list(self.thread)


class _NullState:
    async def set(self, *_a, **_kw):
        return None


def _machine(name: str, ado, **cfg_over) -> tuple[AdoPollerService, list[int]]:
    cfg = Settings(stage_entry_tag="vm-autopilot-run", fleet_role="worker",
                   fleet_worker_name=name, run_now_claim_settle_seconds=0, **cfg_over)
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(ado=ado, state_repo=_NullState())
    svc._live, svc._processed = {}, {}
    quiet = lambda *a, **k: None  # noqa: E731
    svc._log = SimpleNamespace(info=quiet, warning=quiet, debug=quiet, error=quiet)
    started: list[int] = []

    async def fake_process(item):
        started.append(item.id)

    svc._process = fake_process
    return svc, started


def _tagged(item_id: int = 41, *tags: str) -> WorkItemInfo:
    return WorkItemInfo(id=item_id, title="t", work_item_type="Task",
                        state="Ready for Testing", tags=list(tags or ["vm-autopilot-run"]))


async def test_two_machines_one_item_one_run():
    thread: list[dict] = []
    ado_a, ado_b = _SharedAdo(thread), _SharedAdo(thread)
    ado_a.tagged_items = [_tagged()]
    ado_b.tagged_items = [_tagged()]
    a, started_a = _machine("vm-a", ado_a)
    b, started_b = _machine("vm-b", ado_b)

    await asyncio.gather(a._reconcile_stage_entries(), b._reconcile_stage_entries())
    await asyncio.sleep(0)

    assert started_a + started_b == [41]                  # exactly one machine runs it
    winner = "vm-a" if started_a else "vm-b"
    loser_ado = ado_b if winner == "vm-a" else ado_a
    assert loser_ado.removed == []                        # the loser leaves the tag alone
    assert any("🔒" in c["text"] for c in thread)


async def test_a_live_claim_by_another_machine_is_honoured_without_posting():
    thread = [{"id": 5, "text": claims.marker("runnow", "vm-other"), "is_bot": True}]
    ado = _SharedAdo(thread)
    ado.tagged_items = [_tagged()]
    svc, started = _machine("vm-a", ado)
    await svc._reconcile_stage_entries()
    await asyncio.sleep(0)
    assert started == [] and len(thread) == 1


async def test_unreadable_comments_mean_do_not_act():
    class _Blind(_SharedAdo):
        async def get_work_item_comments(self, work_item_id):
            return []                                       # what the client returns on error

    ado = _Blind([])
    ado.tagged_items = [_tagged()]
    svc, started = _machine("vm-a", ado)
    await svc._reconcile_stage_entries()
    await asyncio.sleep(0)
    assert started == [] and ado.removed == []


async def test_a_roles_own_tag_is_not_leased():
    ado = _SharedAdo([])
    ado.tagged_items = [_tagged(9, "vm-autopilot-run-qc")]
    svc, started = _machine("vm-a", ado, sdlc_roles={
        "qc": SdlcRole(stages=["test"], entry_tag="vm-autopilot-run-qc")})
    await svc._reconcile_stage_entries()
    await asyncio.sleep(0)
    assert started == [9] and ado.comments == []


async def test_standalone_machines_do_not_claim_unless_asked():
    ado = _SharedAdo([])
    ado.tagged_items = [_tagged()]
    svc, started = _machine("vm-a", ado, run_now_lease="auto")
    svc._config.fleet_role = ""
    await svc._reconcile_stage_entries()
    await asyncio.sleep(0)
    assert started == [41] and ado.comments == []
    assert AdoPollerService._lease_active(SimpleNamespace(
        _config=SimpleNamespace(run_now_lease="on", fleet_role="")))
    assert not AdoPollerService._lease_active(SimpleNamespace(
        _config=SimpleNamespace(run_now_lease="off", fleet_role="worker")))
