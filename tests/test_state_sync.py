"""Tests for auto state transitions (merged PR → state, parent roll-up)."""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.state_sync import (
    StateSyncService,
    items_awaiting_deploy,
    parent_rollup_target,
    parse_rollup_map,
)


def _wi(wid, state="", tags=None, parent_id=None, assigned_to=None):
    return WorkItemInfo(
        id=wid, title="t", work_item_type="Task", state=state, tags=tags or [],
        parent_id=parent_id, assigned_to=assigned_to,
    )


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


async def test_merge_detects_bugfix_branch():
    # 'bugfix/' is a recognised bot-branch prefix (regression: it wasn't before).
    svc, c = _svc_shared(on_merge_state="Ready to Deploy")
    c.ado.completed = [{"pullRequestId": 9, "sourceRefName": "refs/heads/bugfix/5209-activity-log"}]
    c.ado.items[5209] = _wi(5209, state="Ready to Review", tags=["autopilot"])
    await svc._scan()
    assert (5209, "Ready to Deploy") in c.ado.states


async def test_merge_respects_assignee_filter():
    svc, c = _svc_shared(on_merge_state="Ready for Review", auto_transition_assignee="Phong")
    c.ado.completed = [{"pullRequestId": 5, "sourceRefName": "refs/heads/feature/be/42-thing"}]
    # assigned to someone else → skipped
    c.ado.items[42] = _wi(42, state="Active", tags=["autopilot"], assigned_to="Someone Else")
    await svc._scan()
    assert c.ado.states == []
    # assigned to Phong → transitioned (substring match)
    c.ado.items[42] = _wi(42, state="Active", tags=["autopilot"], assigned_to="Phong Pham")
    await svc._scan()
    assert (42, "Ready for Review") in c.ado.states


async def test_merge_skips_item_without_trigger_tag():
    svc, c = _svc_shared(on_merge_state="Ready for Review")
    c.ado.completed = [{"pullRequestId": 5, "sourceRefName": "refs/heads/feature/be/42-thing"}]
    c.ado.items[42] = _wi(42, state="Active", tags=["someone-else"])
    await svc._scan()
    assert c.ado.states == [] and c.ado.tags == []


def test_parse_rollup_map():
    assert parse_rollup_map(["Ready for Testing = Impl Done", "Active"]) == [
        ("Ready for Testing", "Impl Done"),
        ("Active", "Active"),
    ]


def test_parent_rollup_target_maps_least_advanced_child():
    pairs = parse_rollup_map(["Active = Active", "Ready for Testing = Impl Done"])
    # all children at Ready for Testing → parent Impl Done (mapped, different name)
    assert parent_rollup_target(
        [_wi(1, state="Ready for Testing"), _wi(2, state="Ready for Testing")], pairs
    ) == "Impl Done"
    # one child still Active → parent = Active (mapped from least-advanced child)
    assert parent_rollup_target([_wi(1, state="Active"), _wi(2, state="Ready for Testing")], pairs) == "Active"
    # a child outside the map → None (leave the parent alone)
    assert parent_rollup_target([_wi(1, state="New")], pairs) is None
    assert parent_rollup_target([], pairs) is None


async def test_parent_stage_rollup_maps_child_state_to_parent_state():
    svc, c = _svc_shared(parent_rollup_map=["Active = Active", "Ready for Testing = Impl Done"])
    c.ado.tagged = [_wi(1, tags=["autopilot"], parent_id=100)]
    c.ado.items[100] = _wi(100, state="Active")
    # slowest child still Active → parent Active (already) → no change
    c.ado.children[100] = [_wi(1, state="Active"), _wi(2, state="Ready for Testing")]
    await svc._scan()
    assert c.ado.states == []
    # every child now Ready for Testing → parent → Impl Done
    c.ado.children[100] = [_wi(1, state="Ready for Testing"), _wi(2, state="Ready for Testing")]
    await svc._scan()
    assert (100, "Impl Done") in c.ado.states


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
