"""Jira Cloud as a work-item provider.

Speaks the same shape as ``AdoClient`` for the half of it the pipeline uses (see
``providers/base.py``), so the poller does not learn a second vocabulary. Three places
where Jira genuinely differs, and each is where the bugs would be:

**Ids.** An issue has BOTH a numeric ``id`` (10042) and a key ("DXF-7"). The numeric one
is the key everywhere in this codebase — database rows, branch names, every page — so it
stays the id, and the key rides along in ``WorkItemInfo.key`` for display, for Jira's own
API (which is addressed by key) and for branch names a smart commit can link.

**Moving an issue is a transition, not a field write.** Jira will not accept
``status = "In Progress"``; it accepts a transition id that its workflow allows FROM
where the issue stands. A state that exists but is unreachable from here is a normal
answer, and the log has to name what WAS reachable or nobody can fix the flow.

**Comments are ADF, not HTML.** The pipeline writes HTML (the ADO client takes it
directly); this converts to text and wraps it, preserving the bot signature — otherwise
the poller reads its own comments as human replies and reacts to them.
"""

from __future__ import annotations

import base64
import html as html_mod
import re
from datetime import datetime
from typing import Any

import httpx

from ai_autopilot.logging_config import describe_exc, get_logger
from ai_autopilot.models import TaskCategory, WorkItemInfo
from ai_autopilot.providers.base import (
    CAT_COMPLETED,
    CAT_IN_PROGRESS,
    CAT_PROPOSED,
    PROVIDER_JIRA,
)

_API = "/rest/api/3"
# What the search returns. Asked for explicitly rather than taking Jira's default: the
# default payload is enormous (every custom field on the issue) and this runs on a timer.
_FIELDS = (
    "summary,status,issuetype,labels,assignee,reporter,description,parent,priority,"
    "created,updated"
)
# Jira's own three categories → the vocabulary the rest of the app already speaks.
_CATEGORY = {
    "new": CAT_PROPOSED,
    "indeterminate": CAT_IN_PROGRESS,
    "done": CAT_COMPLETED,
}
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """HTML the pipeline writes → plain text Jira can hold.

    The bot signature must survive: ``is_bot_signed`` looks for the 🤖 marker in the
    comment's CONTENT, and it is that check — not the author — that stops the poller
    reading its own comments as human instructions.
    """
    text = re.sub(r"<(br|/li|/p|/div|/ul)[^>]*>", "\n", html or "", flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.IGNORECASE)
    return html_mod.unescape(_TAG_RE.sub("", text)).strip()


def _adf(text: str) -> dict:
    """Plain text as an Atlassian Document Format body (what /comment takes on v3)."""
    lines = [line for line in (text or "").split("\n")]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph",
             "content": ([{"type": "text", "text": line}] if line else [])}
            for line in lines
        ],
    }


