"""Jira as a work-item provider.

Weighted towards the three places Jira genuinely differs from ADO, because those are
where a quiet wrong answer is possible: the id/key split, moving an issue by transition
rather than by writing a field, and comments that are ADF rather than HTML.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from ai_autopilot.config import Settings, WorkspaceConfig, is_bot_signed
from ai_autopilot.providers import PROVIDER_JIRA, WorkItemProvider
from ai_autopilot.providers.base import CAT_COMPLETED, CAT_IN_PROGRESS, CAT_PROPOSED
from ai_autopilot.providers.jira import JiraClient, html_to_text

ISSUE = {
    "id": "10042",
    "key": "DXF-7",
    "fields": {
        "summary": "Biểu đồ TSKT hiển thị 3 chữ số",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "issuetype": {"name": "Story"},
        "labels": ["vm-autopilot", "uat"],
        "assignee": {"displayName": "Ngan Nguyen", "emailAddress": "ngan@x.vn",
                     "accountId": "acc-1"},
        "reporter": {"displayName": "Phong Pham"},
        "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Cần 3 chữ số"}]}]},
        "parent": {"id": "10000", "key": "DXF-1"},
        "priority": {"name": "High"},
        "created": "2026-09-01T03:00:00.000+0700",
        "updated": "2026-09-16T08:30:00.000+0700",
    },
}


def _client(handler, **ws_over):
    """A JiraClient whose HTTP goes to ``handler`` instead of the network."""
    ws = WorkspaceConfig(
        name="J", ado_projects=["DXF"], provider=PROVIDER_JIRA,
        jira_url="https://site.atlassian.net", jira_email="bot@x.vn",
        jira_token="tok", jira_project="DXF", **ws_over,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg = Settings(trigger_tags=["vm-autopilot"], trigger_states=["To Do", "In Progress"])
    return JiraClient(http, cfg, ws), http


def _json(payload, status=200):
    return httpx.Response(status, json=payload)


# ── the contract ─────────────────────────────────────────────────────────────


def test_the_jira_client_satisfies_the_provider_protocol():
    """The protocol is the only thing the poller knows about; a client that drifts from
    it fails at the first poll, in production, not here."""
    client, _ = _client(lambda request: _json({}))
    assert isinstance(client, WorkItemProvider)


# ── discovery ────────────────────────────────────────────────────────────────


async def test_the_poll_asks_for_the_trigger_labels_in_the_trigger_states():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["jql"] = json.loads(request.content)["jql"]
        return _json({"issues": [ISSUE]})

    client, http = _client(handler)
    async with http:
        await client.get_pending_work_items()

    jql = seen["jql"]
    assert 'project = "DXF"' in jql
    assert 'labels = "vm-autopilot"' in jql
    assert 'status IN ("To Do", "In Progress")' in jql


async def test_the_run_now_sweep_does_not_filter_by_state():
    """An item started by hand must be found wherever it stands — the same reason the
    ADO sweep queries by tag alone."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["jql"] = json.loads(request.content)["jql"]
        return _json({"issues": []})

    client, http = _client(handler)
    async with http:
        await client.get_work_items_tagged_any(["vm-autopilot-run"])

    assert "status IN" not in seen["jql"]
    assert 'labels = "vm-autopilot-run"' in seen["jql"]


# ── mapping ──────────────────────────────────────────────────────────────────


async def test_an_issue_keeps_its_numeric_id_and_carries_its_key():
    """The numeric id is what the database, the branch names and every page are keyed
    on; the key is what a human (and a Jira smart commit) reads."""
    client, http = _client(lambda request: _json({"issues": [ISSUE]}))
    async with http:
        item = (await client.get_pending_work_items())[0]

    assert item.id == 10042 and item.key == "DXF-7"
    assert item.ref == "DXF-7"                 # …and that is what a card shows
    assert item.provider == "jira"
    assert item.project == "DXF"               # so scoped_for_project finds it again
    assert item.state == "In Progress"
    assert item.tags == ["vm-autopilot", "uat"]
    assert item.assigned_to == "Ngan Nguyen"
    assert item.description == "Cần 3 chữ số"  # ADF flattened, not a dict repr
    assert item.parent_id == 10000
    assert item.priority == 2                   # High


@pytest.mark.parametrize(
    ("jira_category", "expected"),
    [("new", CAT_PROPOSED), ("indeterminate", CAT_IN_PROGRESS), ("done", CAT_COMPLETED)],
)
async def test_state_categories_speak_the_vocabulary_the_rest_of_the_app_uses(
    jira_category, expected
):
    client, http = _client(lambda request: _json(
        [{"name": "Ready for Dev", "statusCategory": {"key": jira_category}}]
    ))
    async with http:
        assert await client.get_state_categories() == {"ready for dev": expected}


# ── labels ───────────────────────────────────────────────────────────────────


