"""A hold released by a person must release everywhere — and Resume must really resume.

Two field bugs:
- Removing ``autopilot-hold`` in ADO left the item in "Needs human" forever: the board
  trusted the persisted pipeline state over the tag, and nothing updated that state.
- Resume on /dashboard/queue removed only the hold tag. An item also wearing
  ``autopilot-done`` was set to Queued and then skipped by the poller for good.
"""

from __future__ import annotations

import tempfile
from types import SimpleNamespace

from starlette.testclient import TestClient

from ai_autopilot.board import _column_for
from ai_autopilot.config import Settings
from ai_autopilot.data import PipelineState
from ai_autopilot.models import WorkItemInfo
from tests.test_poller_agent import _poller


def _wi(id_, tags, state="Active"):
    return WorkItemInfo(id=id_, title=f"t{id_}", work_item_type="Task", state=state, tags=tags)


# ── board ───────────────────────────────────────────────────────────────────

def test_board_ignores_a_needs_human_state_once_the_hold_tag_is_gone():
    cfg = Settings()
    held = _wi(1, ["vm-autopilot", "autopilot-hold"])
    released = _wi(2, ["vm-autopilot"])
    released_done = _wi(3, ["vm-autopilot", "autopilot-done"])
    assert _column_for(held, None, cfg, persisted="Needs human") == "Needs human"
    assert _column_for(released, None, cfg, persisted="Needs human") != "Needs human"
    assert _column_for(released_done, None, cfg, persisted="Needs human") == "Done"
    # Other persisted states are still trusted as before.
    assert _column_for(released, None, cfg, persisted="In review") == "In review"


# ── poller reconcile ────────────────────────────────────────────────────────

async def test_poller_moves_released_holds_out_of_needs_human():
    svc, c = _poller(done_states=["Closed"])
    rows = [SimpleNamespace(work_item_id=i, state=PipelineState.NEEDS_HUMAN)
            for i in (1, 2, 3, 4, 5)]

    async def all_rows():
        return rows

    items = {
        1: _wi(1, ["autopilot-hold"]),                       # still held → untouched
        2: _wi(2, ["vm-autopilot"]),                         # released → carry on
        3: _wi(3, ["autopilot-done"]),                       # released as done
        4: _wi(4, ["autopilot-review"]),                     # released into review
        5: _wi(5, [], state="Closed"),                       # released, moved to a done state
    }

    async def by_ids(ids):
        return [items[i] for i in ids]

    c.state_repo.all = all_rows
    c.ado.get_work_items_by_ids = by_ids
    svc._processed[2] = object()                             # seen this process lifetime
    await svc._reconcile_released_holds()
    got = dict(c.state_repo.calls)
    assert 1 not in got
    assert got[2] == PipelineState.QUEUED
    assert got[3] == PipelineState.DONE
    assert got[4] == PipelineState.IN_REVIEW
    assert got[5] == PipelineState.DONE
    assert 2 not in svc._processed and 2 in c.retry_policy.successes   # fresh run


async def test_poller_forget_clears_dedup_and_retry_budget():
    svc, c = _poller()
    svc._processed[7] = object()
    svc.forget(7)
    assert 7 not in svc._processed and c.retry_policy.successes == [7]


# ── Resume ──────────────────────────────────────────────────────────────────

class _Ado:
    def __init__(self, tags):
        self.tags = list(tags)
        self.removed: list[str] = []
        self.added: list[str] = []

    async def get_work_item(self, iid):
        return _wi(iid, self.tags)

    async def remove_tag(self, iid, tag):
        self.removed.append(tag)
        self.tags = [t for t in self.tags if t != tag]
        return True

    async def add_tag(self, iid, tag):
        self.added.append(tag)
        return True

    async def update_state(self, iid, state):
        return True


def test_resume_clears_every_outcome_tag_and_forgets_the_item():
    from ai_autopilot.app import create_app

    d = tempfile.mkdtemp()
    s = Settings(database_url=f"sqlite+aiosqlite:///{d}/x.db", trigger_tag="vm-autopilot")
    with TestClient(create_app(s)) as client:
        c = client.app.state.container
        c.ado = _Ado(["vm-autopilot", "autopilot-done", "autopilot-hold", "customer-tag"])
        forgotten: list[int] = []
        client.app.state.poller = SimpleNamespace(forget=forgotten.append)
        r = client.post("/dashboard/queue/resume", data={"ids": "9447"}, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert sorted(c.ado.removed) == ["autopilot-done", "autopilot-hold"]
        assert "customer-tag" in c.ado.tags and "vm-autopilot" in c.ado.tags
        assert forgotten == [9447]