def _adf_to_text(node: Any) -> str:
    """ADF (or a plain string, on older payloads) → text, for reading comments back."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        parts = [_adf_to_text(child) for child in node.get("content") or []]
        joiner = "\n" if node.get("type") in ("doc", "paragraph") else ""
        return joiner.join(p for p in parts if p)
    if isinstance(node, list):
        return "".join(_adf_to_text(child) for child in node)
    return ""


def _as_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def jql_escape(value: str) -> str:
    """Quote a JQL string literal. Jira takes backslash escapes inside double quotes."""
    return (value or "").replace("\\", "\\\\").replace('"', '\\"')


class JiraClient:
    """One Jira site + project, as a :class:`WorkItemProvider`."""

    def __init__(self, http: httpx.AsyncClient, config: Any, workspace: Any) -> None:
        self._http = http
        self._config = config
        self._ws = workspace
        self._log = get_logger("providers.jira")
        # project → the ADO-style project name callers pass around. Jira's project key
        # IS that name for items from this site, so items answer with it and
        # `scoped_for_project` keeps resolving the right workspace.
        self._project = (getattr(workspace, "jira_project", "") or "").strip()

    # ── plumbing ─────────────────────────────────────────────────────────────

    @property
    def _base(self) -> str:
        return (getattr(self._ws, "jira_url", "") or "").rstrip("/")

    def _headers(self) -> dict[str, str]:
        email = (getattr(self._ws, "jira_email", "") or "").strip()
        token = (getattr(self._ws, "jira_token", "") or "").strip()
        raw = f"{email}:{token}".encode()
        return {
            "Authorization": "Basic " + base64.b64encode(raw).decode(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def refresh(self) -> None:
        """Nothing is cached from config beyond what is read per call."""
        return None

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response | None:
        """One API call. Returns None when the site could not be reached or refused —
        never raises, for the same reason the ADO client does not: a tracker that is
        briefly down must cost a poll cycle, not the process."""
        if not self._base:
            return None
        try:
            resp = await self._http.request(
                method, f"{self._base}{path}", headers=self._headers(), **kwargs
            )
        except httpx.HTTPError as exc:
            self._log.warning("jira request failed", path=path, error=describe_exc(exc))
            return None
        if resp.status_code >= 400:
            self._log.warning(
                "jira api error", path=path, status=resp.status_code,
                detail=" ".join((resp.text or "").split())[:300],
            )
            return None
        return resp

    # ── discovery ────────────────────────────────────────────────────────────

    def _tag_clause(self) -> str:
        tags = self._config.effective_trigger_tags or ["autopilot"]
        return "(" + " OR ".join(f'labels = "{jql_escape(t)}"' for t in tags) + ")"

    def build_jql(self, *, states: list[str] | None = None, tags: list[str] | None = None) -> str:
        """The JQL behind a poll. Kept separate so a test can read it, and so an
        operator can paste it into Jira when the poll returns nothing they expected."""
        parts = []
        if self._project:
            parts.append(f'project = "{jql_escape(self._project)}"')
        if tags is not None:
            parts.append(
                "(" + " OR ".join(f'labels = "{jql_escape(t)}"' for t in tags) + ")"
                if tags else "labels IS NOT EMPTY"
            )
        else:
            parts.append(self._tag_clause())
        if states:
            joined = ", ".join(f'"{jql_escape(s)}"' for s in states)
            parts.append(f"status IN ({joined})")
        return " AND ".join(parts) + " ORDER BY updated DESC"

    async def _search(self, jql: str, limit: int = 200) -> list[WorkItemInfo]:
        resp = await self._request(
            "POST", f"{_API}/search",
            json={"jql": jql, "maxResults": limit, "fields": _FIELDS.split(",")},
        )
        if resp is None:
            return []
        issues = (resp.json() or {}).get("issues") or []
        items = [self._map(issue) for issue in issues]
        self._log.info("jira poll", jql=jql, matched=len(items))
        return items

    async def get_pending_work_items(self) -> list[WorkItemInfo]:
        return await self._search(
            self.build_jql(states=list(self._config.effective_trigger_states))
        )

    async def get_all_tagged_work_items(self) -> list[WorkItemInfo]:
        return await self._search(self.build_jql())

    async def get_work_items_tagged_any(self, tags: list[str]) -> list[WorkItemInfo]:
        wanted = [t for t in (tags or []) if (t or "").strip()]
        if not wanted:
            return []
        return await self._search(self.build_jql(tags=wanted))

    # ── reading ──────────────────────────────────────────────────────────────

    async def get_work_item(self, work_item_id: int) -> WorkItemInfo | None:
        resp = await self._request("GET", f"{_API}/issue/{work_item_id}?fields={_FIELDS}")
        return self._map(resp.json()) if resp is not None else None

    async def get_work_items_by_ids(self, ids: list[int]) -> list[WorkItemInfo]:
        wanted = [str(i) for i in (ids or []) if i]
        if not wanted:
            return []
        return await self._search(f"id IN ({', '.join(wanted)}) ORDER BY updated DESC")

    async def get_children(self, parent_id: int) -> list[WorkItemInfo]:
        return await self._search(f"parent = {int(parent_id)} ORDER BY updated DESC")

    async def get_work_item_comments(self, work_item_id: int) -> list[dict[str, Any]]:
        """Newest first, in the shape the poller's comment loop reads."""
        resp = await self._request(
            "GET", f"{_API}/issue/{work_item_id}/comment?orderBy=-created&maxResults=50"
        )
        if resp is None:
            return []
        out = []
        for c in (resp.json() or {}).get("comments") or []:
            author = c.get("author") or {}
            out.append({
                "text": _adf_to_text(c.get("body")),
                "created_by": author.get("displayName") or author.get("emailAddress") or "",
                "created_date": c.get("created") or "",
            })
        return out

    async def get_state_categories(self) -> dict[str, str]:
        """``status name (lower) -> CAT_*`` for this project's statuses."""
        resp = await self._request("GET", f"{_API}/status")
        if resp is None:
            return {}
        out: dict[str, str] = {}
        for status in resp.json() or []:
            name = str(status.get("name") or "").strip()
            key = str((status.get("statusCategory") or {}).get("key") or "").lower()
            if name:
                out[name.lower()] = _CATEGORY.get(key, CAT_PROPOSED)
        return out

    # ── writing ──────────────────────────────────────────────────────────────

    async def add_comment(self, work_item_id: int, comment: str) -> bool:
        resp = await self._request(
            "POST", f"{_API}/issue/{work_item_id}/comment",
            json={"body": _adf(html_to_text(comment))},
        )
        return resp is not None

    async def _labels(self, work_item_id: int) -> list[str] | None:
        resp = await self._request("GET", f"{_API}/issue/{work_item_id}?fields=labels")
        if resp is None:
            return None
        return list(((resp.json() or {}).get("fields") or {}).get("labels") or [])

    async def _edit_labels(self, work_item_id: int, op: str, tag: str) -> bool:
        # The `update` verb adds or removes ONE label server-side. Writing the whole
        # list back instead would silently drop any label added by a person between the
        # read and the write — the same reason the ADO client edits tags atomically.
        resp = await self._request(
            "PUT", f"{_API}/issue/{work_item_id}",
            json={"update": {"labels": [{op: tag}]}},
        )
        return resp is not None

    async def add_tag(self, work_item_id: int, tag: str) -> bool:
        return await self._edit_labels(work_item_id, "add", tag)

    async def remove_tag(self, work_item_id: int, tag: str) -> bool:
        return await self._edit_labels(work_item_id, "remove", tag)

    async def update_state(self, work_item_id: int, new_state: str) -> bool:
        """Move the issue by finding the transition that lands on ``new_state``.

        Jira has no "set the status" — only transitions its workflow permits from where
        the issue stands right now. So "that state exists but you cannot get there from
        here" is an ordinary answer, and the log names what WAS available: without that,
        a stuck board looks like a broken integration instead of a workflow that has no
        edge for the jump the outcome policy asked for.
        """
        wanted = (new_state or "").strip().lower()
        if not wanted:
            return False
        resp = await self._request("GET", f"{_API}/issue/{work_item_id}/transitions")
        if resp is None:
            return False
        transitions = (resp.json() or {}).get("transitions") or []
        match = next(
            (t for t in transitions
             if str((t.get("to") or {}).get("name", "")).strip().lower() == wanted
             or str(t.get("name", "")).strip().lower() == wanted),
            None,
        )
        if match is None:
            self._log.error(
                "jira refused the state — no transition leads there from where it stands",
                id=work_item_id, state=new_state,
                available=[str((t.get("to") or {}).get("name", "")) for t in transitions],
                hint="pick one of the available states for this outcome, or add the "
                     "transition in the Jira workflow",
            )
            return False
        done = await self._request(
            "POST", f"{_API}/issue/{work_item_id}/transitions",
            json={"transition": {"id": match.get("id")}},
        )
        return done is not None

    # ── mapping ──────────────────────────────────────────────────────────────

    def _map(self, issue: dict[str, Any]) -> WorkItemInfo:
        f: dict[str, Any] = issue.get("fields") or {}
        assignee = f.get("assignee") or {}
        reporter = f.get("reporter") or {}
        parent = f.get("parent") or {}
        priority = str((f.get("priority") or {}).get("name") or "").lower()
        return WorkItemInfo(
            # The numeric id, not the key: it is what the database, the branch names and
            # every page are keyed on.
            id=int(issue.get("id") or 0),
            key=str(issue.get("key") or ""),
            provider=PROVIDER_JIRA,
            title=str(f.get("summary") or ""),
            # The project name the rest of the app routes on, so `scoped_for_project`
            # finds this workspace again.
            project=self._project,
            work_item_type=str((f.get("issuetype") or {}).get("name") or ""),
            state=str((f.get("status") or {}).get("name") or ""),
            assigned_to=assignee.get("displayName") or None,
            assigned_to_email=assignee.get("emailAddress") or None,
            assigned_to_id=assignee.get("accountId") or None,
            description=_adf_to_text(f.get("description")) or None,
            parent_id=int(parent["id"]) if str(parent.get("id") or "").isdigit() else None,
            tags=[str(t) for t in (f.get("labels") or [])],
            changed_date=_as_dt(f.get("updated")),
            created_date=_as_dt(f.get("created")),
            created_by=reporter.get("displayName") or None,
            priority={"highest": 1, "high": 2, "medium": 3, "low": 4, "lowest": 4}.get(
                priority, 3
            ),
            category=TaskCategory.UNKNOWN,
        )
