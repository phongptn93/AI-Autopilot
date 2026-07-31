"""Tests for auto state transitions (merged PR → state, parent roll-up)."""

from __future__ import annotations

from types import SimpleNamespace

from ai_autopilot.config import Settings
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.services.state_sync import (
    StateSyncService,
    already_at_or_past_merge,
    items_awaiting_deploy,
    parent_rollup_target,
    parse_rollup_map,
)


def _wi(wid, state="", tags=None, parent_id=None, assigned_to=None, assigned_to_email=None,
        wi_type="Task"):
    return WorkItemInfo(
        id=wid, title="t", work_item_type=wi_type, state=state, tags=tags or [],
        parent_id=parent_id, assigned_to=assigned_to, assigned_to_email=assigned_to_email,
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
        # States ADO should refuse (None = accept everything), so a test can reproduce
        # "this state doesn't exist on that work-item type" without a live project.
        self.reject_state: set[str] | None = None

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
        # Must mirror the real client's ``bool``: state_sync now treats a falsy result as
        # "ADO rejected the transition" and skips the rest of the outcome, so a fake that
        # returned None would make every transition look like a failure.
        return self.reject_state is None or state not in self.reject_state

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


async def test_merge_assignee_matches_email():
    # assignee configured as an email still matches (display name has no email in it)
    svc, c = _svc_shared(on_merge_state="Ready to Deploy", auto_transition_assignee="phong.pham@nois.vn")
    c.ado.completed = [{"pullRequestId": 9, "sourceRefName": "refs/heads/bugfix/5209-x"}]
    c.ado.items[5209] = _wi(
        5209, state="Ready to Review", tags=["autopilot"],
        assigned_to="Phong Pham (Industrial - Head of P&T)", assigned_to_email="phong.pham@nois.vn",
    )
    await svc._scan()
    assert (5209, "Ready to Deploy") in c.ado.states


async def test_merge_skips_item_without_trigger_tag():
    svc, c = _svc_shared(on_merge_state="Ready for Review")
    c.ado.completed = [{"pullRequestId": 5, "sourceRefName": "refs/heads/feature/be/42-thing"}]
    c.ado.items[42] = _wi(42, state="Active", tags=["someone-else"])
    await svc._scan()
    assert c.ado.states == [] and c.ado.tags == []


def test_already_at_or_past_merge():
    cfg = Settings(
        on_merge_state="Ready to Deploy", on_deploy_state="Ready to Testing",
        done_states=["Closed", "Resolved"],
    )
    assert already_at_or_past_merge("Ready to Deploy", cfg) is True
    assert already_at_or_past_merge("ready to testing", cfg) is True   # case-insensitive
    assert already_at_or_past_merge("Closed", cfg) is True
    assert already_at_or_past_merge("Active", cfg) is False
    assert already_at_or_past_merge("", cfg) is False


async def test_merge_does_not_pull_advanced_item_backward():
    # The bug: an item already at on_deploy_state must NOT be dragged back to
    # on_merge_state by a still-"completed" merged PR (e.g. after a restart).
    svc, c = _svc_shared(
        on_merge_state="Ready to Deploy", on_deploy_state="Ready to Testing",
        done_states=["Closed"],
    )
    c.ado.completed = [{"pullRequestId": 7, "sourceRefName": "refs/heads/feature/be/42-x"}]
    c.ado.items[42] = _wi(42, state="Ready to Testing", tags=["autopilot"])  # already deployed
    await svc._scan()
    assert c.ado.states == []            # not moved back to Ready to Deploy
    # Simulate a restart: dedup memory lost → guard must STILL protect it.
    svc._merged.clear()
    await svc._scan()
    assert c.ado.states == []


async def test_merge_still_transitions_a_fresh_item():
    # Guard must not block the legitimate forward transition.
    svc, c = _svc_shared(on_merge_state="Ready to Deploy", on_deploy_state="Ready to Testing")
    c.ado.completed = [{"pullRequestId": 8, "sourceRefName": "refs/heads/feature/be/43-x"}]
    c.ado.items[43] = _wi(43, state="Active", tags=["autopilot"])
    await svc._scan()
    assert (43, "Ready to Deploy") in c.ado.states


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
    cfg = Settings(on_merge_state="Ready for Review")
    assert items_awaiting_deploy(tagged, cfg) == [1, 3]      # case-insensitive
    assert items_awaiting_deploy(tagged, Settings()) == []   # no merge state → nothing awaits


def test_items_awaiting_deploy_uses_each_type_own_merge_state():
    """The merge state differs by type, so one shared comparison would only ever find
    the items of whichever type the flat setting happened to name."""
    cfg = Settings(work_item_flows=[
        {"name": "Dev", "types": ["Task"], "states": {"on_merge": "Ready to Deploy"}},
        {"name": "Req", "types": ["Requirement"], "states": {"on_merge": "Implement Done"}},
    ])
    tagged = [
        _wi(1, state="Ready to Deploy", wi_type="Task"),
        _wi(2, state="Implement Done", wi_type="Requirement"),
        _wi(3, state="Ready to Deploy", wi_type="Requirement"),  # not ITS merge state
    ]
    assert items_awaiting_deploy(tagged, cfg) == [1, 2]


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


# ── Regressions for the three defects per-type flows fix ──────────────────────

async def test_a_rejected_merge_state_does_not_mark_the_item_done():
    """The live defect. ``Ready to Deploy`` exists on Bug/Task but not Requirement, so
    ADO refuses the transition — yet the item was still tagged done and commented
    "autopilot marked it done". The board and the comment both lied, and the done tag
    then made the poller skip the item forever."""
    svc, c = _svc_shared(on_merge_state="Ready to Deploy")
    c.ado.reject_state = {"Ready to Deploy"}          # as ADO does for a Requirement
    c.ado.completed = [{"pullRequestId": 7, "sourceRefName": "refs/heads/feature/be/77-x"}]
    c.ado.items[77] = _wi(77, state="Active", tags=["autopilot"], wi_type="Requirement")

    await svc._scan()

    assert (77, "Ready to Deploy") in c.ado.states     # it was attempted…
    assert c.ado.tags == []                            # …but nothing was marked done
    assert c.ado.comments == []                        # and nothing claimed it was
    assert 7 not in svc._merged                        # so a fixed flow gets another go

    # Once the flow names a state this type has, the same PR completes properly.
    c.config.work_item_flows = [
        {"name": "Req", "types": ["Requirement"], "states": {"on_merge": "Implement Done"}},
    ]
    await svc._scan()
    assert (77, "Implement Done") in c.ado.states
    assert (77, c.config.processed_tag) in c.ado.tags
    assert 7 in svc._merged


async def test_merge_state_is_resolved_per_work_item_type():
    """Two types, one scan, different target states — what a single flat field cannot do."""
    svc, c = _svc_shared(on_merge_state="Resolved", work_item_flows=[
        {"name": "Dev", "types": ["Task", "Bug"], "states": {"on_merge": "Ready to Deploy"}},
        {"name": "Req", "types": ["Requirement"], "states": {"on_merge": "Implement Done"}},
    ])
    c.ado.completed = [
        {"pullRequestId": 1, "sourceRefName": "refs/heads/feature/be/11-a"},
        {"pullRequestId": 2, "sourceRefName": "refs/heads/feature/be/22-b"},
        {"pullRequestId": 3, "sourceRefName": "refs/heads/feature/be/33-c"},
    ]
    c.ado.items[11] = _wi(11, state="Active", tags=["autopilot"], wi_type="Task")
    c.ado.items[22] = _wi(22, state="Active", tags=["autopilot"], wi_type="Requirement")
    c.ado.items[33] = _wi(33, state="Active", tags=["autopilot"], wi_type="Feature")  # ungrouped

    await svc._scan()

    assert (11, "Ready to Deploy") in c.ado.states
    assert (22, "Implement Done") in c.ado.states
    assert (33, "Resolved") in c.ado.states            # falls back to the flat setting


async def test_per_type_flow_can_override_the_done_tag_and_silence_the_comment():
    """The configurable actions, end to end."""
    svc, c = _svc_shared(on_merge_state="Resolved", work_item_flows=[
        {"name": "Req", "types": ["Requirement"],
         "states": {"on_merge": "Implement Done"},
         "tags": {"on_merge": "req-implemented"},
         "comment": {"on_merge": False}},
    ])
    c.ado.completed = [{"pullRequestId": 4, "sourceRefName": "refs/heads/feature/be/44-d"}]
    c.ado.items[44] = _wi(44, state="Active", tags=["autopilot"], wi_type="Requirement")

    await svc._scan()

    assert (44, "req-implemented") in c.ado.tags
    assert (44, c.config.processed_tag) not in c.ado.tags
    assert c.ado.comments == []


async def test_advanced_item_check_is_per_type():
    """The don't-go-backward guard must compare against THIS type's states, or a
    Requirement already at 'Testing' looks un-advanced and gets yanked back."""
    cfg = Settings(work_item_flows=[
        {"name": "Req", "types": ["Requirement"],
         "states": {"on_merge": "Implement Done", "on_deploy": "Testing"}},
    ], on_merge_state="Ready to Deploy", on_deploy_state="Ready to Testing")
    assert already_at_or_past_merge("Testing", cfg, "Requirement") is True
    assert already_at_or_past_merge("Ready to Testing", cfg, "Requirement") is False
    assert already_at_or_past_merge("Ready to Testing", cfg, "Task") is True


async def test_rollup_names_the_child_states_missing_from_the_map():
    """An incomplete map silently held the roll-up — indistinguishable from "the parent
    is already up to date". The held state must now say which line is missing.

    Asserted against the service's own logger rather than captured output: structlog is
    configured globally, so whether a record reaches stdout, stderr or stdlib logging
    depends on which test ran first — this passed in isolation and failed in a full run
    twice over before being pinned down. The claim is that the reason is logged with the
    offending states, which the spy checks directly.
    """
    svc, c = _svc_shared(parent_rollup_map=["Active = Active"])
    c.ado.tagged = [_wi(1, tags=["autopilot"], parent_id=100)]
    c.ado.items[100] = _wi(100, state="Active", wi_type="Requirement")
    c.ado.children[100] = [_wi(1, state="Active"), _wi(2, state="Ready to Testing")]

    records: list[tuple[str, dict]] = []
    svc._log = SimpleNamespace(
        info=lambda event, **kw: records.append((event, kw)),
        warning=lambda event, **kw: records.append((event, kw)),
        error=lambda event, **kw: records.append((event, kw)),
    )

    await svc._scan()

    assert c.ado.states == []                      # correctly held (unknown child stage)
    held = [kw for event, kw in records if "child states not in the map" in event]
    assert len(held) == 1
    assert held[0]["states"] == ["Ready to Testing"]
    assert held[0]["id"] == 100


async def test_rollup_uses_the_parent_flow_map():
    """Roll-up lines belong to the parent's flow: the state they set is the parent's."""
    svc, c = _svc_shared(parent_rollup_map=["Active = Wrong"], work_item_flows=[
        {"name": "Req", "types": ["Requirement"],
         "rollup": ["Active = Active", "Ready to Testing = Implement Done"]},
    ])
    c.ado.tagged = [_wi(1, tags=["autopilot"], parent_id=100)]
    c.ado.items[100] = _wi(100, state="Active", wi_type="Requirement")
    c.ado.children[100] = [_wi(1, state="Ready to Testing"), _wi(2, state="Ready to Testing")]

    await svc._scan()

    assert (100, "Implement Done") in c.ado.states
    assert not any(state == "Wrong" for _, state in c.ado.states)
