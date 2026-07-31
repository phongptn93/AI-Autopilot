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


def test_candidate_clause_tag_only_by_default():
    c = _client(trigger_tag="phong-autopilot")
    clause = c._candidate_clause()
    assert "CONTAINS 'phong-autopilot'" in clause
    assert "AssignedTo" not in clause                 # no assignee branch when tag blank


def test_candidate_clause_adds_assignee_scoped_tag():
    c = _client(
        trigger_tag="phong-autopilot",
        assignee_trigger_tag="ai-autopilot",
        assignee_trigger_user="phong.pham@nois.vn",
    )
    clause = c._candidate_clause()
    # shared tag only counts when assigned to this machine's user
    assert "[System.Tags] CONTAINS 'ai-autopilot'" in clause
    assert "[System.AssignedTo] CONTAINS 'phong.pham@nois.vn'" in clause
    assert " OR " in clause                            # OR-ed with the normal trigger tag


def test_candidate_clause_falls_back_to_auto_assignee():
    c = _client(assignee_trigger_tag="ai-autopilot", auto_transition_assignee="alice@x.com")
    assert "AssignedTo] CONTAINS 'alice@x.com'" in c._candidate_clause()


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


# ── Batched work-item fetch ───────────────────────────────────────────────────
# Verified against live ADO first: 3 valid ids + 1 bogus returns HTTP 404 with an empty
# body-less result, and `errorPolicy=Omit` turns the same request into a 200 carrying the
# 3 real items. These tests pin that behaviour so it cannot regress silently.


class _BatchHttp:
    """Records each GET and replays canned responses in order."""

    def __init__(self, responses):
        self.urls: list[str] = []
        self._responses = list(responses)

    async def get(self, url, headers=None):
        self.urls.append(url)
        return self._responses.pop(0)


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _wi(wid: int) -> dict:
    return {"id": wid, "fields": {"System.Title": f"t{wid}", "System.State": "Active",
                                  "System.WorkItemType": "Task"}}


def _batch_client(responses) -> AdoClient:
    c = _client(ado_project="Proj")
    c._http = _BatchHttp(responses)

    class _NoAuth:
        async def get_auth_header(self):
            return {}

    c._auth = _NoAuth()
    return c


async def test_more_than_200_ids_are_chunked_not_truncated():
    """ADO caps this endpoint at 200 ids (VS403474). The old code sliced to the first 200,
    so a 300-id fetch silently reported the last 100 as non-existent."""
    ids = list(range(1, 301))
    c = _batch_client([
        _Resp(payload={"value": [_wi(i) for i in ids[:200]]}),
        _Resp(payload={"value": [_wi(i) for i in ids[200:]]}),
    ])
    got = await c.get_work_items_by_ids(ids)
    assert len(got) == 300
    assert len(c._http.urls) == 2                       # two requests, nothing dropped
    assert "ids=1," in c._http.urls[0]
    assert "ids=201," in c._http.urls[1]


async def test_batch_asks_ado_to_omit_unreadable_ids():
    """Without errorPolicy=Omit, ONE unreadable id 404s the whole batch — so a single stale
    link used to blank out every other item in the request."""
    c = _batch_client([_Resp(payload={"value": [_wi(1)]})])
    await c.get_work_items_by_ids([1])
    assert "errorPolicy=Omit" in c._http.urls[0]


async def test_nulls_from_omit_are_filtered_not_mapped():
    """With Omit, ADO returns a null in place of each id it could not read; mapping one
    would raise."""
    c = _batch_client([_Resp(payload={"value": [_wi(1), None, _wi(3)]})])
    got = await c.get_work_items_by_ids([1, 2, 3])
    assert [i.id for i in got] == [1, 3]


async def test_failure_logs_the_ids_and_the_ado_message(capsys):
    """The old warning carried only the status, so it could not say WHICH item — while the
    body it discarded named it: "TF401232: Work item 999 does not exist".

    capsys, not caplog: structlog writes to stdout rather than through the stdlib handlers
    caplog attaches to, so caplog.text is empty here even when the line was emitted.
    """
    body = '{"message":"TF401232: Work item 999 does not exist, or you do not have permissions"}'
    c = _batch_client([_Resp(status=404, text=body)])
    assert await c.get_work_items_by_ids([999]) == []
    logged = capsys.readouterr().out
    assert "999" in logged
    assert "TF401232" in logged          # the actionable part, previously thrown away
