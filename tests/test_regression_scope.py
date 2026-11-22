"""Regression scope: earlier work that changed the same files / modules, and its cases."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ai_autopilot import regression_scope as rs
from ai_autopilot.config import Settings
from ai_autopilot.models import ExecutionResult, WorkItemInfo


def _rec(wid, files, title=""):
    return SimpleNamespace(work_item_id=wid, title=title or f"item {wid}",
                           files_changed=json.dumps(files))


HISTORY = rs.history_from_records([
    _rec(1, ["api/src/orders/service.py", "api/src/orders/model.py"]),
    _rec(1, ["api/src/orders/repo.py"]),                     # same item, second run: merged
    _rec(2, ["api/src/orders/other.py"]),                    # same module only
    _rec(3, ["web/src/orders/page.ts"]),                     # another repo's src/orders
    _rec(4, ["api/docs/readme.md"]),
    SimpleNamespace(work_item_id=5, title="", files_changed="not json"),
])


def test_history_merges_runs_per_item_and_skips_garbage():
    by_id = {h.work_item_id: h for h in HISTORY}
    assert set(by_id) == {1, 2, 3, 4}
    assert len(by_id[1].files) == 3


def test_same_file_ranks_above_same_module_and_repos_stay_apart():
    found = rs.related(["api/src/orders/service.py"], HISTORY, exclude=99)
    assert [r.work_item_id for r in found] == [1, 2]
    assert found[0].shared_files == ["api/src/orders/service.py"]
    assert found[1].shared_modules == ["src/orders"] and found[1].shared_files == []


def test_the_item_itself_is_excluded_and_nothing_shared_is_empty():
    assert [r.work_item_id for r in rs.related(["api/src/orders/service.py"], HISTORY,
                                               exclude=1)] == [2]
    assert rs.related(["api/src/billing/x.py"], HISTORY) == []
    assert rs.related([], HISTORY) == []


def test_render_lists_cases_first_and_escapes():
    r = rs.Related(1, "<b>orders</b>", ["api/src/orders/service.py"], [],
                   [(501, "Đặt hàng <script>")])
    text = rs.render([r])
    assert text.index("Test case nên chạy lại") < text.index("Việc liên quan")
    assert "#501" in text and "<script>" not in text and "&lt;b&gt;" in text


class _Repo:
    async def get_recent(self, n):
        return [_rec(1, ["api/src/orders/service.py"]), _rec(2, ["api/src/orders/x.py"])]


class _Ado:
    def __init__(self):
        self.comments = []

    async def get_linked_ids(self, ids, rel):
        assert rel == rs.TESTED_BY
        return {1: {501}, 2: set()}

    async def get_work_items_by_ids(self, ids):
        return [WorkItemInfo(id=501, title="Đặt hàng", work_item_type="Test Case", state="Ready")]

    async def add_pull_request_comment(self, repo, pr_id, text, **_):
        self.comments.append((repo, pr_id, text))
        return True


def _poller(cfg, ado):
    log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    return SimpleNamespace(_config=cfg, _c=SimpleNamespace(ado=ado, execution_repo=_Repo()),
                           _log=log)


def _result(files):
    return ExecutionResult(work_item_id=7, success=True, files_changed=files,
                           pr_url="https://dev.azure.com/o/p/_git/api/pullrequest/9")


@pytest.mark.asyncio
async def test_poller_posts_scope_with_the_linked_cases():
    from ai_autopilot.services.poller import AdoPollerService
    ado = _Ado()
    await AdoPollerService._post_regression_scope(
        _poller(Settings(dry_run=False), ado), SimpleNamespace(id=7),
        _result(["api/src/orders/service.py"]))
    [(repo, pr, text)] = ado.comments
    assert (repo, pr) == ("api", 9) and "#501 Đặt hàng" in text and "#2" in text


@pytest.mark.asyncio
async def test_poller_stays_quiet_when_nothing_is_related_or_switched_off():
    from ai_autopilot.services.poller import AdoPollerService
    ado = _Ado()
    await AdoPollerService._post_regression_scope(
        _poller(Settings(dry_run=False), ado), SimpleNamespace(id=7), _result(["x/y/z.py"]))
    await AdoPollerService._post_regression_scope(
        _poller(Settings(dry_run=False, pr_regression_hints=False), ado), SimpleNamespace(id=7),
        _result(["api/src/orders/service.py"]))
    assert ado.comments == []
