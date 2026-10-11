"""The Overview's "PR đã merge" must count every PR, not ADO's first page.

ADO answers a pull-request list without ``$top`` with one short page and no
continuation, so on a busy repo the merged count silently stopped at the newest ~100 —
and repos in a second code project were never scanned at all.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from ai_autopilot.ado.client import AdoClient
from ai_autopilot.config import Settings
from ai_autopilot.dashboard import common


class _Resp:
    def __init__(self, value):
        self.status_code = 200
        self._value = value

    def json(self):
        return {"value": self._value}


def _client(total: int, page: int = 500):
    client = AdoClient.__new__(AdoClient)
    client._config = Settings(ado_organization="https://dev.azure.com/o", ado_project="P")
    client._base = "https://dev.azure.com/o"
    client._auth = SimpleNamespace(get_auth_header=lambda: _async({}))
    client._log = SimpleNamespace(warning=lambda *a, **k: None)
    calls = []

    async def send(method, url, **kw):
        q = parse_qs(urlparse(url).query)
        top, skip = int(q["$top"][0]), int(q["$skip"][0])
        calls.append((top, skip))
        return _Resp([{"pullRequestId": i} for i in range(skip, min(skip + top, total))])

    client._send = send
    return client, calls


async def _async(v):
    return v


def test_every_page_is_read():
    client, calls = _client(total=1234)
    prs = asyncio.run(client.get_completed_pull_requests("r1"))
    assert len(prs) == 1234 and len({p["pullRequestId"] for p in prs}) == 1234
    assert calls == [(500, 0), (500, 500), (500, 1000)]


def test_a_short_repo_costs_one_request():
    client, calls = _client(total=7)
    assert len(asyncio.run(client.get_abandoned_pull_requests("r1"))) == 7
    assert len(calls) == 1


def test_scan_covers_every_code_project_once():
    asked = []

    class _Ado:
        async def get_repositories(self, project=""):
            asked.append(project)
            return [{"id": f"repo-{project or 'default'}"}, {"id": "shared"}]

        async def get_completed_pull_requests(self, rid, project=""):
            return [{"sourceRefName": f"refs/heads/feature/{10 if rid == 'repo-B' else 1}-x",
                     "pullRequestId": 1}]

        async def get_active_pull_requests(self, rid, project=""):
            return []

        async def get_abandoned_pull_requests(self, rid, project=""):
            return []

        async def get_pull_request_work_items(self, rid, pid, project=""):
            return []

    class _Exec:
        async def work_item_ids(self):
            return {1, 10}

    cfg = Settings(ado_project="A", ado_projects=["B"], code_project_map={"B": "B"})
    c = SimpleNamespace(ado=_Ado(), execution_repo=_Exec(), config=cfg)
    counts = asyncio.run(common._scan_pr_outcomes(c))
    assert len(asked) == len(set(asked)) and "B" in asked
    # repo-A, repo-B and the shared repo (counted once), one merged PR each
    assert counts["merged"] == 3
