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
    assert router.route(item) == "/implement-task-be 2152"


def test_route_bug(router):
    item = router.classify(_item(title="oops", work_item_type="Bug"))
    item.id = 99
    assert router.route(item) == "/bugfix-workflow 99"


def test_route_unknown_returns_none(router):
    assert router.route(_item(title="random")) is None


def test_route_database_returns_none(router):
    # DatabaseTask has no skill mapping → unroutable (parity with .NET).
    item = router.classify(_item(title="[DB] migration"))
    assert router.route(item) is None
