"""Tests for auto state transitions (merged PR → state, parent roll-up)."""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.state_sync import (
    StateSyncService,
    all_children_done,
    items_awaiting_deploy,
    parent_rollup_target,
)


def _wi(wid, state="", tags=None, parent_id=None):
    return WorkItemInfo(
        id=wid, title="t", work_item_type="Task", state=state, tags=tags or [], parent_id=parent_id
    )


def test_all_children_done_by_state_or_tag():
    assert all_children_done(
        [_wi(1, state="Closed"), _wi(2, state="Ready to Testing")],
        ["Closed", "Ready to Testing"], "autopilot-done",
    )
    assert not all_children_done(  # one child still active
        [_wi(1, state="Closed"), _wi(2, state="Active")], ["Closed"], "autopilot-done"
    )
    assert all_children_done([_wi(1, tags=["autopilot-done"])], [], "autopilot-done")  # done via tag
    assert not all_children_done([], ["Closed"], "autopilot-done")  # no children → not a roll-up


class _FakeAdo:
    def __init__(self):
        self.states: list[tuple[int, str]] = []
        self.tags: list[tuple[int, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.repos = [{"id": "r1"}]
        self.completed: list[dict] = []
        self.items: dict[int, WorkItemInfo] = {}
        self.children: dict[int, list[WorkItemInfo]] = {}
        self.tagged: list[WorkItemInfo] = []
        self.builds: list[dict] = []

    async def get_successful_builds(self, definition_id, branch):
        return self.builds

    async def get_repositories(self):
        return self.repos

    async def get_completed_pull_requests(self, repo_id):
        return self.completed

    async def get_work_item(self, wid):
        return self.items.get(wid)

    async def get_children(self, parent_id):
        return self.children.get(parent_id, [])

    async def get_all_tagged_work_items(self):
        return self.tagged

    async def update_state(self, wid, state):
        self.states.append((wid, state))

    async def add_tag(self, wid, tag):
        self.tags.append((wid, tag))

    async def add_comment(self, wid, content):
        self.comments.append((wid, content))


def _svc_shared(**cfg_over):
    cfg = Settings(auto_transition_enabled=True, trigger_tag="autopilot", **cfg_over)
    c = SimpleNamespace(config=cfg, ado=_FakeAdo())
    return StateSyncService(c), c


async def test_merge_transitions_work_item():
    svc, c = _svc_shared(on_merge_state="Ready for Review")
    c.ado.completed = [{"pullRequestId": 5, "sourceRefName": "refs/heads/feature/be/42-thing"}]
    c.ado.items[42] = _wi(42, state="Active", tags=["autopilot"])
    await svc._scan()
    assert (42, "Ready for Review") in c.ado.states
    assert (42, c.config.processed_tag) in c.ado.tags
    c.ado.states.clear()
    await svc._scan()               # PR already handled → no repeat
    assert c.ado.states == []


async def test_merge_skips_item_without_trigger_tag():
    svc, c = _svc_shared(on_merge_state="Ready for Review")
    c.ado.completed = [{"pullRequestId": 5, "sourceRefName": "refs/heads/feature/be/42-thing"}]
    c.ado.items[42] = _wi(42, state="Active", tags=["someone-else"])
    await svc._scan()
    assert c.ado.states == [] and c.ado.tags == []


def test_parent_rollup_target_least_advanced_child():
    stages = ["Active", "Ready for Review", "Deployed"]
    assert parent_rollup_target(
        [_wi(1, state="Ready for Review"), _wi(2, state="Ready for Review")], stages
    ) == "Ready for Review"
    # one child still Active → parent = Active (least advanced)
    assert parent_rollup_target([_wi(1, state="Active"), _wi(2, state="Deployed")], stages) == "Active"
    # a child outside the stages → None (leave the parent alone)
    assert parent_rollup_target([_wi(1, state="Ready for Review"), _wi(2, state="New")], stages) is None
    assert parent_rollup_target([], stages) is None


async def test_parent_stage_rollup_follows_least_advanced_child():
    svc, c = _svc_shared(parent_rollup_stages=["Active", "Ready for Review", "Deployed"])
    c.ado.tagged = [_wi(1, tags=["autopilot"], parent_id=100)]
    c.ado.items[100] = _wi(100, state="Active")
    # one child Active, one further along → parent stays Active (min)
    c.ado.children[100] = [_wi(1, state="Active"), _wi(2, state="Ready for Review")]
    await svc._scan()
    assert c.ado.states == []
    # every child now Ready for Review → parent advances
    c.ado.children[100] = [_wi(1, state="Ready for Review"), _wi(2, state="Ready for Review")]
    await svc._scan()
    assert (100, "Ready for Review") in c.ado.states


async def test_parent_rollup_when_all_children_done():
    svc, c = _svc_shared(parent_done_state="Resolved", done_states=["Closed"])
    c.ado.tagged = [_wi(1, tags=["autopilot"], parent_id=100)]
    c.ado.children[100] = [_wi(1, state="Closed"), _wi(2, state="Closed")]
    c.ado.items[100] = _wi(100, state="Active")
    await svc._scan()
    assert (100, "Resolved") in c.ado.states
    c.ado.states.clear()
    await svc._scan()               # idempotent
    assert c.ado.states == []


async def test_parent_rollup_skips_when_a_child_not_done():
    svc, c = _svc_shared(parent_done_state="Resolved", done_states=["Closed"])
    c.ado.tagged = [_wi(1, tags=["autopilot"], parent_id=100)]
    c.ado.children[100] = [_wi(1, state="Closed"), _wi(2, state="Active")]
    c.ado.items[100] = _wi(100, state="Active")
    await svc._scan()
    assert c.ado.states == []


def test_items_awaiting_deploy_filters_by_state():
    tagged = [_wi(1, state="Ready for Review"), _wi(2, state="Active"), _wi(3, state="ready for review")]
    assert items_awaiting_deploy(tagged, "Ready for Review") == [1, 3]  # case-insensitive
    assert items_awaiting_deploy(tagged, "") == []


async def test_deploy_marks_awaiting_items_on_new_build():
    svc, c = _svc_shared(on_merge_state="Ready for Review", on_deploy_state="Deployed", base_branch="main")
    c.ado.tagged = [
        _wi(1, state="Ready for Review", tags=["autopilot"]),
        _wi(2, state="Active", tags=["autopilot"]),  # not awaiting deploy
    ]
    c.ado.builds = [{"id": 10}]
    await svc._scan()                       # first scan → baseline, no transition
    assert c.ado.states == []
    c.ado.builds = [{"id": 11}]             # a new successful build
    await svc._scan()
    assert (1, "Deployed") in c.ado.states
    assert all(wid != 2 for wid, _ in c.ado.states)
    c.ado.states.clear()
    await svc._scan()                       # same build → idempotent
    assert c.ado.states == []


async def test_deploy_dry_run_writes_nothing():
    svc, c = _svc_shared(
        on_merge_state="Ready for Review", on_deploy_state="Deployed", base_branch="main", dry_run=True
    )
    c.ado.tagged = [_wi(1, state="Ready for Review", tags=["autopilot"])]
    c.ado.builds = [{"id": 10}]
    await svc._scan()
    c.ado.builds = [{"id": 11}]
    await svc._scan()
    assert c.ado.states == []


async def test_dry_run_writes_nothing():
    svc, c = _svc_shared(on_merge_state="Ready for Review", dry_run=True)
    c.ado.completed = [{"pullRequestId": 5, "sourceRefName": "refs/heads/feature/be/42-thing"}]
    c.ado.items[42] = _wi(42, state="Active", tags=["autopilot"])
    await svc._scan()
    assert c.ado.states == [] and c.ado.tags == []
