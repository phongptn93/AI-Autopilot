"""Queued rows nothing will ever pick up are settled instead of shown as "Sắp chạy".

Real case: items re-queued at a restart (they were In progress), then finished and
merged by people — the poll never qualified them again, so the Now page listed them as
up next for months.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.data import PipelineState
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.poller import AdoPollerService


class _States:
    def __init__(self, rows):
        self.rows = {r.work_item_id: r for r in rows}

    async def all(self):
        return list(self.rows.values())

    async def set(self, wid, state, *, title="", detail=None, pr_url=None):
        row = self.rows[wid]
        row.state, row.detail = state, detail


def _row(wid, minutes_ago, state=PipelineState.QUEUED):
    return SimpleNamespace(work_item_id=wid, state=state, detail=None, title="",
                           updated_at=datetime.now(UTC) - timedelta(minutes=minutes_ago))


def test_stale_queue_is_settled_by_the_trigger_rules():
    cfg = Settings(dry_run=True, trigger_tag="vm-autopilot", ado_project="P",
                   trigger_states=["New"], assignee_trigger_tag="")
    items = [
        WorkItemInfo(id=1, project="P", state="Closed", tags=["vm-autopilot", "autopilot-done"]),
        WorkItemInfo(id=2, project="P", state="New", tags=["vm-autopilot"]),          # eligible
        WorkItemInfo(id=3, project="P", state="Active", tags=["vm-autopilot"]),       # stuck
    ]

    async def by_ids(ids):
        return [i for i in items if i.id in ids]

    states = _States([_row(1, 90), _row(2, 90), _row(3, 90), _row(4, 90), _row(5, 2),
                      _row(6, 90, PipelineState.DONE)])
    svc = AdoPollerService.__new__(AdoPollerService)
    svc._config = cfg
    svc._c = SimpleNamespace(state_repo=states, ado=SimpleNamespace(get_work_items_by_ids=by_ids))
    svc._inflight, svc._live = set(), {}
    svc._log = SimpleNamespace(info=lambda *a, **k: None)
    asyncio.run(svc._reconcile_stale_queue())
    r = states.rows
    assert r[1].state == PipelineState.DONE                        # finished elsewhere
    assert r[2].state == PipelineState.QUEUED                      # genuinely up next
    assert r[3].state == PipelineState.NEEDS_HUMAN and "State" in r[3].detail
    assert r[4].state == PipelineState.DONE and "tracker" in r[4].detail   # gone from ADO
    assert r[5].state == PipelineState.QUEUED                      # too fresh to judge
    # rate-limited: a second call in the same 10 minutes does nothing
    r[2].state = PipelineState.QUEUED
    asyncio.run(svc._reconcile_stale_queue())
