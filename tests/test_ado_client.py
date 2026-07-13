"""Tests for AdoClient URL scoping (work-item vs code project)."""

from __future__ import annotations

from ai_autopilot.ado.client import AdoClient
from ai_autopilot.config import Settings


def _client(**over) -> AdoClient:
    cfg = Settings(ado_organization="https://dev.azure.com/org", **over)
    return AdoClient(http=None, auth=None, config=cfg)  # URL helpers don't touch http/auth


def test_git_url_falls_back_to_ado_project():
    c = _client(ado_project="WorkItems")
    assert c._git_url("git/repositories") == "https://dev.azure.com/org/WorkItems/_apis/git/repositories"


def test_git_url_uses_code_project_when_set():
    c = _client(ado_project="WorkItems", code_project="DxFactory")
    # git/PR/build scoped to the code project…
    assert c._git_url("git/repositories") == "https://dev.azure.com/org/DxFactory/_apis/git/repositories"
    # …while work-item URLs stay on the work-item project
    assert c._url("wit/workitems/5") == "https://dev.azure.com/org/WorkItems/_apis/wit/workitems/5"


class _CaptureResp:
    status_code = 200
    text = '{"workItems": []}'

    @staticmethod
    def json():
        return {"workItems": []}


class _CaptureHttp:
    def __init__(self):
        self.last_query = ""

    async def post(self, url, json=None, headers=None):
        self.last_query = (json or {}).get("query", "")
        return _CaptureResp()


async def _assignee_query(assignee: str) -> str:
    c = _client(ado_project="Proj")
    c._http = _CaptureHttp()
    async def _no_headers():
        return {}
    c._headers = _no_headers
    await c.get_work_items_by_assignee(assignee)
    return c._http.last_query


async def test_single_assignee_uses_equals():
    q = await _assignee_query("a@x.com")
    assert "[System.AssignedTo] = 'a@x.com'" in q
    assert " IN (" not in q.split("AssignedTo", 1)[1][:30]


async def test_multi_assignee_uses_in_clause():
    q = await _assignee_query("a@x.com, b@x.com ; c@x.com")
    assert "[System.AssignedTo] IN ('a@x.com', 'b@x.com', 'c@x.com')" in q


async def test_blank_assignee_returns_empty(tmp_path):
    c = _client(ado_project="Proj")
    c._http = _CaptureHttp()
    assert await c.get_work_items_by_assignee("  , ; ") == []
    assert c._http.last_query == ""          # never queried
