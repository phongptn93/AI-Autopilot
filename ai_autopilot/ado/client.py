"""Azure DevOps REST API client (ported from ``AdoClient``).

Talks to the ADO REST API 7.1 directly with httpx (this runs *outside* Claude
Code, so it does not use MCP). Auth headers are injected per-request via
``AdoAuthService`` to support both PAT and OAuth token refresh.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from ai_autopilot.ado.auth import AdoAuthService
from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import TaskCategory, WorkItemInfo

_API = "api-version=7.1"
_FIELDS = (
    "System.Id,System.Title,System.WorkItemType,System.State,"
    "System.AssignedTo,System.Description,Microsoft.VSTS.Common.AcceptanceCriteria,"
    "System.Parent,System.Tags,System.AreaPath,System.IterationPath,System.ChangedDate,"
    "Microsoft.VSTS.Common.Priority,System.CreatedBy"
)


class AdoClient:
    def __init__(self, http: httpx.AsyncClient, auth: AdoAuthService, config: Settings) -> None:
        self._http = http
        self._auth = auth
        self._config = config
        self._log = get_logger("ado.client")
        self._base = config.ado_organization.rstrip("/")

    async def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        headers = await self._auth.get_auth_header()
        headers["Content-Type"] = content_type
        return headers

    def _url(self, path: str) -> str:
        return f"{self._base}/{self._config.ado_project}/_apis/{path}"

    async def get_pending_work_items(self) -> list[WorkItemInfo]:
        """Query work items tagged with the trigger tag in pending states."""
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.Tags] CONTAINS '{self._config.trigger_tag}' "
            "AND [System.State] IN ('New', 'To Do', 'Proposed') "
            f"AND [System.TeamProject] = '{self._config.ado_project}' "
            "ORDER BY [System.ChangedDate] DESC"
        )
        try:
            resp = await self._http.post(
                self._url(f"wit/wiql?{_API}"),
                json={"query": wiql},
                headers=await self._headers(),
            )
        except httpx.HTTPError as exc:
            self._log.warning("WIQL request error", error=str(exc))
            return []

        text = resp.text.lstrip()
        if resp.status_code >= 400 or not text.startswith("{"):
            self._log.warning(
                "WIQL query failed (response is not JSON — check PAT)", status=resp.status_code
            )
            return []

        refs = resp.json().get("workItems") or []
        ids = [r["id"] for r in refs]
        if not ids:
            return []
        return await self.get_work_items_by_ids(ids)

    async def get_work_items_by_ids(self, ids: list[int]) -> list[WorkItemInfo]:
        if not ids:
            return []
        ids_param = ",".join(str(i) for i in ids[:200])
        resp = await self._http.get(
            self._url(f"wit/workitems?ids={ids_param}&fields={_FIELDS}&{_API}"),
            headers=await self._auth.get_auth_header(),
        )
        if resp.status_code >= 400:
            self._log.warning("get_work_items failed", status=resp.status_code)
            return []
        values = resp.json().get("value") or []
        return [self._map(wi) for wi in values]

    async def get_work_item(self, work_item_id: int) -> WorkItemInfo | None:
        items = await self.get_work_items_by_ids([work_item_id])
        return items[0] if items else None

    async def add_comment(self, work_item_id: int, comment: str) -> bool:
        resp = await self._http.post(
            self._url(f"wit/workitems/{work_item_id}/comments?api-version=7.1-preview.4"),
            json={"text": comment},
            headers=await self._headers(),
        )
        if resp.status_code >= 400:
            self._log.warning("add_comment failed", id=work_item_id, status=resp.status_code)
        return resp.status_code < 400

    async def update_state(self, work_item_id: int, new_state: str) -> bool:
        patch = [{"op": "replace", "path": "/fields/System.State", "value": new_state}]
        resp = await self._patch(work_item_id, patch)
        if resp.status_code >= 400:
            self._log.warning("update_state failed", id=work_item_id, status=resp.status_code)
        return resp.status_code < 400

    async def add_tag(self, work_item_id: int, tag: str) -> bool:
        item = await self.get_work_item(work_item_id)
        if item is None:
            return False
        current = "; ".join(item.tags)
        new_tags = tag if not current else f"{current}; {tag}"
        patch = [{"op": "replace", "path": "/fields/System.Tags", "value": new_tags}]
        resp = await self._patch(work_item_id, patch)
        return resp.status_code < 400

    async def create_work_item(
        self, title: str, item_type: str, parent_id: int | None, tag: str
    ) -> int:
        patch: list[dict[str, Any]] = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Tags", "value": tag},
        ]
        if parent_id is not None:
            patch.append(
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
                        "url": (
                            f"{self._base}/{self._config.ado_project}"
                            f"/_apis/wit/workitems/{parent_id}"
                        ),
                    },
                }
            )
        url = self._url(f"wit/workitems/${quote(item_type, safe='')}?{_API}")
        resp = await self._http.post(
            url, content=_json(patch), headers=await self._headers("application/json-patch+json")
        )
        if resp.status_code >= 400:
            self._log.warning("create_work_item failed", status=resp.status_code, body=resp.text)
            return 0
        return int(resp.json().get("id", 0))

    async def _patch(self, work_item_id: int, patch: list[dict[str, Any]]) -> httpx.Response:
        return await self._http.patch(
            self._url(f"wit/workitems/{work_item_id}?{_API}"),
            content=_json(patch),
            headers=await self._headers("application/json-patch+json"),
        )

    # ── Git / Pull Request API (PR babysitter) ──────────────────────────────

    async def get_repositories(self) -> list[dict[str, Any]]:
        resp = await self._http.get(
            self._url(f"git/repositories?{_API}"), headers=await self._auth.get_auth_header()
        )
        if resp.status_code >= 400:
            self._log.warning("get_repositories failed", status=resp.status_code)
            return []
        return resp.json().get("value") or []

    async def get_active_pull_requests(self, repo_id: str) -> list[dict[str, Any]]:
        url = self._url(
            f"git/repositories/{repo_id}/pullrequests?searchCriteria.status=active&{_API}"
        )
        resp = await self._http.get(url, headers=await self._auth.get_auth_header())
        if resp.status_code >= 400:
            self._log.warning("get_active_pull_requests failed", status=resp.status_code)
            return []
        return resp.json().get("value") or []

    async def get_pull_request_threads(self, repo_id: str, pr_id: int) -> list[dict[str, Any]]:
        url = self._url(f"git/repositories/{repo_id}/pullRequests/{pr_id}/threads?{_API}")
        resp = await self._http.get(url, headers=await self._auth.get_auth_header())
        if resp.status_code >= 400:
            self._log.warning("get_pull_request_threads failed", status=resp.status_code)
            return []
        return resp.json().get("value") or []

    # ── Field mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _map(wi: dict[str, Any]) -> WorkItemInfo:
        f: dict[str, Any] = wi.get("fields", {})
        tags = [t.strip() for t in str(f.get("System.Tags", "")).split(";") if t.strip()]
        return WorkItemInfo(
            id=wi["id"],
            title=str(f.get("System.Title", "")),
            work_item_type=str(f.get("System.WorkItemType", "")),
            state=str(f.get("System.State", "")),
            assigned_to=_identity(f.get("System.AssignedTo")),
            description=f.get("System.Description"),
            acceptance_criteria=f.get("Microsoft.VSTS.Common.AcceptanceCriteria"),
            parent_id=_as_int(f.get("System.Parent")),
            tags=tags,
            area_path=f.get("System.AreaPath"),
            iteration_path=f.get("System.IterationPath"),
            changed_date=_as_dt(f.get("System.ChangedDate")),
            priority=_as_int(f.get("Microsoft.VSTS.Common.Priority")) or 3,
            created_by=_identity(f.get("System.CreatedBy")),
            category=TaskCategory.UNKNOWN,
        )


def _json(obj: Any) -> str:
    return json.dumps(obj)


def _identity(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("displayName")
    return str(value) if value is not None else None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
