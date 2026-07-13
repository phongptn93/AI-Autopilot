"""AI-conflict feedback: verdicts confirmed in Planning Analyze defer pairs in the poller."""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.poller import AdoPollerService


class _Ado:
    async def get_work_item_links(self, ids):
        return {}, {}                       # no BA-declared links at all

    async def get_work_items_by_ids(self, ids):
        return []

    async def add_comment(self, i, t):
        pass


class _AiRepo:
    """Returns a symmetric conflict edge 1<->2 (as if Analyze confirmed it)."""

    def __init__(self, edges, expect_min):
        self._edges, self._expect_min = edges, expect_min
        self.called_with = None

    async def related_edges(self, ids, min_score):
        self.called_with = (sorted(ids), min_score)
        idset = set(ids)
        return {a: {b for b in peers if b in idset} for a, peers in self._edges.items() if a in idset}


def _poller(cfg, ai_repo):
    c = SimpleNamespace(config=cfg, ado=_Ado(), ai_conflict_repo=ai_repo)
    return AdoPollerService(c), c


def _items():
    return [
        WorkItemInfo(id=1, title="A", work_item_type="Task"),
        WorkItemInfo(id=2, title="B", work_item_type="Task"),
    ]


async def test_ai_conflict_defers_the_pair():
    cfg = Settings(
        dependency_scheduling_enabled=True, scheduler_use_ai_conflicts=True,
        scheduler_ai_conflict_min_score=60, dry_run=True,
    )
    ai = _AiRepo({1: {2}, 2: {1}}, expect_min=60)
    poller, _ = _poller(cfg, ai)
    ready = await poller._schedule(_items())
    # Only one of the conflicting pair goes this wave; the other defers.
    assert len(ready) == 1
    assert ai.called_with == ([1, 2], 60)


async def test_feedback_off_lets_both_run():
    cfg = Settings(
        dependency_scheduling_enabled=True, scheduler_use_ai_conflicts=False, dry_run=True,
    )
    ai = _AiRepo({1: {2}, 2: {1}}, expect_min=60)
    poller, _ = _poller(cfg, ai)
    ready = await poller._schedule(_items())
    assert {i.id for i in ready} == {1, 2}          # no conflict applied
    assert ai.called_with is None                   # repo not consulted
