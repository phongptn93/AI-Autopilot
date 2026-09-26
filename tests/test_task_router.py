"""Classification & routing tests (ported from the .NET TaskRouterTests)."""

from __future__ import annotations

import pytest

from ai_autopilot.models import TaskCategory, WorkItemInfo
from ai_autopilot.routing import TaskRouter


@pytest.fixture
def router() -> TaskRouter:
    return TaskRouter()


def _item(title="", work_item_type="Task", tags=None) -> WorkItemInfo:
    return WorkItemInfo(id=1, title=title, work_item_type=work_item_type, tags=tags or [])


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("[BE] Add login API", TaskCategory.BACKEND_TASK),
        ("[FE] Build dashboard", TaskCategory.FRONTEND_TASK),
        ("[DB] Add users table", TaskCategory.DATABASE_TASK),
        ("[QC] Test checkout", TaskCategory.TEST_TASK),
        ("[TEST] Smoke suite", TaskCategory.TEST_TASK),
    ],
)
def test_classify_by_title_prefix(router, title, expected):
    assert router.classify(_item(title=title)).category is expected


def test_bug_type_takes_priority_over_title(router):
    item = _item(title="[FE] crash on submit", work_item_type="Bug")
    assert router.classify(item).category is TaskCategory.BUG


@pytest.mark.parametrize("wit", ["User Story", "Requirement", "Feature"])
def test_requirement_types(router, wit):
    assert router.classify(_item(title="Something", work_item_type=wit)).category is (
        TaskCategory.REQUIREMENT
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Add new endpoint for orders", TaskCategory.BACKEND_TASK),
        ("Create order controller", TaskCategory.BACKEND_TASK),
        ("Build the checkout component", TaskCategory.FRONTEND_TASK),
        ("New angular page", TaskCategory.FRONTEND_TASK),
    ],
)
def test_classify_task_by_keyword(router, title, expected):
    assert router.classify(_item(title=title, work_item_type="Task")).category is expected


def test_classify_by_tag(router):
    item = _item(title="ambiguous", tags=["backend"])
    assert router.classify(item).category is TaskCategory.BACKEND_TASK


def test_unknown_when_nothing_matches(router):
    assert router.classify(_item(title="random work")).category is TaskCategory.UNKNOWN


def test_route_known_categories(router):
    item = router.classify(_item(title="[BE] thing"))
    item.id = 2152
    assert router.route(item) == "/crud-full-stack 2152"


def test_route_frontend(router):
    item = router.classify(_item(title="[FE] thing"))
    item.id = 2153
    assert router.route(item) == "/fe-module 2153"


def test_route_bug(router):
    item = router.classify(_item(title="oops", work_item_type="Bug"))
    item.id = 99
    assert router.route(item) == "/bugfix-workflow 99"


def test_route_unknown_returns_none(router):
    assert router.route(_item(title="random")) is None


def test_route_database(router):
    # DatabaseTask routes to the sql-migration skill.
    item = router.classify(_item(title="[DB] migration"))
    item.id = 77
    assert router.route(item) == "/sql-migration 77"


# ── Task room: the PR decoration fan-out ─────────────────────────────────────


class _RepoCountingAdo:
    """Counts how often the repo list is asked for."""

    def __init__(self):
        self.repo_calls = 0

    async def get_repositories(self):
        self.repo_calls += 1
        return [{"id": "guid-1", "name": "repo1"}]

    async def get_pull_request(self, _repo_id, pr_id, project=""):
        return {"status": "active", "title": f"PR {pr_id}",
                "sourceRefName": "refs/heads/feature/1-a"}


async def test_task_room_asks_for_the_repo_list_once_not_once_per_pr():
    """`_decorate_from_ado` fetched the repo list itself, i.e. INSIDE the per-PR loop:
    a task with several PRs asked Azure DevOps for the same list once per PR, only to
    read one entry out of a dict."""
    from types import SimpleNamespace

    from ai_autopilot.config import Settings
    from ai_autopilot.services.task_room import PrView, TaskRoom, TaskRoomService

    ado = _RepoCountingAdo()
    c = SimpleNamespace(ado=ado, config=Settings())
    svc = TaskRoomService(c)

    org = "https://dev.azure.com/org"
    room = TaskRoom(work_item_id=1)
    room.runs = [
        SimpleNamespace(
            pr_url=f"{org}/proj/_git/repo1/pullrequest/{n}",
            pr_urls="[]", branch_name="feature/1-a",
        )
        for n in (11, 12, 13)
    ]
    views = await svc._pull_requests(room, Settings(), with_diff=False)
    # Assert the loop actually ran: repo_calls == 1 would also hold vacuously if no
    # PR URL parsed, which would make this test prove nothing.
    assert len(views) == 3
    assert all(isinstance(v, PrView) and v.status == "active" for v in views)
    assert ado.repo_calls == 1          # once for the page, not once per PR


async def test_task_room_survives_an_unreachable_repo_list():
    """A missing label must cost a label, not the page."""
    from types import SimpleNamespace

    from ai_autopilot.config import Settings
    from ai_autopilot.services.task_room import TaskRoomService

    class _Boom:
        async def get_repositories(self):
            raise RuntimeError("ADO throttled")

    svc = TaskRoomService(SimpleNamespace(ado=_Boom(), config=Settings()))
    assert await svc._repo_guids() == {}
