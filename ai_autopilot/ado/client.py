"""Azure DevOps REST API client (ported from ``AdoClient``).

Talks to the ADO REST API 7.1 directly with httpx (this runs *outside* Claude
Code, so it does not use MCP). Auth headers are injected per-request via
``AdoAuthService`` to support both PAT and OAuth token refresh.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from ai_autopilot.ado.auth import AdoAuthService
from ai_autopilot.config import BOT_COMMENT_PREFIX, Settings, is_bot_signed
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import TaskCategory, WorkItemInfo

_API = "api-version=7.1"
# ADO's cap on the ids= batch GET. Anything past this must be a second request, not
# dropped: silently returning the first 200 of 300 ids reads as "the other 100 don't exist".
_MAX_IDS_PER_BATCH = 200
# The work-item type → state map costs 1 + N requests to build (19 on a stock process
# template) and only changes when someone edits the process, so a short cache turns
# every reader of it into a free lookup.
_TYPE_STATE_TTL_SECONDS = 300.0


def _terse(body: str, limit: int = 300) -> str:
    """One-line, bounded view of an error body — enough to name the cause in a log.

    ADO error bodies are sometimes an HTML page rather than JSON, so this collapses
    whitespace instead of dumping a screenful into the log.
    """
    return " ".join((body or "").split())[:limit]
_FIELDS = (
    "System.Id,System.Title,System.TeamProject,System.WorkItemType,System.State,"
    "System.AssignedTo,System.Description,Microsoft.VSTS.Common.AcceptanceCriteria,"
    "System.Parent,System.Tags,System.AreaPath,System.IterationPath,System.ChangedDate,"
    "System.CreatedDate,"
    "Microsoft.VSTS.Common.Priority,System.CreatedBy"
)
# Bound on the id → project memo. Work-item ids only ever accumulate, and the memo
# exists to save a lookup, not to be authoritative — so it is capped and dropped
# oldest-first rather than allowed to grow for the life of the process.
_MAX_PROJECT_MEMO = 5000

# ADO relation types used for dependency-aware scheduling (P1).
_REL_PREDECESSOR = "System.LinkTypes.Dependency-Reverse"  # this item depends on target
_REL_SUCCESSOR = "System.LinkTypes.Dependency-Forward"    # target depends on this item
_REL_RELATED = "System.LinkTypes.Related"                 # soft conflict (touch same area)


def _wiql_lit(value: object) -> str:
    """Escape a value for safe inclusion inside a single-quoted WIQL string literal.

    ``ado_project``, ``trigger_states``, trigger tags, etc. are all editable at
    runtime via the dashboard Settings form, so they are untrusted input, not
    constants. A stray apostrophe would otherwise break out of the literal and
    rewrite the WIQL predicate (or hard-fail every poll). WIQL escapes ``'`` by
    doubling it.
    """
    return str(value).replace("'", "''")


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
        # project → (fetched_at, {type: [state dicts]}) — see _type_state_map. Keyed by
        # project because the process template (and therefore the types and their
        # states) is a per-project thing: sharing one cache across projects would hand
        # a Bug's states to a project that has no Bug type.
        self._type_states: dict[str, tuple[float, dict[str, list[dict]]]] = {}
        # work-item id → its System.TeamProject, learned whenever we map an item.
        # Saves a round trip on the project-scoped endpoints (comments).
        self._item_projects: dict[int, str] = {}

    def refresh(self) -> None:
        """Re-read the organization URL after a live config change."""
        self._base = self._config.ado_organization.rstrip("/")
        self._type_states = {}  # a different org/project has different types
        self._item_projects = {}

    async def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        headers = await self._auth.get_auth_header()
        headers["Content-Type"] = content_type
        return headers

    async def _send(
        self, method: str, url: str, *, retries: int = 2, **kwargs
    ) -> httpx.Response:
        """HTTP send with bounded backoff on ADO throttling (429) / transient 5xx.

        Use ONLY for idempotent reads (GET, WIQL queries) — NEVER for mutations,
        which must not be replayed. Without this a single 429 surfaces as an empty
        result and the caller acts on a false 'nothing to do' (mis-scheduling)."""
        resp = await self._http.request(method, url, **kwargs)
        for attempt in range(retries):
            if resp.status_code not in (429, 502, 503, 504):
                return resp
            ra = resp.headers.get("Retry-After", "")
            delay = float(ra) if ra.replace(".", "", 1).isdigit() else min(2**attempt, 8)
            self._log.warning(
                "ADO throttled/5xx — backing off",
                status=resp.status_code, attempt=attempt + 1, delay=delay,
            )
            await asyncio.sleep(delay)
            resp = await self._http.request(method, url, **kwargs)
        return resp

    def _url(self, path: str, project: str = "") -> str:
        """Work-item API URL scoped to a project — ``project`` when given, else the
        default ``ado_project``.

        Only use this for the endpoints ADO genuinely scopes to a project (comments,
        work-item creation, the type/state map). Reads and PATCHes by id must go
        through :meth:`_org_url`: with several projects polled, an id from project B
        addressed under project A's route 404s."""
        name = (project or self._config.ado_project).strip()
        return f"{self._base}/{quote(name, safe='')}/_apis/{path}"

    def _org_url(self, path: str) -> str:
        """Organization-level API URL — no project segment.

        ADO resolves a work-item id org-wide, so ``GET``/``PATCH``/``wiql`` all work
        here. That is what lets ONE query span every configured project instead of one
        request per project, and what makes an item's own project irrelevant to reading
        or updating it."""
        return f"{self._base}/_apis/{path}"

    def _git_url(self, path: str, project: str = "") -> str:
        """Git / PR / build API URL — scoped to the code project for ``project``
        (repos and PRs may live in a different project than the work items).

        ``project`` is the WORK-ITEM project whose code we want; blank asks for the
        default. See ``Settings.code_project_for`` for the resolution order."""
        return f"{self._base}/{quote(self._config.code_project_for(project), safe='')}/_apis/{path}"

    def _project_clause(self) -> str:
        """WIQL predicate restricting a query to the configured work-item project(s).

        One project → equality (what a single-project setup has always emitted);
        several → ``IN``, so all of them come back from a SINGLE org-level query."""
        projects = self._config.effective_ado_projects
        if not projects:
            # Nothing configured: emit the empty-equality the single-project code always
            # emitted (matches no item) rather than an empty string, which would splice
            # a dangling AND into every caller's WIQL and hard-fail the whole query.
            return "[System.TeamProject] = ''"
        if len(projects) == 1:
            return f"[System.TeamProject] = '{_wiql_lit(projects[0])}'"
        joined = ", ".join(f"'{_wiql_lit(p)}'" for p in projects)
        return f"[System.TeamProject] IN ({joined})"

    def _remember_projects(self, items: list[WorkItemInfo]) -> None:
        """Memoise id → project for the project-scoped endpoints (see ``_item_projects``)."""
        for item in items:
            if item.project:
                self._item_projects[item.id] = item.project
        overflow = len(self._item_projects) - _MAX_PROJECT_MEMO
        if overflow > 0:  # dict preserves insertion order → drops the oldest entries
            for key in list(self._item_projects)[:overflow]:
                self._item_projects.pop(key, None)

    async def _project_for(self, work_item_id: int) -> str:
        """Which project work item ``work_item_id`` lives in.

        Answered from the memo when we have already seen the item (the normal case —
        the poller maps an item before ever commenting on it). With a single project
        configured there is nothing to resolve. Otherwise the item is fetched once, and
        a failed lookup falls back to the default project rather than raising: the
        caller's job (posting a comment) is better attempted than abandoned."""
        cached = self._item_projects.get(work_item_id)
        if cached:
            return cached
        projects = self._config.effective_ado_projects
        if len(projects) <= 1:
            return self._config.ado_project
        await self.get_work_item(work_item_id)  # fills the memo via _work_item_batch
        return self._item_projects.get(work_item_id) or self._config.ado_project

    def _tag_clause(self) -> str:
        """WIQL predicate matching ANY configured trigger tag, e.g.
        ``([System.Tags] CONTAINS 'a' OR [System.Tags] CONTAINS 'b')``."""
        tags = self._config.effective_trigger_tags or ["autopilot"]
        ors = " OR ".join(f"[System.Tags] CONTAINS '{_wiql_lit(t)}'" for t in tags)
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
                # Note the two CONTAINS mean different things here. On System.Tags, ADO
                # matches a WHOLE tag (verified: 'phong-autop' matches nothing while
                # 'phong-autopilot' matches). On System.AssignedTo it is a real substring,
                # so this clause can over-fetch — `matches_user` is the authoritative gate
                # and rejects the extras, which is the safe order (over-fetch, then narrow).
                parts.append(
                    f"([System.Tags] CONTAINS '{atag}' "
                    f"AND [System.AssignedTo] CONTAINS '{user}')"
                )
            else:  # no assignee configured → treat the tag like a plain trigger tag
                parts.append(f"[System.Tags] CONTAINS '{atag}'")
        return "(" + " OR ".join(parts) + ")"

    async def get_pending_work_items(self) -> list[WorkItemInfo]:
        """Query work items tagged with any trigger tag in pending states."""
        states = ", ".join(f"'{_wiql_lit(s)}'" for s in self._config.trigger_states) or "'New'"
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE {self._candidate_clause()} "
            f"AND [System.State] IN ({states}) "
            f"AND {self._project_clause()} "
            "ORDER BY [System.ChangedDate] DESC"
        )
        try:
            resp = await self._send(
                "POST",
                self._org_url(f"wit/wiql?{_API}"),
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
            projects=self._config.effective_ado_projects,
            matched=len(ids),
        )
        if not ids:
            return []
        return await self.get_work_items_by_ids(ids)

    async def get_work_items_by_assignee(
        self, assignee: str, states: list[str] | None = None,
        types: list[str] | None = None, top: int = 200,
    ) -> list[WorkItemInfo]:
        """Work items for the Planning workbench — any tag, optionally filtered to
        ``states`` / ``types``.

        ``assignee`` is an email/unique name, or several separated by comma/semicolon
        (a team). **Blank means every assignee** (including unassigned items): planning
        a sprint is a team-wide act, so forcing one name hid everyone else's work and
        made cross-person conflicts invisible — exactly what the conflict analyzer is
        for. Client-side capped at ``top`` (WIQL has no LIMIT), logged when it truncates.
        """
        people = [
            p.replace("'", "''").strip()
            for p in re.split(r"[,;]", assignee or "")
            if p.strip()
        ]
        clauses = [self._project_clause()]
        if len(people) == 1:
            clauses.append(f"[System.AssignedTo] = '{people[0]}'")
        elif people:
            who_list = ", ".join(f"'{p}'" for p in people)
            clauses.append(f"[System.AssignedTo] IN ({who_list})")
        if states:
            state_list = ", ".join(f"'{_wiql_lit(s)}'" for s in states)
            clauses.append(f"[System.State] IN ({state_list})")
        if types:
            type_list = ", ".join(f"'{_wiql_lit(t)}'" for t in types)
            clauses.append(f"[System.WorkItemType] IN ({type_list})")
        wiql = (
            "SELECT [System.Id] FROM WorkItems WHERE "
            + " AND ".join(clauses)
            + " ORDER BY [System.ChangedDate] DESC"
        )
        try:
            resp = await self._http.post(
                self._org_url(f"wit/wiql?{_API}"), json={"query": wiql},
                headers=await self._headers(),
            )
        except httpx.HTTPError as exc:
            self._log.warning("assignee WIQL request error", error=str(exc))
            return []
        text = resp.text.lstrip()
        if resp.status_code >= 400 or not text.startswith("{"):
            self._log.warning("assignee WIQL failed", status=resp.status_code)
            return []
        all_ids = [r["id"] for r in (resp.json().get("workItems") or [])]
        if len(all_ids) > top:
            self._log.info(
                "planning load truncated", matched=len(all_ids), returned=top,
                assignee=assignee or "(everyone)",
            )
        return await self.get_work_items_by_ids(all_ids[:top])

    async def get_all_tagged_work_items(self) -> list[WorkItemInfo]:
        """Query every work item carrying the trigger tag, in ANY state.

        Used by the board to show the full picture (queued, in-flight, done,
        held) — unlike ``get_pending_work_items`` which filters to trigger states.
        """
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE {self._candidate_clause()} "
            f"AND {self._project_clause()} "
            "ORDER BY [System.ChangedDate] DESC"
        )
        try:
            resp = await self._http.post(
                self._org_url(f"wit/wiql?{_API}"),
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

    async def get_all_active_work_items(self, top: int = 300) -> list[WorkItemInfo]:
        """Every work item in the project (ANY tag, ANY assignee), most-recently
        changed first — unlike ``get_pending_work_items``/``get_all_tagged_work_items``
        (both scoped to the trigger tag). Used by the Teams digest's team-standup
        report. Client-side capped at ``top`` (WIQL has no LIMIT clause) — logs when
        the true match count exceeds it so truncation is never silent."""
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE {self._project_clause()} "
            "ORDER BY [System.ChangedDate] DESC"
        )
        try:
            resp = await self._http.post(
                self._org_url(f"wit/wiql?{_API}"), json={"query": wiql},
                headers=await self._headers(),
            )
        except httpx.HTTPError as exc:
            self._log.warning("get_all_active_work_items request error", error=str(exc))
            return []
        text = resp.text.lstrip()
        if resp.status_code >= 400 or not text.startswith("{"):
            self._log.warning("get_all_active_work_items failed", status=resp.status_code)
            return []
        all_ids = [r["id"] for r in (resp.json().get("workItems") or [])]
        if len(all_ids) > top:
            self._log.info(
                "get_all_active_work_items truncated", matched=len(all_ids), returned=top,
            )
        return await self.get_work_items_by_ids(all_ids[:top])

    async def get_state_categories(self) -> dict[str, str]:
        """State name → ADO state category (``Proposed``/``InProgress``/``Resolved``/
        ``Completed``/``Removed``). Best-effort — ``{}`` on any error."""
        out: dict[str, str] = {}
        for states in (await self._type_state_map_all()).values():
            for s in states:
                name = s.get("name")
                if name and name not in out:
                    out[name] = s.get("stateCategory", "")
        return out

    async def get_work_items_by_ids(self, ids: list[int]) -> list[WorkItemInfo]:
        """Fetch work items by id, in batches ADO will accept.

        Previously this truncated to the first 200 ids and returned the rest as though
        they did not exist — silently, so a 201-item planning load simply lost the tail.
        """
        if not ids:
            return []
        out: list[WorkItemInfo] = []
        for start in range(0, len(ids), _MAX_IDS_PER_BATCH):
            out.extend(await self._work_item_batch(ids[start : start + _MAX_IDS_PER_BATCH]))
        return out

    async def _work_item_batch(self, ids: list[int]) -> list[WorkItemInfo]:
        ids_param = ",".join(str(i) for i in ids)
        # errorPolicy=Omit is load-bearing: by default ADO fails the WHOLE request with 404
        # when ANY single id is unreadable (deleted, moved to another project, or just
        # wrong), so one stale link in a batch of 200 used to blank out all 200 — and the
        # callers read that empty list as "none of these work items exist".
        resp = await self._http.get(
            self._org_url(
                f"wit/workitems?ids={ids_param}&fields={_FIELDS}&errorPolicy=Omit&{_API}"
            ),
            headers=await self._auth.get_auth_header(),
        )
        if resp.status_code >= 400:
            # The response body is where ADO names the offending id ("TF401232: Work item
            # 1234 does not exist"). Logging the status alone made this warning
            # unactionable: it said something failed without saying which item or why.
            self._log.warning(
                "get_work_items failed", status=resp.status_code,
                ids=ids if len(ids) <= 20 else f"{ids[:20]}… ({len(ids)} total)",
                detail=_terse(resp.text),
            )
            return []
        values = resp.json().get("value") or []
        # With errorPolicy=Omit ADO returns a null in place of each id it could not read,
        # so the array is positional and _map() would raise on those.
        found = [self._map(wi) for wi in values if wi]
        self._remember_projects(found)
        missing = sorted(set(ids) - {item.id for item in found})
        if missing:
            self._log.info(
                "work items skipped — not readable", ids=missing,
                hint="deleted, or the PAT cannot see them",
            )
        return found

    async def get_raw_work_items(
        self, project: str, since_days: int, top: int = 1000
    ) -> list[dict]:
        """Raw ``fields`` dicts for one project's recently-changed items.

        Process-health metrics read fields that are NOT part of ``_FIELDS`` and are not
        guaranteed to exist at all (``Blocked``, ``Found In Environment`` — a process
        may simply never have defined them). Naming them in the ``fields=`` parameter
        would 400 the whole batch on a project that lacks one, so this fetches the full
        field set and lets the caller take what is there. Scoped per project for the
        same reason: one project's missing field must not blank the other's report.
        """
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = '{_wiql_lit(project)}' "
            f"AND [System.ChangedDate] >= @today-{int(since_days)} "
            "ORDER BY [System.ChangedDate] DESC"
        )
        try:
            resp = await self._http.post(
                self._org_url(f"wit/wiql?{_API}"), json={"query": wiql},
                headers=await self._headers(),
            )
        except httpx.HTTPError as exc:
            self._log.warning("health WIQL request error", project=project, error=str(exc))
            return []
        if resp.status_code >= 400 or not resp.text.lstrip().startswith("{"):
            self._log.warning("health WIQL failed", project=project, status=resp.status_code)
            return []
        ids = [r["id"] for r in (resp.json().get("workItems") or [])][:top]
        out: list[dict] = []
        for start in range(0, len(ids), _MAX_IDS_PER_BATCH):
            chunk = ",".join(str(i) for i in ids[start : start + _MAX_IDS_PER_BATCH])
            try:
                batch = await self._http.get(
                    self._org_url(f"wit/workitems?ids={chunk}&errorPolicy=Omit&{_API}"),
                    headers=await self._auth.get_auth_header(),
                )
            except httpx.HTTPError as exc:
                self._log.warning("health fetch error", project=project, error=str(exc))
                continue
            if batch.status_code >= 400:
                self._log.warning(
                    "health fetch failed", project=project, status=batch.status_code,
                    detail=_terse(batch.text),
                )
                continue
            # ``id`` lives on the work item, NOT inside ``fields`` — folding it in keeps
            # the caller's contract "one dict per item, all of it addressable by ADO
            # reference name" true, instead of silently yielding id-less rows.
            out += [
                {**(wi.get("fields") or {}), "System.Id": wi.get("id")}
                for wi in (batch.json().get("value") or []) if wi
            ]
        return out

    async def get_work_item(self, work_item_id: int) -> WorkItemInfo | None:
        items = await self.get_work_items_by_ids([work_item_id])
        return items[0] if items else None

    async def add_comment(self, work_item_id: int, comment: str) -> bool:
        # Lead with the bot icon so this comment is recognisable as ours at a glance —
        # the comment-reaction loop skips it (see config.BOT_COMMENT_PREFIX).
        # The comments API is one of the few genuinely project-scoped work-item routes,
        # so the item's OWN project decides the URL (see _project_for).
        project = await self._project_for(work_item_id)
        resp = await self._http.post(
            self._url(
                f"wit/workitems/{work_item_id}/comments?api-version=7.1-preview.4", project
            ),
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
        url = self._url(
            f"wit/workItems/{work_item_id}/comments?api-version=7.1-preview.4",
            await self._project_for(work_item_id),
        )
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
            # Name the state and quote ADO's reason. Logging only the status made the
            # commonest cause — a state that does not exist on THIS item's type — look
            # like a generic API failure, so a transition could fail on every item of a
            # type for months without anyone being able to tell why from the log.
            self._log.warning(
                "update_state failed", id=work_item_id, state=new_state,
                status=resp.status_code, detail=_terse(resp.text),
            )
        return resp.status_code < 400

    async def _update_tags_atomic(self, work_item_id: int, mutate, *, what: str) -> bool:
        """Read-modify-write ``System.Tags`` with optimistic concurrency.

        ``mutate(current_tags) -> new_tags | None`` (None = no change). PATCH carries
        a ``test`` op on ``/rev`` so a concurrent tag write (poller + reviewer tracker
        + a human in ADO all touch tags) can't silently clobber the other side — on a
        409/412 conflict we re-read the latest rev and retry, rather than overwriting
        the whole field with a stale snapshot."""
        for attempt in range(3):
            resp = await self._send(
                "GET", self._org_url(f"wit/workitems/{work_item_id}?{_API}"),
                headers=await self._auth.get_auth_header(),
            )
            if resp.status_code >= 400:
                self._log.warning(f"{what}: fetch failed", id=work_item_id, status=resp.status_code)
                return False
            data = resp.json()
            rev = data.get("rev")
            raw_tags = str((data.get("fields") or {}).get("System.Tags", ""))
            current = [t.strip() for t in raw_tags.split(";") if t.strip()]
            new_tags = mutate(current)
            if new_tags is None:
                return True  # nothing to change
            patch = [
                {"op": "test", "path": "/rev", "value": rev},
                {"op": "replace", "path": "/fields/System.Tags", "value": "; ".join(new_tags)},
            ]
            presp = await self._patch(work_item_id, patch)
            if presp.status_code < 400:
                return True
            if presp.status_code in (409, 412):  # someone else won the race — re-read + retry
                self._log.info(f"{what}: tag conflict, retrying", id=work_item_id, attempt=attempt + 1)
                continue
            self._log.warning(f"{what} failed", id=work_item_id, status=presp.status_code)
            return False
        self._log.warning(f"{what}: gave up after repeated tag conflicts", id=work_item_id)
        return False

    async def add_tag(self, work_item_id: int, tag: str) -> bool:
        def mutate(current: list[str]) -> list[str] | None:
            if any(t.lower() == tag.lower() for t in current):
                return None  # already present
            return [*current, tag]

        return await self._update_tags_atomic(work_item_id, mutate, what="add_tag")

    async def remove_tag(self, work_item_id: int, tag: str) -> bool:
        def mutate(current: list[str]) -> list[str] | None:
            remaining = [t for t in current if t.lower() != tag.lower()]
            return None if len(remaining) == len(current) else remaining

        return await self._update_tags_atomic(work_item_id, mutate, what="remove_tag")

    async def create_work_item(
        self, title: str, item_type: str, parent_id: int | None, tag: str,
        description: str = "", project: str = "",
    ) -> int:
        """Create a work item, in ``project`` when given else the default project.

        A child MUST land in its parent's project: ADO cannot make a hierarchy link
        across projects, so decomposing an item from a secondary project into the
        default one would fail the link (or, worse, orphan the children somewhere the
        poller for that stream never looks). Callers pass the parent's project;
        ``parent_id`` alone resolves it when they don't."""
        if not project.strip() and parent_id is not None:
            project = await self._project_for(parent_id)
        patch: list[dict[str, Any]] = [
            {"op": "add", "path": "/fields/System.Title", "value": title},
            {"op": "add", "path": "/fields/System.Tags", "value": tag},
        ]
        if description:
            patch.append(
                {"op": "add", "path": "/fields/System.Description", "value": description}
            )
        if parent_id is not None:
            patch.append(
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
                        # Org-level link URL — ADO resolves the parent by id, and this
                        # cannot then name the wrong project for a cross-project parent.
                        "url": f"{self._base}/_apis/wit/workitems/{parent_id}",
                    },
                }
            )
        url = self._url(f"wit/workitems/${quote(item_type, safe='')}?{_API}", project)
        resp = await self._http.post(
            url, content=_json(patch), headers=await self._headers("application/json-patch+json")
        )
        if resp.status_code >= 400:
            self._log.warning("create_work_item failed", status=resp.status_code, body=resp.text)
            return 0
        return int(resp.json().get("id", 0))

    async def _patch(self, work_item_id: int, patch: list[dict[str, Any]]) -> httpx.Response:
        # Org-level on purpose: ADO resolves an id across the whole organization, so a
        # state/tag update works regardless of which polled project the item is in.
        return await self._http.patch(
            self._org_url(f"wit/workitems/{work_item_id}?{_API}"),
            content=_json(patch),
            headers=await self._headers("application/json-patch+json"),
        )

    # ── Git / Pull Request API (PR babysitter) ──────────────────────────────

    async def get_repositories(self) -> list[dict[str, Any]]:
        resp = await self._send(
            "GET",
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
        resp = await self._send("GET", url, headers=await self._auth.get_auth_header())
        if resp.status_code >= 400:
            self._log.warning("get_pull_requests failed", status=resp.status_code, pr_status=status)
            return []
        return resp.json().get("value") or []

    async def get_pull_request(self, repo_id: str, pr_id: int, project: str = "") -> dict | None:
        resp = await self._send(
            "GET",
            self._git_url(f"git/repositories/{repo_id}/pullrequests/{pr_id}?{_API}", project),
            headers=await self._auth.get_auth_header(),
        )
        if resp.status_code >= 400:
            self._log.warning("get_pull_request failed", pr=pr_id, status=resp.status_code)
            return None
        return resp.json()

    async def get_pull_request_work_items(
        self, repo_id: str, pr_id: int, project: str = ""
    ) -> list[int]:
        """Ids of the work items linked to a PR (empty when none, or on error)."""
        resp = await self._send(
            "GET",
            self._git_url(
                f"git/repositories/{repo_id}/pullrequests/{pr_id}/workitems?{_API}", project
            ),
            headers=await self._auth.get_auth_header(),
        )
        if resp.status_code >= 400:
            self._log.warning(
                "get_pull_request_work_items failed", pr=pr_id, status=resp.status_code
            )
            return []
        return [
            int(w["id"]) for w in (resp.json().get("value") or [])
            if str(w.get("id", "")).isdigit()
        ]

    async def link_work_item_to_pr(
        self, work_item_id: int, project_guid: str, repo_guid: str, pr_id: int, pr_url: str = ""
    ) -> bool:
        """Attach a pull request to a work item as an ``ArtifactLink``.

        This is the ONLY way the link exists: ADO stores it on the work item, not on the
        PR, so a PR opened without it shows "no work item" forever and the traceability
        the process depends on — which ticket shipped this change — is simply absent.
        The artifact URI wants the GUIDs, URL-encoded, in the order project/repo/pr.
        """
        artifact = (
            f"vstfs:///Git/PullRequestId/{project_guid}%2F{repo_guid}%2F{pr_id}"
        )
        patch = [{
            "op": "add", "path": "/relations/-",
            "value": {
                "rel": "ArtifactLink", "url": artifact,
                "attributes": {"name": "Pull Request"},
            },
        }]
        resp = await self._patch(work_item_id, patch)
        if resp.status_code >= 400:
            body = _terse(resp.text)
            # A link that is already there comes back 400 with this message; that is the
            # desired end state, not a failure — reporting it as one would make the
            # caller retry (and comment) on every poll.
            if "RelationAlreadyExists" in body or "already exists" in body.lower():
                return True
            self._log.warning(
                "link_work_item_to_pr failed", id=work_item_id, pr=pr_id,
                status=resp.status_code, detail=body,
            )
            return False
        self._log.info("linked PR to work item", id=work_item_id, pr=pr_id, url=pr_url)
        return True

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
            f"AND {self._project_clause()}"
        )
        try:
            resp = await self._http.post(
                self._org_url(f"wit/wiql?{_API}"), json={"query": wiql},
                headers=await self._headers(),
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
                self._org_url(f"wit/workitems?ids={ids_param}&$expand=relations&{_API}"),
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

    async def get_connection_data(self) -> dict[str, Any] | None:
        """The identity behind this client's credentials (PAT/OAuth) — used by the
        reviewer tracker to recognise "the bot was added as a reviewer" without any
        configuration. Returns ``{"id", "display_name", "unique_name"}`` or ``None``
        on any error (the tracker then falls back to ``pr_bot_identity``)."""
        url = f"{self._base}/_apis/connectionData"
        try:
            resp = await self._http.get(url, headers=await self._auth.get_auth_header())
        except httpx.HTTPError as exc:
            self._log.warning("get_connection_data error", error=str(exc))
            return None
        if resp.status_code >= 400 or not resp.text.lstrip().startswith("{"):
            self._log.warning("get_connection_data failed", status=resp.status_code)
            return None
        user = resp.json().get("authenticatedUser") or {}
        props = user.get("properties") or {}
        unique = (props.get("Account") or {}).get("$value") or user.get("providerDisplayName")
        return {
            "id": user.get("id") or "",
            "display_name": user.get("customDisplayName") or user.get("providerDisplayName") or "",
            "unique_name": unique or "",
        }

    async def get_pull_request_reviewers(
        self, repo_id: str, pr_id: int
    ) -> list[dict[str, Any]]:
        """Current reviewers of a PR (fresh — the PR-list payload can lag a vote)."""
        url = self._git_url(f"git/repositories/{repo_id}/pullRequests/{pr_id}/reviewers?{_API}")
        try:
            resp = await self._http.get(url, headers=await self._auth.get_auth_header())
        except httpx.HTTPError as exc:
            self._log.warning("get_pr_reviewers error", pr=pr_id, error=str(exc))
            return []
        if resp.status_code >= 400:
            self._log.warning("get_pr_reviewers failed", pr=pr_id, status=resp.status_code)
            return []
        return resp.json().get("value") or []

    async def add_pull_request_reviewer(
        self, repo_id: str, pr_id: int, reviewer_id: str, *, required: bool = False
    ) -> bool:
        """Add an identity as a reviewer on a PR (idempotent — a re-PUT is a no-op).

        ``reviewer_id`` is the ADO identity GUID (``System.AssignedTo``'s ``id``), not an
        email. ``required`` marks them a *required* reviewer, which blocks completion until
        they vote. Same endpoint as :meth:`cast_pull_request_vote` but with ``vote: 0`` —
        added, not voting — so the human's own verdict is never pre-empted.
        """
        url = self._git_url(
            f"git/repositories/{repo_id}/pullRequests/{pr_id}/reviewers/{reviewer_id}?{_API}"
        )
        try:
            resp = await self._http.put(
                url, json={"vote": 0, "isRequired": required}, headers=await self._headers()
            )
        except httpx.HTTPError as exc:
            self._log.warning("add_pr_reviewer error", pr=pr_id, error=str(exc))
            return False
        if resp.status_code >= 400:
            # Most common cause is ADO refusing the PR's own author as a reviewer — a
            # warning, never fatal: the PR exists and the run must not be failed for it.
            self._log.warning(
                "add_pr_reviewer failed", pr=pr_id, reviewer=reviewer_id,
                status=resp.status_code, body=resp.text[:200],
            )
            return False
        return True

    async def cast_pull_request_vote(
        self, repo_id: str, pr_id: int, reviewer_id: str, vote: int
    ) -> bool:
        """Cast the authenticated identity's vote on a PR (it must already be — or is
        implicitly added as — a reviewer). ADO scale: 10 approved, 5 approved with
        suggestions, 0 no vote, -5 waiting for author, -10 rejected."""
        url = self._git_url(
            f"git/repositories/{repo_id}/pullRequests/{pr_id}/reviewers/{reviewer_id}?{_API}"
        )
        try:
            resp = await self._http.put(
                url, json={"vote": vote}, headers=await self._headers()
            )
        except httpx.HTTPError as exc:
            self._log.warning("cast_pr_vote error", pr=pr_id, error=str(exc))
            return False
        if resp.status_code >= 400:
            self._log.warning(
                "cast_pr_vote failed", pr=pr_id, vote=vote, status=resp.status_code,
                body=resp.text[:200],
            )
            return False
        return True

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

    # ── Work-item states (for the Settings picker + the Flow editor) ─────────

    async def _type_state_map(self, project: str = "") -> dict[str, list[dict]]:
        """``{work-item type: [raw state dicts]}`` for one project, briefly cached.

        This is the primitive the three public readers share. It matters that the
        per-type grouping is preserved here: ADO states belong to a TYPE, and every
        caller that flattened them lost the only fact that makes a transition valid or
        invalid (``Ready to Deploy`` exists on Bug and Task but on no other type, so a
        single project-wide state list cannot tell you whether setting it will 400).

        One walk costs 1 + N requests (19 on a stock process template), which is why
        the result is cached rather than re-fetched per reader. Best-effort throughout:
        an error yields ``{}`` (or omits the failing type) so the pages that depend on
        this still render with a manual-entry fallback.
        """
        project = (project or self._config.ado_project or "").strip()
        cached = self._type_states.get(project.lower())
        if cached is not None and time.monotonic() - cached[0] < _TYPE_STATE_TTL_SECONDS:
            return cached[1]
        out: dict[str, list[dict]] = {}
        try:
            resp = await self._http.get(
                self._url(f"wit/workitemtypes?{_API}", project),
                headers=await self._auth.get_auth_header(),
            )
            if resp.status_code >= 400:
                self._log.warning("list work-item types failed", status=resp.status_code,
                                  detail=_terse(resp.text))
                return {}
            types = [
                t["name"]
                for t in (resp.json().get("value") or [])
                if t.get("name") and not t.get("isDisabled")
            ]
            for type_name in types:
                sresp = await self._http.get(
                    self._url(
                        f"wit/workitemtypes/{quote(type_name, safe='')}/states?{_API}", project
                    ),
                    headers=await self._auth.get_auth_header(),
                )
                if sresp.status_code >= 400:
                    self._log.warning("list states failed for type", type=type_name,
                                      status=sresp.status_code)
                    continue
                out[type_name] = [s for s in (sresp.json().get("value") or []) if s.get("name")]
        except httpx.HTTPError as exc:
            self._log.warning("work-item type/state request error", error=str(exc))
            return {}
        self._type_states[project.lower()] = (time.monotonic(), out)
        return out

    async def _type_state_map_all(self) -> dict[str, list[dict]]:
        """``_type_state_map`` unioned over every polled project.

        The Settings state picker and the Flow editor offer states for the WHOLE
        instance, so with several projects they must show the union — a state that
        exists only in the second project is still one an operator needs to pick. Types
        that exist in more than one project keep the FIRST project's state list; the
        alternative (merging state lists across projects) would invent combinations no
        single project actually accepts, which is exactly what the per-type grouping is
        there to prevent."""
        projects = self._config.effective_ado_projects
        if len(projects) <= 1:
            return await self._type_state_map(projects[0] if projects else "")
        merged: dict[str, list[dict]] = {}
        for project in projects:
            for type_name, states in (await self._type_state_map(project)).items():
                merged.setdefault(type_name, states)
        return merged

    async def get_states_by_type(self) -> dict[str, list[str]]:
        """``{work-item type: [state names]}`` in the type's own board order.

        The Flow editor and its validation both need this: a state is only settable on
        the types that actually define it.
        """
        return {
            type_name: [s["name"] for s in sorted(states, key=lambda s: int(s.get("order", 0)))]
            for type_name, states in (await self._type_state_map_all()).items()
        }

    async def get_states(self) -> list[str]:
        """Distinct work-item states configured in the project, board-ordered.

        Best-effort: returns ``[]`` on any error (offline / bad PAT) so the
        Settings page can always render with a manual-entry fallback. Aggregates
        distinct names across every type, ordered by state category (Proposed →
        InProgress → Resolved → Completed → Removed) then the type-defined order.
        """
        # Lower rank = shown first.
        cat_rank = {"Proposed": 0, "InProgress": 1, "Resolved": 2, "Completed": 3, "Removed": 4}
        # name → (category_rank, order) — keep the strongest (lowest) seen.
        seen: dict[str, tuple[int, int]] = {}
        for states in (await self._type_state_map_all()).values():
            for s in states:
                name = s["name"]
                key = (cat_rank.get(s.get("stateCategory", ""), 9), int(s.get("order", 0)))
                if name not in seen or key < seen[name]:
                    seen[name] = key
        return [name for name, _ in sorted(seen.items(), key=lambda kv: (kv[1], kv[0]))]

    # ── Field mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _map(wi: dict[str, Any]) -> WorkItemInfo:
        f: dict[str, Any] = wi.get("fields", {})
        tags = [t.strip() for t in str(f.get("System.Tags", "")).split(";") if t.strip()]
        return WorkItemInfo(
            id=wi["id"],
            title=str(f.get("System.Title", "")),
            project=str(f.get("System.TeamProject", "")),
            work_item_type=str(f.get("System.WorkItemType", "")),
            state=str(f.get("System.State", "")),
            assigned_to=_identity(f.get("System.AssignedTo")),
            assigned_to_email=_identity_email(f.get("System.AssignedTo")),
            assigned_to_id=_identity_id(f.get("System.AssignedTo")),
            description=f.get("System.Description"),
            acceptance_criteria=f.get("Microsoft.VSTS.Common.AcceptanceCriteria"),
            parent_id=_as_int(f.get("System.Parent")),
            tags=tags,
            area_path=f.get("System.AreaPath"),
            iteration_path=f.get("System.IterationPath"),
            changed_date=_as_dt(f.get("System.ChangedDate")),
            created_date=_as_dt(f.get("System.CreatedDate")),
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


def _identity_id(value: Any) -> str | None:
    """The identity GUID — the only form the PR *reviewers* endpoint accepts."""
    if isinstance(value, dict):
        ident = value.get("id")
        return str(ident) if ident else None
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
