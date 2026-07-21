"""Azure DevOps REST API client (ported from ``AdoClient``).

Talks to the ADO REST API 7.1 directly with httpx (this runs *outside* Claude
Code, so it does not use MCP). Auth headers are injected per-request via
``AdoAuthService`` to support both PAT and OAuth token refresh.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from ai_autopilot.ado.auth import AdoAuthService
from ai_autopilot.config import BOT_COMMENT_PREFIX, Settings, is_bot_signed
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import TaskCategory, WorkItemInfo

_API = "api-version=7.1"
_FIELDS = (
    "System.Id,System.Title,System.WorkItemType,System.State,"
    "System.AssignedTo,System.Description,Microsoft.VSTS.Common.AcceptanceCriteria,"
    "System.Parent,System.Tags,System.AreaPath,System.IterationPath,System.ChangedDate,"
    "Microsoft.VSTS.Common.Priority,System.CreatedBy"
)

# ADO relation types used for dependency-aware scheduling (P1).
_REL_PREDECESSOR = "System.LinkTypes.Dependency-Reverse"  # this item depends on target
_REL_SUCCESSOR = "System.LinkTypes.Dependency-Forward"    # target depends on this item
_REL_RELATED = "System.LinkTypes.Related"                 # soft conflict (touch same area)


def is_bot_comment(text: str | None) -> bool:
    """True if a comment was authored by the autopilot — decided by the signature in the
    CONTENT (author is useless: the bot posts under the operator's own identity).
    Delegates to ``config.is_bot_signed``, which HTML-unescapes before matching (ADO
    stores the 🤖 emoji as an entity)."""
    return is_bot_signed(text)


class AdoClient:
    def __init__(self, http: httpx.AsyncClient, auth: AdoAuthService, config: Settings) -> None:
        self._http = http
        self._auth = auth
        self._config = config
        self._log = get_logger("ado.client")
        self._base = config.ado_organization.rstrip("/")

    def refresh(self) -> None:
        """Re-read the organization URL after a live config change."""
        self._base = self._config.ado_organization.rstrip("/")

    async def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        headers = await self._auth.get_auth_header()
        headers["Content-Type"] = content_type
        return headers

    def _url(self, path: str) -> str:
        """Work-item API URL (scoped to ado_project — where work items live)."""
        return f"{self._base}/{self._config.ado_project}/_apis/{path}"

    def _git_url(self, path: str) -> str:
        """Git / PR / build API URL — scoped to code_project when set (repos and
        PRs may live in a different project than the work items)."""
        project = self._config.code_project or self._config.ado_project
        return f"{self._base}/{project}/_apis/{path}"

    def _tag_clause(self) -> str:
        """WIQL predicate matching ANY configured trigger tag, e.g.
        ``([System.Tags] CONTAINS 'a' OR [System.Tags] CONTAINS 'b')``."""
        tags = self._config.effective_trigger_tags or ["autopilot"]
        ors = " OR ".join(f"[System.Tags] CONTAINS '{t}'" for t in tags)
        return f"({ors})"

    def _candidate_clause(self) -> str:
        """The full trigger predicate: any trigger tag, OR the shared assignee-trigger
        tag scoped to this machine's assignee (so a team can share one tag)."""
        parts = [self._tag_clause()]
        atag = (self._config.assignee_trigger_tag or "").strip().replace("'", "''")
        if atag:
            user = (
                self._config.assignee_trigger_user
                or self._config.auto_transition_assignee
                or ""
            ).strip().replace("'", "''")
            if user:
                parts.append(
                    f"([System.Tags] CONTAINS '{atag}' "
                    f"AND [System.AssignedTo] CONTAINS '{user}')"
                )
            else:  # no assignee configured → treat the tag like a plain trigger tag
                parts.append(f"[System.Tags] CONTAINS '{atag}'")
        return "(" + " OR ".join(parts) + ")"

    async def get_pending_work_items(self) -> list[WorkItemInfo]:
        """Query work items tagged with any trigger tag in pending states."""
        states = ", ".join(f"'{s}'" for s in self._config.trigger_states) or "'New'"
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE {self._candidate_clause()} "
            f"AND [System.State] IN ({states}) "
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
                "WIQL query failed — check PAT / organization / project",
                status=resp.status_code,
                body=resp.text[:300],
            )
            return []

        refs = resp.json().get("workItems") or []
        ids = [r["id"] for r in refs]
        self._log.info(
            "ADO poll",
            tags=self._config.effective_trigger_tags,
            states=self._config.trigger_states,
            project=self._config.ado_project,
            matched=len(ids),
        )
        if not ids:
            return []
        return await self.get_work_items_by_ids(ids)

    async def get_work_items_by_assignee(
        self, assignee: str, states: list[str] | None = None,
        types: list[str] | None = None, top: int = 200,
    ) -> list[WorkItemInfo]:
        """Work items assigned to ``assignee`` — email/unique name, or several separated
        by comma/semicolon (a team) — any tag, for the Planning workbench to pick from.
        Optionally filtered to ``states`` / ``types``."""
        people = [
            p.replace("'", "''").strip()
            for p in re.split(r"[,;]", assignee or "")
            if p.strip()
        ]
        if not people:
            return []
        if len(people) == 1:
            who_clause = f"[System.AssignedTo] = '{people[0]}'"
        else:
            who_list = ", ".join(f"'{p}'" for p in people)
            who_clause = f"[System.AssignedTo] IN ({who_list})"
        clauses = [
            who_clause,
            f"[System.TeamProject] = '{self._config.ado_project}'",
        ]
        if states:
            state_list = ", ".join(f"'{s}'" for s in states)
            clauses.append(f"[System.State] IN ({state_list})")
        if types:
            type_list = ", ".join(f"'{t}'" for t in types)
            clauses.append(f"[System.WorkItemType] IN ({type_list})")
        wiql = (
            "SELECT [System.Id] FROM WorkItems WHERE "
            + " AND ".join(clauses)
            + " ORDER BY [System.ChangedDate] DESC"
        )
        try:
            resp = await self._http.post(
                self._url(f"wit/wiql?{_API}"), json={"query": wiql}, headers=await self._headers()
            )
        except httpx.HTTPError as exc:
            self._log.warning("assignee WIQL request error", error=str(exc))
            return []
        text = resp.text.lstrip()
        if resp.status_code >= 400 or not text.startswith("{"):
            self._log.warning("assignee WIQL failed", status=resp.status_code)
            return []
        ids = [r["id"] for r in (resp.json().get("workItems") or [])][:top]
        return await self.get_work_items_by_ids(ids)

    async def get_all_tagged_work_items(self) -> list[WorkItemInfo]:
        """Query every work item carrying the trigger tag, in ANY state.

        Used by the board to show the full picture (queued, in-flight, done,
        held) — unlike ``get_pending_work_items`` which filters to trigger states.
        """
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE {self._candidate_clause()} "
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
            self._log.warning("board WIQL request error", error=str(exc))
            return []
        text = resp.text.lstrip()
        if resp.status_code >= 400 or not text.startswith("{"):
            self._log.warning("board WIQL failed", status=resp.status_code)
            return []
        ids = [r["id"] for r in (resp.json().get("workItems") or [])]
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
        # Lead with the bot icon so this comment is recognisable as ours at a glance —
        # the comment-reaction loop skips it (see config.BOT_COMMENT_PREFIX).
        resp = await self._http.post(
            self._url(f"wit/workitems/{work_item_id}/comments?api-version=7.1-preview.4"),
            json={"text": BOT_COMMENT_PREFIX + comment},
            headers=await self._headers(),
        )
        if resp.status_code >= 400:
            self._log.warning("add_comment failed", id=work_item_id, status=resp.status_code)
        return resp.status_code < 400

    async def get_work_item_comments(self, work_item_id: int) -> list[dict[str, Any]]:
        """A work item's discussion comments, oldest→newest.

        Each dict: ``{"id", "text", "is_bot", "created_by"}``. ``is_bot`` is decided by
        the bot icon in the content (the author is same-account, so it cannot
        distinguish bot from human)."""
        url = self._url(f"wit/workItems/{work_item_id}/comments?api-version=7.1-preview.4")
        try:
            resp = await self._http.get(url, headers=await self._headers())
        except httpx.HTTPError as exc:
            self._log.warning("get_work_item_comments error", id=work_item_id, error=str(exc))
            return []
        if resp.status_code >= 400:
            self._log.warning(
                "get_work_item_comments failed", id=work_item_id, status=resp.status_code
            )
            return []
        raw = resp.json().get("comments") or []
        out = [
            {
                "id": c.get("id"),
                "text": c.get("text") or "",
                "is_bot": is_bot_comment(c.get("text")),
                "created_by": _identity(c.get("createdBy")),
                "created_by_email": _identity_email(c.get("createdBy")),
            }
            for c in raw
            if c.get("id") is not None
        ]
        out.sort(key=lambda c: c["id"])
        return out

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

    async def remove_tag(self, work_item_id: int, tag: str) -> bool:
        item = await self.get_work_item(work_item_id)
        if item is None:
            return False
        remaining = [t for t in item.tags if t.lower() != tag.lower()]
        if len(remaining) == len(item.tags):
            return True  # tag wasn't present — nothing to do
        patch = [{"op": "replace", "path": "/fields/System.Tags", "value": "; ".join(remaining)}]
        resp = await self._patch(work_item_id, patch)
        if resp.status_code >= 400:
            self._log.warning("remove_tag failed", id=work_item_id, status=resp.status_code)
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
            self._git_url(f"git/repositories?{_API}"), headers=await self._auth.get_auth_header()
        )
        if resp.status_code >= 400:
            self._log.warning("get_repositories failed", status=resp.status_code)
            return []
        return resp.json().get("value") or []

    async def get_active_pull_requests(self, repo_id: str) -> list[dict[str, Any]]:
        return await self._pull_requests_by_status(repo_id, "active")

    async def get_completed_pull_requests(self, repo_id: str) -> list[dict[str, Any]]:
        return await self._pull_requests_by_status(repo_id, "completed")

    async def get_abandoned_pull_requests(self, repo_id: str) -> list[dict[str, Any]]:
        return await self._pull_requests_by_status(repo_id, "abandoned")

    async def _pull_requests_by_status(self, repo_id: str, status: str) -> list[dict[str, Any]]:
        url = self._git_url(
            f"git/repositories/{repo_id}/pullrequests?searchCriteria.status={status}&{_API}"
        )
        resp = await self._http.get(url, headers=await self._auth.get_auth_header())
        if resp.status_code >= 400:
            self._log.warning("get_pull_requests failed", status=resp.status_code, pr_status=status)
            return []
        return resp.json().get("value") or []

    async def get_successful_builds(
        self, definition_id: int, branch: str
    ) -> list[dict[str, Any]]:
        """Recent successful builds on ``branch`` (optionally for one pipeline)."""
        query = (
            f"build/builds?statusFilter=completed&resultFilter=succeeded"
            f"&branchName=refs/heads/{quote(branch, safe='')}&$top=5&{_API}"
        )
        if definition_id:
            query += f"&definitions={definition_id}"
        resp = await self._http.get(self._git_url(query), headers=await self._auth.get_auth_header())
        if resp.status_code >= 400:
            self._log.warning("get_successful_builds failed", status=resp.status_code)
            return []
        return resp.json().get("value") or []

    async def get_children(self, parent_id: int) -> list[WorkItemInfo]:
        """Child work items of a parent (via System.Parent)."""
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.Parent] = {parent_id} "
            f"AND [System.TeamProject] = '{self._config.ado_project}'"
        )
        try:
            resp = await self._http.post(
                self._url(f"wit/wiql?{_API}"), json={"query": wiql}, headers=await self._headers()
            )
        except httpx.HTTPError as exc:
            self._log.warning("get_children request error", error=str(exc))
            return []
        text = resp.text.lstrip()
        if resp.status_code >= 400 or not text.startswith("{"):
            self._log.warning("get_children failed", status=resp.status_code)
            return []
        ids = [r["id"] for r in (resp.json().get("workItems") or [])]
        return await self.get_work_items_by_ids(ids)

    async def get_work_item_links(
        self, ids: list[int]
    ) -> tuple[dict[int, set[int]], dict[int, set[int]]]:
        """Read the dependency link graph for ``ids`` via ``$expand=relations``.

        Returns ``(predecessors, related)`` — each ``id → {linked ids}``:
        ``predecessors`` are items this one must wait for (``Dependency-Reverse``),
        ``related`` are soft-conflict neighbours (``Related``). Best-effort: any
        error yields empty maps so scheduling degrades to plain priority order.
        """
        preds: dict[int, set[int]] = {}
        related: dict[int, set[int]] = {}
        if not ids:
            return preds, related
        ids_param = ",".join(str(i) for i in list(ids)[:200])
        try:
            resp = await self._http.get(
                self._url(f"wit/workitems?ids={ids_param}&$expand=relations&{_API}"),
                headers=await self._auth.get_auth_header(),
            )
        except httpx.HTTPError as exc:
            self._log.warning("get_work_item_links request error", error=str(exc))
            return preds, related
        if resp.status_code >= 400:
            self._log.warning("get_work_item_links failed", status=resp.status_code)
            return preds, related
        for wi in resp.json().get("value") or []:
            wid = wi.get("id")
            if wid is None:
                continue
            p, r = set(), set()
            for rel in wi.get("relations") or []:
                target = _rel_target_id(str(rel.get("url", "")))
                if target is None or target == wid:
                    continue
                rel_type = rel.get("rel", "")
                if rel_type == _REL_PREDECESSOR:
                    p.add(target)
                elif rel_type == _REL_RELATED:
                    r.add(target)
            if p:
                preds[wid] = p
            if r:
                related[wid] = r
        return preds, related

    async def get_pull_request_threads(self, repo_id: str, pr_id: int) -> list[dict[str, Any]]:
        url = self._git_url(f"git/repositories/{repo_id}/pullRequests/{pr_id}/threads?{_API}")
        resp = await self._http.get(url, headers=await self._auth.get_auth_header())
        if resp.status_code >= 400:
            self._log.warning("get_pull_request_threads failed", status=resp.status_code)
            return []
        return resp.json().get("value") or []

    async def add_pull_request_comment(
        self, repo_id: str, pr_id: int, text: str, *, active: bool = False
    ) -> bool:
        """Post a comment on a PR as a new thread (stamped with the bot signature).

        Defaults to a CLOSED (informational) thread so status updates don't pile up as
        unresolved comments — and so the babysitter's ``actionable_comments`` skips them
        (resolved threads are ignored), keeping the bot's own PR comments from
        re-triggering a revision."""
        url = self._git_url(f"git/repositories/{repo_id}/pullRequests/{pr_id}/threads?{_API}")
        body = {
            "comments": [
                {"parentCommentId": 0, "content": BOT_COMMENT_PREFIX + text, "commentType": 1}
            ],
            "status": "active" if active else "closed",
        }
        try:
            resp = await self._http.post(url, json=body, headers=await self._headers())
        except httpx.HTTPError as exc:
            self._log.warning("add_pull_request_comment error", pr=pr_id, error=str(exc))
            return False
        if resp.status_code >= 400:
            self._log.warning("add_pull_request_comment failed", pr=pr_id, status=resp.status_code)
            return False
        return True

    async def reply_to_pull_request_thread(
        self, repo_id: str, pr_id: int, thread_id: int, text: str
    ) -> bool:
        """Reply to an EXISTING PR thread (keeps the conversation on the human's own
        comment instead of scattering new threads). Stamped with the bot signature."""
        url = self._git_url(
            f"git/repositories/{repo_id}/pullRequests/{pr_id}/threads/{thread_id}/comments?{_API}"
        )
        body = {"content": BOT_COMMENT_PREFIX + text, "commentType": 1}
        try:
            resp = await self._http.post(url, json=body, headers=await self._headers())
        except httpx.HTTPError as exc:
            self._log.warning("reply_to_pr_thread error", pr=pr_id, error=str(exc))
            return False
        if resp.status_code >= 400:
            self._log.warning(
                "reply_to_pr_thread failed", pr=pr_id, thread=thread_id, status=resp.status_code
            )
            return False
        return True

    async def set_pull_request_thread_status(
        self, repo_id: str, pr_id: int, thread_id: int, status: str
    ) -> bool:
        """Set a PR thread's status (e.g. ``"fixed"`` to resolve it once addressed)."""
        url = self._git_url(
            f"git/repositories/{repo_id}/pullRequests/{pr_id}/threads/{thread_id}?{_API}"
        )
        try:
            resp = await self._http.patch(
                url, json={"status": status}, headers=await self._headers()
            )
        except httpx.HTTPError as exc:
            self._log.warning("set_pr_thread_status error", pr=pr_id, error=str(exc))
            return False
        if resp.status_code >= 400:
            self._log.warning(
                "set_pr_thread_status failed", pr=pr_id, thread=thread_id, status=resp.status_code
            )
            return False
        return True

    # ── Work-item states (for the Settings picker) ──────────────────────────

    async def get_states(self) -> list[str]:
        """Distinct work-item states configured in the project, board-ordered.

        Best-effort: returns ``[]`` on any error (offline / bad PAT) so the
        Settings page can always render with a manual-entry fallback. Fetches the
        project's work-item types then each type's states, aggregating distinct
        names ordered by state category (Proposed → InProgress → Resolved →
        Completed → Removed) then the type-defined order.
        """
        # Lower rank = shown first.
        cat_rank = {"Proposed": 0, "InProgress": 1, "Resolved": 2, "Completed": 3, "Removed": 4}
        # name → (category_rank, order) — keep the strongest (lowest) seen.
        seen: dict[str, tuple[int, int]] = {}
        try:
            resp = await self._http.get(
                self._url(f"wit/workitemtypes?{_API}"), headers=await self._auth.get_auth_header()
            )
            if resp.status_code >= 400:
                self._log.warning("get_states: list types failed", status=resp.status_code)
                return []
            types = [
                t["name"]
                for t in (resp.json().get("value") or [])
                if t.get("name") and not t.get("isDisabled")
            ]
            for type_name in types:
                sresp = await self._http.get(
                    self._url(f"wit/workitemtypes/{quote(type_name, safe='')}/states?{_API}"),
                    headers=await self._auth.get_auth_header(),
                )
                if sresp.status_code >= 400:
                    continue
                for s in sresp.json().get("value") or []:
                    name = s.get("name")
                    if not name:
                        continue
                    key = (cat_rank.get(s.get("stateCategory", ""), 9), int(s.get("order", 0)))
                    if name not in seen or key < seen[name]:
                        seen[name] = key
        except httpx.HTTPError as exc:
            self._log.warning("get_states request error", error=str(exc))
            return []
        return [name for name, _ in sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))]

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
            assigned_to_email=_identity_email(f.get("System.AssignedTo")),
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


_REL_ID_RE = re.compile(r"/workItems/(\d+)", re.IGNORECASE)


def _rel_target_id(url: str) -> int | None:
    """Parse the work-item id from a relation url (…/_apis/wit/workItems/1234)."""
    m = _REL_ID_RE.search(url)
    return int(m.group(1)) if m else None


def _json(obj: Any) -> str:
    return json.dumps(obj)


def _identity(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("displayName")
    return str(value) if value is not None else None


def _identity_email(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("uniqueName") or value.get("mailAddress")
    return None


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