async def test_adding_a_label_edits_that_label_and_leaves_the_others_alone():
    """Writing the whole list back would drop any label a person added between the read
    and the write — the same race the ADO client edits tags atomically to avoid."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["method"] = request.method
        return _json({})

    client, http = _client(handler)
    async with http:
        assert await client.add_tag(10042, "autopilot-done") is True

    assert seen["method"] == "PUT"
    assert seen["body"] == {"update": {"labels": [{"add": "autopilot-done"}]}}


async def test_removing_a_label_uses_the_remove_verb():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json({})

    client, http = _client(handler)
    async with http:
        await client.remove_tag(10042, "vm-autopilot")

    assert seen["body"] == {"update": {"labels": [{"remove": "vm-autopilot"}]}}


# ── moving an issue ──────────────────────────────────────────────────────────


async def test_a_state_change_goes_through_the_transition_that_lands_there():
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _json({"transitions": [
                {"id": "11", "name": "Start", "to": {"name": "In Progress"}},
                {"id": "21", "name": "Done", "to": {"name": "Resolved"}},
            ]})
        posted["body"] = json.loads(request.content)
        return _json({})

    client, http = _client(handler)
    async with http:
        assert await client.update_state(10042, "Resolved") is True

    assert posted["body"] == {"transition": {"id": "21"}}


async def test_a_state_with_no_transition_from_here_is_refused_and_names_what_is_available():
    """Jira has no "set the status": a state can exist and still be unreachable from
    where the issue stands. A board stuck for that reason looks like a broken
    integration unless the log says what WAS reachable."""
    logged: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return _json({"transitions": [{"id": "11", "to": {"name": "In Progress"}}]})

    client, http = _client(handler)
    client._log = SimpleNamespace(
        error=lambda msg, **kw: logged.update(kw), warning=lambda *a, **k: None,
        info=lambda *a, **k: None,
    )
    async with http:
        assert await client.update_state(10042, "Ready for Testing") is False

    assert logged["available"] == ["In Progress"]


# ── comments ─────────────────────────────────────────────────────────────────


async def test_a_comment_arrives_as_text_and_keeps_the_bot_signature():
    """The signature is how the poller tells its own comments from a human's. Lose it in
    the HTML → ADF conversion and the bot starts answering itself."""
    from ai_autopilot.config import BOT_COMMENT_PREFIX

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json({})

    client, http = _client(handler)
    async with http:
        await client.add_comment(10042, f"{BOT_COMMENT_PREFIX}<div>▶️ <b>Đã nhận việc</b></div>")

    text = "\n".join(
        part["text"]
        for block in seen["body"]["body"]["content"]
        for part in (block.get("content") or [])
    )
    assert "Đã nhận việc" in text and "<b>" not in text
    assert is_bot_signed(text)


async def test_comments_are_read_back_newest_first_in_the_shape_the_poller_expects():
    client, http = _client(lambda request: _json({"comments": [
        {"body": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "/ai sửa giúp"}]}]},
         "author": {"displayName": "Ngan"}, "created": "2026-09-16T09:00:00.000+0700"},
    ]}))
    async with http:
        comments = await client.get_work_item_comments(10042)

    assert comments[0]["text"] == "/ai sửa giúp"
    assert comments[0]["created_by"] == "Ngan"


# ── failure is an answer, not an exception ───────────────────────────────────


async def test_a_site_that_is_down_costs_a_cycle_not_the_process():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client, http = _client(handler)
    async with http:
        assert await client.get_pending_work_items() == []
        assert await client.add_comment(1, "x") is False
        assert await client.update_state(1, "Done") is False


async def test_a_rejected_request_is_logged_not_raised():
    client, http = _client(lambda request: _json({"errorMessages": ["nope"]}, status=401))
    async with http:
        assert await client.get_pending_work_items() == []


def test_html_is_flattened_rather_than_pasted_into_jira():
    assert html_to_text("<div>A<br/>B</div>") == "A\nB"
    assert html_to_text("<ul><li>one</li><li>two</li></ul>") == "• one\n• two"
    assert html_to_text("&lt;kept&gt;") == "<kept>"


# ── routing: which tracker owns which project ────────────────────────────────


def _container(**over):
    from ai_autopilot.container import Container

    c = Container(Settings(**over))
    c.build_providers()
    return c


def test_an_install_with_no_jira_routes_everything_to_ado():
    """The registry is empty until a workspace asks for another tracker, so adding this
    changes nothing for every existing machine."""
    c = _container()
    assert c.providers == {}
    assert c.provider_for("Anything") is c.ado
    assert c.provider_for("") is c.ado


def test_a_jira_workspace_owns_its_projects_and_nothing_else():
    c = _container(workspaces=[WorkspaceConfig(
        name="J", ado_projects=["DXF", "DXF-Ops"], provider=PROVIDER_JIRA,
        jira_url="https://site.atlassian.net", jira_email="b@x", jira_token="t",
        jira_project="DXF",
    )])
    assert isinstance(c.provider_for("DXF"), JiraClient)
    assert isinstance(c.provider_for("dxf-ops"), JiraClient)   # case-insensitive
    assert c.provider_for("TLCL-DxFac") is c.ado               # an ADO project is untouched


def test_the_agent_brief_names_the_tracker_the_item_came_from():
    """Telling a Jira team's agent to use the Azure DevOps MCP sends it to a server that
    workspace does not have, and the failure looks like an item with no detail."""
    from ai_autopilot.execution.claude_executor import ClaudeExecutor
    from ai_autopilot.models import WorkItemInfo

    ex = ClaudeExecutor(Settings(workspace_directory="C:/ws"), None)
    jira = WorkItemInfo(id=10042, key="DXF-7", provider="jira", title="t")
    ado = WorkItemInfo(id=9083, title="t")

    jira_prompt = ex._build_prompt(jira, "/implement", "C:/ws/Backend-Fresh")
    assert "Jira MCP" in jira_prompt
    assert "Jira issue DXF-7" in jira_prompt          # the id ITS tools answer to
    assert "#10042" not in jira_prompt
    assert "Azure DevOps MCP" in ex._build_prompt(ado, "/implement", "C:/ws/Backend-Fresh")
