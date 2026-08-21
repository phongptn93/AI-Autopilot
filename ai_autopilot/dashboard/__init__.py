"""Server-rendered dashboard (replaces the Blazor dashboard)."""

from __future__ import annotations

import contextlib
import json
import secrets
import time
from collections import Counter, OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ai_autopilot import (
    activity,
    delivery,
    security,
    spec_drift,
)
from ai_autopilot import (
    flows as flows_mod,
)
from ai_autopilot import (
    workspaces as workspaces_mod,
)
from ai_autopilot.board import board_columns, build_board, latest_records, parse_drop_map
from ai_autopilot.config import config_file_path
from ai_autopilot.container import Container
from ai_autopilot.dashboard import settings_form
from ai_autopilot.data.entities import PipelineState, QualityKind
from ai_autopilot.logging_config import get_logger
from ai_autopilot.services import delivery_report, planning_analyzer
from ai_autopilot.services.pr_feedback import parse_work_item_id
from ai_autopilot.services.spec_guard import SpecGuard
from ai_autopilot.services.task_room import TaskRoomService
from ai_autopilot.skills_catalog import discover_skills
from ai_autopilot.workspace import discover_repos

_log = get_logger("dashboard")

def _group_drifts(rows: list) -> list:
    """Deviations grouped by work item — the unit a BA actually edits.

    A flat list would have them open the same item once per deviation, and the "mark
    updated" action is per item too. Order of first appearance is kept, so the newest
    report stays at the top.
    """
    groups: OrderedDict[int, dict] = OrderedDict()
    for row in rows:
        group = groups.get(row.work_item_id)
        if group is None:
            group = groups[row.work_item_id] = {
                "work_item_id": row.work_item_id, "title": row.title,
                "project": row.project, "pr_url": row.pr_url,
                "created_at": row.created_at, "resolved_at": row.resolved_at,
                "resolved_by": row.resolved_by, "rows": [],
            }
        group["rows"].append(row)
        group["pr_url"] = group["pr_url"] or row.pr_url
    return [_DriftGroup(**g) for g in groups.values()]


class _DriftGroup(dict):
    """Attribute access for the template (``group.rows`` reads better than ``group['rows']``)."""

    __getattr__ = dict.get


_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ── Flash messages ────────────────────────────────────────────────────────────
# The outcome of a POST used to travel in the query string of the redirect that follows
# it (`/dashboard/settings?saved=1`, `?import_error=<text>`). The POST itself was always
# correct; the problem is what the GET then carries:
#
#   • refreshing the page replays the banner, claiming a save that did not happen
#   • the URL is bookmarkable and shareable WITH the banner, so it lies later
#   • free-form error text has to be URL-encoded into it, which caps its length and
#     leaves an unreadable address bar
#
# A one-shot cookie fixes all three: it is set on the POST response, consumed and cleared
# on the next GET, and never appears in the URL. Only a short CODE travels — the wording
# lives here, so nothing user-supplied is ever echoed back into the page.
_FLASH_COOKIE = "autopilot_flash"

FLASH_MESSAGES: dict[str, tuple[str, str]] = {
    "saved": ("green", "✅ Đã lưu và áp dụng. Field có nhãn <em>restart</em> cần khởi động lại."),
    "reloaded": ("green", "↻ Đã nạp lại cấu hình từ file vào tiến trình đang chạy."),
    "imported": ("green", "⬆ Đã nạp và áp dụng cấu hình. PAT và trigger tag của máy này "
                          "không đổi."),
    "imported_full": ("green", "🔓 Đã restore cấu hình đầy đủ (kèm secret). Khởi động lại để "
                               "áp dụng phần lồng nhau (tenants, repos)."),
    "flow_saved": ("green", "✅ Đã lưu flow theo work item type và áp dụng ngay "
                            "(không cần khởi động lại)."),
    "flow_invalid": ("red", "⛔ Chưa lưu — xem các lỗi bên dưới. Giá trị bạn vừa nhập "
                            "vẫn được giữ."),
    "ws_saved": ("green", "✅ Đã lưu workspace và áp dụng ngay (không cần khởi động lại)."),
    "ws_invalid": ("red", "⛔ Chưa lưu — xem các lỗi bên dưới. Giá trị bạn vừa nhập "
                          "vẫn được giữ."),
    "err_no_file": ("red", "⚠️ Chưa chọn tệp."),
    "err_invalid": ("red", "⚠️ Tệp không hợp lệ hoặc không phải YAML cấu hình."),
    "err_nothing": ("red", "⚠️ Tệp không chứa setting nào áp dụng được."),
    "err_password": ("red", "⚠️ Cần mật khẩu của tệp."),
    "err_wrong_password": ("red", "⚠️ Sai mật khẩu, hoặc tệp bị hỏng."),
    "err_no_export_password": (
        "red",
        "⚠️ Chưa đặt <b>Full-export password</b> — file sẽ không được bảo vệ. Đặt "
        "<code>config_export_password</code> ở mục <b>Web / Security</b> rồi export lại.",
    ),
}


def _flash(url: str, code: str) -> RedirectResponse:
    """Redirect to ``url`` carrying a one-shot ``code`` in a cookie, not the query string."""
    response = RedirectResponse(url, status_code=303)
    if code in FLASH_MESSAGES:
        response.set_cookie(
            _FLASH_COOKIE, code, max_age=30, httponly=True, samesite="lax",
            path="/dashboard",
        )
    else:  # a code we don't have wording for would show nothing — fail loudly in the log
        _log.error("unknown flash code — no banner will be shown", code=code)
    return response


def _take_flash(request: Request) -> tuple[str, str] | None:
    """``(colour, message)`` for this request's flash, or None. Caller must clear it."""
    return FLASH_MESSAGES.get(request.cookies.get(_FLASH_COOKIE) or "")


# A rejected Flow save has to carry two things back to the GET: the reasons (free text,
# one per problem) and the values the operator typed, so their work isn't thrown away.
# Neither fits the flash cookie — a cookie is ~4KB and this is unbounded prose — so the
# payload is held server-side under a random token and only the token travels.
_FLOW_ERROR_COOKIE = "autopilot_flow_errors"
_FLOW_REJECTS: OrderedDict[str, dict] = OrderedDict()
_FLOW_REJECTS_MAX = 32   # bounded: this is a hand-off buffer, not storage


def _flow_reject(errors: list[str], flows: list[dict]) -> RedirectResponse:
    token = secrets.token_urlsafe(12)
    _FLOW_REJECTS[token] = {"errors": errors, "flows": flows}
    while len(_FLOW_REJECTS) > _FLOW_REJECTS_MAX:
        _FLOW_REJECTS.popitem(last=False)
    response = _flash("/dashboard/flow", "flow_invalid")
    response.set_cookie(
        _FLOW_ERROR_COOKIE, token, max_age=120, httponly=True, samesite="lax",
        path="/dashboard",
    )
    return response


def _take_flow_reject(request: Request) -> dict:
    """The pending rejection for this request (``{}`` if none). Consumes it."""
    return _FLOW_REJECTS.pop(request.cookies.get(_FLOW_ERROR_COOKIE) or "", {})


# Same hand-off as the Flow editor, for the same reason: a rejected save must come back
# with both the reasons and what the operator typed.
_WS_ERROR_COOKIE = "autopilot_ws_errors"
_WS_REJECTS: OrderedDict[str, dict] = OrderedDict()


def _ws_reject(errors: list[str], views: list) -> RedirectResponse:
    token = secrets.token_urlsafe(12)
    _WS_REJECTS[token] = {"errors": errors, "views": views}
    while len(_WS_REJECTS) > _FLOW_REJECTS_MAX:
        _WS_REJECTS.popitem(last=False)
    response = _flash("/dashboard/workspaces", "ws_invalid")
    response.set_cookie(
        _WS_ERROR_COOKIE, token, max_age=120, httponly=True, samesite="lax",
        path="/dashboard",
    )
    return response


def _take_ws_reject(request: Request) -> dict:
    return _WS_REJECTS.pop(request.cookies.get(_WS_ERROR_COOKIE) or "", {})


# Which workspace the dashboard is currently looking at. A cookie rather than a config
# field on purpose: this is a VIEW filter, personal to the browser, and it must never
# be mistaken for a switch that changes what the autopilot processes.
_WS_COOKIE = "autopilot_workspace"


def selected_workspace(request: Request) -> str:
    """The workspace id this request is scoped to — ``"all"`` when unscoped.

    A query parameter wins over the cookie so a link can carry the scope (and so the
    selector itself works without JavaScript)."""
    return (request.query_params.get("workspace")
            or request.cookies.get(_WS_COOKIE)
            or "all").strip()


def scope_of(request: Request, config) -> tuple[str, list[str] | None]:
    """``(workspace_id, projects_to_show)`` for this request.

    ``projects_to_show`` is ``None`` for "everything" and a (possibly empty) list for a
    specific workspace — the two are NOT interchangeable: an empty workspace must show
    nothing, not everything."""
    workspace_id = selected_workspace(request)
    return workspace_id, workspaces_mod.scope_projects(config, workspace_id)

_CATEGORY_LABELS = {
    "backendtask": ("BE", "cat-be"),
    "frontendtask": ("FE", "cat-fe"),
    "bug": ("Bug", "cat-bug"),
    "databasetask": ("DB", "cat-db"),
    "testtask": ("QC", "cat-qc"),
    "requirement": ("Req", "cat-req"),
}
_STATUS_CLASS = {
    "Success": "badge-success",
    "Failed": "badge-failed",
    "Running": "badge-running",
}
# Baseline work-item types offered in the Planning filter (merged with whatever
# types the loaded items actually have, so custom process types show up too).
_COMMON_WI_TYPES = ("Bug", "Task", "User Story", "Requirement", "Feature", "Test Case")


def _pr_age(created: str | None) -> str:
    """Human-readable age of a PR from its ISO creationDate (best-effort)."""
    if not created:
        return ""
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return ""
    from datetime import timezone
    delta = datetime.now(timezone.utc) - dt
    days, secs = delta.days, delta.seconds
    if days >= 1:
        return f"{days}d"
    if secs >= 3600:
        return f"{secs // 3600}h"
    return f"{max(1, secs // 60)}m"


def _pr_status(pr: dict, approved: int, blocked: int, pending: int, conflicts: bool) -> str:
    """A single overall status token for the board badge."""
    if pr.get("isDraft"):
        return "draft"
    if conflicts:
        return "conflicts"
    if blocked:
        return "blocked"
    if approved and not pending:
        return "approved"
    if approved:
        return "partial"
    return "awaiting"


def _category_badge(category: str) -> tuple[str, str]:
    label, css = _CATEGORY_LABELS.get((category or "").lower(), (category or "—", "cat-default"))
    return label, css


def _status_class(status: str) -> str:
    return _STATUS_CLASS.get(status, "badge-retrying")


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


# PR-outcome figures are an ADO round-trip per repo per status — cache them briefly
# so Overview refreshes don't hammer the API. Module-level: one cache per process.
_PR_OUTCOME_CACHE: dict = {"at": 0.0, "data": None}
_PR_OUTCOME_TTL = 60.0

# The Reviews board scans every repo × active PR × reviewers on each load — cache the
# assembled view briefly so refreshes don't hammer ADO (the tracker updates state on its
# own 30s cadence anyway).
# Label + colour tone per action kind, in the order the report already sorts them.
# Kept here rather than in delivery.py so the pure module stays free of presentation.
_ACTION_LABELS: dict[str, tuple[str, str]] = {
    delivery.KIND_BLOCKED_PR: ("PR bị từ chối", "red"),
    delivery.KIND_MERGE_READY: ("Chờ merge", "red"),
    delivery.KIND_REVIEW_WAITING: ("Chờ review", "amber"),
    delivery.KIND_NEEDS_HUMAN: ("Autopilot cần người", "amber"),
    delivery.KIND_STALE: ("Đứng im", "grey"),
    delivery.KIND_FAILED: ("Run thất bại", "grey"),
}


def work_item_link_base(cfg) -> str:
    """Base URL for "open work item #id in Azure DevOps" links, or "" with no org.

    With several work-item projects polled, most pages do not know which project a
    given id belongs to (the DB records an id, not a project). Azure DevOps resolves
    a work item id ORG-wide, so the project-less form redirects to the right project —
    which is exactly right here, and strictly better than guessing the default project
    and handing the reader a link that 404s. A single-project setup keeps the explicit,
    unchanged URL."""
    org = (cfg.ado_organization or "").rstrip("/")
    if not org:
        return ""
    projects = cfg.effective_ado_projects
    if len(projects) == 1 and projects[0]:
        return f"{org}/{quote(projects[0])}/_workitems/edit"
    return f"{org}/_workitems/edit"


_REVIEWS_CACHE: dict = {"at": 0.0, "data": None}
_REVIEWS_TTL = 30.0


async def _pr_outcomes(c: Container) -> dict:
    """Merged / active / abandoned counts of the autopilot's OWN PRs, across every repo —
    the denominator for "is this actually shipping work, and at what cost".

    A PR is ours when its branch names a work item this autopilot has an execution record
    for. It used to be "the branch starts with a bot prefix", which was wrong in both
    directions and made the cost-per-PR figure meaningless:

      * ``feature/`` and ``fix/`` are what the team names branches too, so on a real project
        this counted 102 merged PRs when 7 were the autopilot's — reporting its cost per
        shipped PR as 719k tokens instead of 10.5M, 14x too cheap.
      * an agent-chosen prefix that isn't on the list (``dxfac/feature/6526-…``) was skipped
        even though it was ours.

    ``ok`` is False when the scan itself failed. Returning silent zeros made a throttled ADO
    look identical to "you have never shipped anything" — 1 + 3N requests per uncached load
    is enough that this does happen.
    """
    now = time.monotonic()
    if _PR_OUTCOME_CACHE["data"] is not None and now - _PR_OUTCOME_CACHE["at"] < _PR_OUTCOME_TTL:
        return _PR_OUTCOME_CACHE["data"]
    counts: dict = {"merged": 0, "active": 0, "abandoned": 0, "ok": True}
    try:
        ours = await c.execution_repo.work_item_ids()
        for repo in await c.ado.get_repositories():
            rid = repo.get("id")
            if not rid:
                continue
            for key, fetch in (
                ("merged", c.ado.get_completed_pull_requests),
                ("active", c.ado.get_active_pull_requests),
                ("abandoned", c.ado.get_abandoned_pull_requests),
            ):
                for pr in await fetch(rid):
                    if parse_work_item_id(pr.get("sourceRefName", "")) in ours:
                        counts[key] += 1
    except Exception as exc:  # noqa: BLE001 — metrics must never break the page
        _log.warning("pr outcome scan failed", error=str(exc))
        counts["ok"] = False
    decided = counts["merged"] + counts["abandoned"]
    counts["merge_rate"] = round(100 * counts["merged"] / decided) if decided else None
    # A failed scan is not cached: the next load should retry rather than serve zeros for
    # a full minute.
    if counts["ok"]:
        _PR_OUTCOME_CACHE.update(at=now, data=counts)
    return counts


def create_dashboard_router() -> APIRouter:
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])

    def _ctx(request: Request, active: str, **extra) -> dict:
        cfg = getattr(getattr(request.app.state, "container", None), "config", None)
        # The workspace selector lives in the sidebar, so every page needs the list and
        # the current choice. Resolved here rather than per-route so a page can never
        # render the shell with a selector that disagrees with its own data.
        all_workspaces = workspaces_mod.resolve(cfg) if cfg else []
        current = selected_workspace(request)
        if current != "all" and not any(w.id == current for w in all_workspaces):
            current = "all"   # a renamed or deleted workspace must not strand the view
        return {
            "request": request,
            "active": active,
            "workspaces": all_workspaces,
            "current_workspace": current,
            # Only worth showing a selector when there is something to choose between.
            "show_workspace_picker": len(all_workspaces) > 1,
            # Drives the sidebar's logout button: with no password there is no session to
            # end, so offering "Đăng xuất" would be a control that does nothing.
            "dashboard_locked": bool(
                cfg and (cfg.dashboard_auth_password_hash or cfg.dashboard_auth_token)
            ),
            "version": request.app.version,  # single source of truth: FastAPI(version=...)
            "mmss": _mmss,
            "fmt_duration": _fmt_duration,
            "category_badge": _category_badge,
            "status_class": _status_class,
            **extra,
        }

    # ── Login ────────────────────────────────────────────────────────────────
    # Reachable while the rest of /dashboard is locked (see `_security_guard`), so this
    # is the one place that must never assume an authenticated caller.

    def _safe_next(raw: str | None) -> str:
        """Only ever redirect back inside our own dashboard.

        `next` comes from the query string, so an attacker can put anything in it. A
        scheme-relative value like `//evil.example` is a valid *path* to a browser and
        would turn our login into an open redirect, which is why the check is a literal
        prefix test rather than "does it start with a slash".
        """
        target = raw or "/dashboard"
        if not target.startswith("/dashboard") or target.startswith("/dashboard//"):
            return "/dashboard"
        return target

    def _login_locked(request: Request) -> bool:
        cfg = request.app.state.container.config
        return bool(cfg.dashboard_auth_password_hash or cfg.dashboard_auth_token)

    @router.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        nxt = _safe_next(request.query_params.get("next"))
        if not _login_locked(request):
            return RedirectResponse(nxt, status_code=303)
        cfg = request.app.state.container.config
        if security.verify_session_token(
            request.cookies.get(security.SESSION_COOKIE), cfg
        ):
            return RedirectResponse(nxt, status_code=303)   # already in
        return _TEMPLATES.TemplateResponse(
            request, "login.html",
            {"request": request, "version": request.app.version, "next": nxt, "error": ""},
        )

    @router.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        cfg = request.app.state.container.config
        form = await request.form()
        nxt = _safe_next(str(form.get("next") or ""))
        password = str(form.get("password") or "")
        ok = (
            security.verify_password(password, cfg.dashboard_auth_password_hash)
            if cfg.dashboard_auth_password_hash
            else bool(cfg.dashboard_auth_token) and secrets.compare_digest(
                password, cfg.dashboard_auth_token
            )
        )
        if not ok:
            _log.warning("dashboard login failed", client=request.client.host
                         if request.client else "?")
            # 401, not 200: a failed login is not a successful page view, and the status
            # is what monitoring and fail2ban-style tooling actually key on.
            return _TEMPLATES.TemplateResponse(
                request, "login.html",
                {"request": request, "version": request.app.version, "next": nxt,
                 "error": "Mật khẩu không đúng."},
                status_code=401,
            )
        response = RedirectResponse(nxt, status_code=303)
        response.set_cookie(
            security.SESSION_COOKIE, security.make_session_token(cfg),
            max_age=security.SESSION_TTL_HOURS * 3600,
            httponly=True,          # not readable from JS
            samesite="lax",         # survives the redirect, blocks cross-site POSTs
            secure=request.url.scheme == "https",
            path="/dashboard",
        )
        with contextlib.suppress(Exception):
            await request.app.state.container.audit_repo.record(
                actor="dashboard", source="dashboard", action="dashboard.login",
                target=request.client.host if request.client else "?", detail="",
            )
        return response

    @router.post("/logout")
    async def logout(request: Request):
        response = RedirectResponse("/dashboard/login", status_code=303)
        response.delete_cookie(security.SESSION_COOKIE, path="/dashboard")
        return response

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        c: Container = request.app.state.container
        selected_tag = request.query_params.get("tag") or "all"
        tag_filter = None if selected_tag == "all" else selected_tag
        # Scoped to the selected workspace. Runs recorded before the project column
        # existed carry no project, so they show only in the unscoped view — better a
        # visibly missing row than another workspace's run counted here.
        _, in_scope = scope_of(request, c.config)
        stats = await c.execution_repo.get_stats(trigger_tag=tag_filter, projects=in_scope)
        recent = await c.execution_repo.get_recent(
            20, trigger_tag=tag_filter, projects=in_scope
        )
        efficiency = await c.execution_repo.get_efficiency(
            trigger_tag=tag_filter, projects=in_scope
        )
        prs = await _pr_outcomes(c)
        tokens_per_merged = (
            efficiency.total_tokens // prs["merged"] if prs["merged"] else None
        )
        return _TEMPLATES.TemplateResponse(
            request,
            "overview.html",
            _ctx(
                request,
                "overview",
                stats=stats,
                recent=recent,
                efficiency=efficiency,
                prs=prs,
                tokens_per_merged=tokens_per_merged,
                tags=c.config.effective_trigger_tags,
                selected_tag=selected_tag,
                # Outstanding spec drift belongs on the landing page: it is work owed to
                # a HUMAN, so it has to be visible without navigating to find it.
                spec_drift_open=await c.spec_drift_repo.open_count(),
            ),
        )

    _BOARD_CATS = {
        "BE": "[BE]", "FE": "[FE]", "DB": "[DB]", "QC": "[QC]", "TEST": "[TEST]",
    }

    def _filter_board_items(items: list, q: str, cat: str, dfrom: str, dto: str) -> list:
        """Apply the board's search / category / changed-date filters to work items."""
        out = items
        if q:
            ql = q.lower()
            out = [i for i in out if ql in str(i.id) or ql in (i.title or "").lower()]
        if cat and cat != "all":
            marker = _BOARD_CATS.get(cat.upper())
            if marker:
                out = [i for i in out if (i.title or "").upper().startswith(marker)]
        if dfrom or dto:
            def _in_range(i) -> bool:
                cd = getattr(i, "changed_date", None)
                if cd is None:
                    return False               # can't verify a date → exclude when filtering
                d = cd.date().isoformat()
                if dfrom and d < dfrom:
                    return False
                if dto and d > dto:
                    return False
                return True
            out = [i for i in out if _in_range(i)]
        return out

    async def _board_ctx(request: Request) -> dict:
        c: Container = request.app.state.container
        qp = request.query_params
        try:
            items = await c.ado.get_all_tagged_work_items()
            error = None
        except Exception as exc:  # noqa: BLE001
            items, error = [], str(exc)
        tags = c.config.effective_trigger_tags
        selected_tag = qp.get("tag") or "all"
        if selected_tag != "all":
            sel = selected_tag.lower()
            items = [i for i in items if any(t.lower() == sel for t in i.tags)]

        # The sidebar's workspace choice narrows the board first; the project dropdown
        # then narrows within it. Two levels because a workspace can hold several
        # projects — picking the workspace is "whose board is this", picking the
        # project is "which stream inside it".
        _, in_scope = scope_of(request, c.config)
        if in_scope is not None:
            allowed = {p.lower() for p in in_scope}
            items = [i for i in items if (i.project or "").lower() in allowed]
        projects = in_scope if in_scope is not None else c.config.effective_ado_projects
        selected_project = qp.get("project") or "all"
        if selected_project != "all":
            sel_proj = selected_project.lower()
            items = [i for i in items if (i.project or "").lower() == sel_proj]

        # Filters: search (id/title), category, changed-date range.
        q = (qp.get("q") or "").strip()
        cat = (qp.get("cat") or "all").strip()
        dfrom = (qp.get("from") or "").strip()
        dto = (qp.get("to") or "").strip()
        items = _filter_board_items(items, q, cat, dfrom, dto)

        records = await c.execution_repo.get_recent(200)
        states = {s.work_item_id: s.state.value for s in await c.state_repo.all()}
        cols = build_board(items, latest_records(records), c.config, states)

        # Per-column display cap + "load more".
        cap = max(0, getattr(c.config, "board_max_per_column", 20))
        try:
            limit = int(qp.get("limit") or cap)
        except ValueError:
            limit = cap
        limit = max(0, limit)

        base = {}
        if selected_tag != "all":
            base["tag"] = selected_tag
        if selected_project != "all":
            base["project"] = selected_project
        if q:
            base["q"] = q
        if cat and cat != "all":
            base["cat"] = cat
        if dfrom:
            base["from"] = dfrom
        if dto:
            base["to"] = dto
        filter_qs = urlencode(base)
        step = cap or 20
        more_qs = urlencode({**base, "limit": (limit or step) + step})

        org = c.config.ado_organization.rstrip("/")
        proj = c.config.ado_project
        ado_item_base = work_item_link_base(c.config)
        # Each card links into ITS OWN project — see BoardCard.url.
        if org:
            for column_cards in cols.values():
                for card in column_cards:
                    card_project = card.project or proj
                    if card_project:
                        card.url = (
                            f"{org}/{quote(card_project)}/_workitems/edit/{card.id}"
                        )
        return _ctx(
            request,
            "board",
            board=cols,
            columns=board_columns(c.config),
            total=len(items),
            error=error,
            project=c.config.ado_project,
            projects=projects,
            selected_project=selected_project,
            interactive=c.config.execution_mode == "interactive",
            tags=tags,
            selected_tag=selected_tag,
            ado_item_base=ado_item_base,
            board_limit=limit,
            filter_qs=filter_qs,
            more_url="/dashboard/board?" + more_qs,
            q=q, cat=cat, date_from=dfrom, date_to=dto,
        )

    @router.get("/board", response_class=HTMLResponse)
    async def board(request: Request):
        return _TEMPLATES.TemplateResponse(request, "board.html", await _board_ctx(request))

    @router.get("/reviews", response_class=HTMLResponse)
    async def reviews(request: Request):
        """PR reviewer tracking: every active PR with its reviewers, votes, and
        reminder status — live ADO data joined with the tracker's memory."""
        from ai_autopilot.services.reviewer_tracker import VOTE_LABELS

        c: Container = request.app.state.container
        cfg = c.config
        now = time.monotonic()
        if (
            _REVIEWS_CACHE["data"] is not None
            and now - _REVIEWS_CACHE["at"] < _REVIEWS_TTL
        ):
            grouped, summary = _REVIEWS_CACHE["data"]
            return _TEMPLATES.TemplateResponse(
                request,
                "reviews.html",
                _ctx(
                    request, "reviews", grouped=grouped, summary=summary,
                    targets=cfg.reviewer_target_branches,
                    tracking_enabled=cfg.pr_reviewer_tracking_enabled,
                    auto_review=cfg.pr_auto_review_on_added,
                    reminder_hours=cfg.pr_reviewer_reminder_hours,
                ),
            )
        org = cfg.ado_organization.rstrip("/")
        project = quote(cfg.code_project or cfg.ado_project, safe="")
        tracked: dict = {}
        try:
            for snap in await c.pr_reviewer_repo.all_reviewers():
                tracked[(snap.pr_id, snap.reviewer_id)] = snap
        except Exception as exc:  # noqa: BLE001 — page must render without the DB
            _log.warning("reviewer state load failed", error=str(exc))
        prs: list[dict] = []
        try:
            for repo in await c.ado.get_repositories():
                rid = repo.get("id")
                if not rid:
                    continue
                rname = repo.get("name") or ""
                for pr in await c.ado.get_active_pull_requests(rid):
                    target = (pr.get("targetRefName") or "").removeprefix("refs/heads/")
                    if not cfg.target_in_scope(pr.get("targetRefName", "")):
                        continue
                    pr_id = pr.get("pullRequestId")
                    reviewers = []
                    bot_reviewed = False
                    for r in pr.get("reviewers") or []:
                        if not r.get("id") or r.get("isContainer"):
                            continue
                        snap = tracked.get((pr_id, str(r["id"])))
                        vote = int(r.get("vote") or 0)
                        is_bot = bool(snap.is_bot) if snap else False
                        if is_bot and snap and snap.reviewed_commit:
                            bot_reviewed = True
                        reviewers.append({
                            "name": r.get("displayName") or r.get("uniqueName") or "?",
                            "vote": vote,
                            "vote_label": VOTE_LABELS.get(vote, str(vote)),
                            "is_bot": is_bot,
                            "required": bool(r.get("isRequired")),
                            "added_at": snap.added_at if snap else None,
                            "reminded": bool(snap.reminded_at) if snap else False,
                        })
                    approved = sum(1 for r in reviewers if r["vote"] >= 5)
                    blocked = sum(1 for r in reviewers if r["vote"] < 0)
                    pending = sum(1 for r in reviewers if r["vote"] == 0)
                    # mergeStatus: 3 = succeeded; 2 = conflicts; else queued/unknown.
                    ms = pr.get("mergeStatus")
                    conflicts = ms == "conflicts" or ms == 2
                    prs.append({
                        "id": pr_id,
                        "title": pr.get("title") or "",
                        "repo": rname,
                        "target": target,
                        "source": (pr.get("sourceRefName") or "").removeprefix("refs/heads/"),
                        "author": (pr.get("createdBy") or {}).get("displayName") or "",
                        "is_draft": bool(pr.get("isDraft")),
                        "created": pr.get("creationDate") or "",
                        "age": _pr_age(pr.get("creationDate")),
                        "conflicts": conflicts,
                        "work_item": parse_work_item_id(pr.get("sourceRefName", "")),
                        "url": f"{org}/{project}/_git/{quote(rname, safe='')}"
                               f"/pullrequest/{pr_id}",
                        "reviewers": reviewers,
                        "approved": approved,
                        "pending": pending,
                        "blocked": blocked,
                        "bot_reviewed": bot_reviewed,
                        "status": _pr_status(pr, approved, blocked, pending, conflicts),
                    })
        except Exception as exc:  # noqa: BLE001
            _log.warning("reviews page PR scan failed", error=str(exc))
        # Group by target (merge-into) branch — most PRs first, target name A→Z.
        groups: dict[str, list[dict]] = {}
        for pr in prs:
            groups.setdefault(pr["target"] or "(unknown)", []).append(pr)
        grouped = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        summary = {
            "total": len(prs),
            "approved": sum(1 for p in prs if p["status"] == "approved"),
            "awaiting": sum(1 for p in prs if p["status"] == "awaiting"),
            "blocked": sum(1 for p in prs if p["status"] in ("blocked", "conflicts")),
            "drafts": sum(1 for p in prs if p["is_draft"]),
        }
        _REVIEWS_CACHE.update(at=now, data=(grouped, summary))
        return _TEMPLATES.TemplateResponse(
            request,
            "reviews.html",
            _ctx(
                request, "reviews", grouped=grouped, summary=summary,
                targets=cfg.reviewer_target_branches,
                tracking_enabled=cfg.pr_reviewer_tracking_enabled,
                auto_review=cfg.pr_auto_review_on_added,
                reminder_hours=cfg.pr_reviewer_reminder_hours,
            ),
        )

    async def _planning_ctx(request: Request, **over) -> dict:
        """Common context for the Planning workbench page (+ its POST re-renders)."""
        c: Container = request.app.state.container
        cfg = c.config
        try:
            scheduled = await c.planned_run_repo.list_active()
        except Exception:  # noqa: BLE001
            scheduled = []
        try:
            sched_history = await c.scheduler_history_repo.recent(cfg.scheduler_history_limit or 20)
        except Exception:  # noqa: BLE001
            sched_history = []
        base = dict(
            view=getattr(c, "scheduler_view", None),
            enabled=cfg.dependency_scheduling_enabled,
            sibling=cfg.sibling_conflict_scheduling,
            poll_interval=cfg.poll_interval_seconds,
            ado_item_base=work_item_link_base(cfg),
            trigger_tags={t.lower() for t in cfg.effective_trigger_tags},
            default_assignee=cfg.auto_transition_assignee,
            states=sorted({*cfg.trigger_states, *cfg.done_states}),
            default_when=datetime.now().replace(  # noqa: DTZ005 — local wall-clock for the picker
                hour=max(0, min(23, cfg.planning_schedule_default_hour)),
                minute=0, second=0, microsecond=0,
            ).strftime("%Y-%m-%dT%H:%M"),
            live_refresh=cfg.planning_live_refresh_seconds,
            scheduled=scheduled,
            sched_history=sched_history,
            loaded=[], selected=set(), analysis=None,
            assignee=cfg.auto_transition_assignee, state_filter="all", type_filter="all",
            started=0, scheduled_n=0,
        )
        base.update(over)
        loaded = base.get("loaded") or []
        base["wtypes"] = sorted(
            {*_COMMON_WI_TYPES, *(i.work_item_type for i in loaded if i.work_item_type)}
        )
        return _ctx(request, "planning", **base)

    async def _scope_rows(request: Request, c: Container, rows: list) -> list:
        """Narrow DB rows keyed only by ``work_item_id`` to the selected workspace.

        The pipeline tables never recorded a project, so the mapping comes from the
        state history. An item the history has never seen has an UNKNOWN project, and
        unknown is excluded from a scoped view rather than shown everywhere: leaking
        another workspace's item into this one is the error that misleads, while a
        missing row is visible the moment the operator switches back to "all"."""
        _, in_scope = scope_of(request, c.config)
        if in_scope is None or not rows:
            return rows
        allowed = {p.lower() for p in in_scope}
        try:
            projects = await c.state_history.known_projects([r.work_item_id for r in rows])
        except Exception as exc:  # noqa: BLE001 — never blank a page over a filter
            _log.warning("workspace scoping unavailable", error=str(exc))
            return rows
        return [r for r in rows if projects.get(r.work_item_id, "").lower() in allowed]

    async def _planning_load(
        c: Container, assignee: str, state: str, wtype: str,
        in_scope: list[str] | None = None,
    ) -> list:
        """Load the work items to plan. A BLANK ``assignee`` is a real choice — the whole
        team's board — not a reason to show nothing (comma-separate names for a subset).

        ``in_scope`` is the selected workspace's projects (``None`` = every project).
        Filtering happens here rather than in the WIQL so the conflict analyser still
        sees one consistent set."""
        states = None if state in ("", "all") else [state]
        types = None if wtype in ("", "all") else [wtype]
        try:
            items = await c.ado.get_work_items_by_assignee(
                assignee, states, types, top=c.config.planning_load_limit
            )
        except Exception:  # noqa: BLE001
            return []
        if in_scope is None:
            return items
        allowed = {p.lower() for p in in_scope}
        return [i for i in items if (i.project or "").lower() in allowed]

    _FILTER_COOKIE = "planning_filter"

    def _restore_filter(request: Request, assignee: str, state: str, wtype: str,
                        default_assignee: str = "") -> tuple[str, str, str]:
        """Resolve the active filter: explicit query params win, then the cookie from the
        last visit, then this machine's own assignee as the opening default.

        An EMPTY assignee means "everyone", which is a choice a user can make — so it has
        to survive the cookie round-trip (``keep_blank_values``) and must not be silently
        replaced by the default, or clearing the box would snap back to one person.
        """
        if any(k in request.query_params for k in ("assignee", "state", "type")):
            return assignee, state, wtype
        parsed = parse_qs(request.cookies.get(_FILTER_COOKIE, ""), keep_blank_values=True)
        if not parsed:
            return default_assignee or assignee, state, wtype
        return (
            (parsed.get("assignee") or [assignee])[0],
            (parsed.get("state") or [state])[0],
            (parsed.get("type") or [wtype])[0],
        )

    @router.get("/planning", response_class=HTMLResponse)
    async def planning_page(
        request: Request, assignee: str = "", state: str = "all", type: str = "all",
        started: int = 0, scheduled: int = 0,
    ):
        c: Container = request.app.state.container
        assignee, state, type = _restore_filter(
            request, assignee, state, type, c.config.auto_transition_assignee
        )
        loaded = await _planning_load(c, assignee, state, type, scope_of(request, c.config)[1])
        ctx = await _planning_ctx(
            request, loaded=loaded, assignee=assignee, state_filter=state, type_filter=type,
            started=started, scheduled_n=scheduled,
        )
        resp = _TEMPLATES.TemplateResponse(request, "planning.html", ctx)
        # Remember the active filter for next time (30 days).
        resp.set_cookie(
            _FILTER_COOKIE,
            urlencode({"assignee": assignee, "state": state, "type": type}),
            max_age=60 * 60 * 24 * 30, samesite="lax", httponly=True,
        )
        return resp

    @router.get("/planning/live-partial", response_class=HTMLResponse)
    async def planning_live_partial(request: Request):
        """Just the Live-schedule card — polled by the page for auto-refresh."""
        c: Container = request.app.state.container
        cfg = c.config
        try:
            sched_history = await c.scheduler_history_repo.recent(
                cfg.scheduler_history_limit or 20
            )
        except Exception:  # noqa: BLE001
            sched_history = []
        return _TEMPLATES.TemplateResponse(
            request,
            "planning_live.html",
            {
                "view": getattr(c, "scheduler_view", None),
                "enabled": cfg.dependency_scheduling_enabled,
                "poll_interval": cfg.poll_interval_seconds,
                "sched_history": sched_history,
            },
        )

    @router.post("/planning/analyze", response_class=HTMLResponse)
    async def planning_analyze(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        ids = [int(x) for x in form.getlist("ids") if str(x).strip().isdigit()]
        # Blank = the whole team; do NOT fall back to this machine's assignee or the
        # analysis would silently re-scope to one person after every Analyze click.
        assignee = str(form.get("assignee", ""))
        state = str(form.get("state", "all"))
        wtype = str(form.get("type", "all"))
        analysis = await planning_analyzer.analyze(c, ids) if ids else None
        loaded = await _planning_load(c, assignee, state, wtype, scope_of(request, c.config)[1])
        ctx = await _planning_ctx(
            request, loaded=loaded, selected=set(ids), analysis=analysis,
            assignee=assignee, state_filter=state, type_filter=wtype,
        )
        return _TEMPLATES.TemplateResponse(request, "planning.html", ctx)

    def _planning_redirect(form, **extra) -> RedirectResponse:
        """Redirect back to the workbench, preserving the active filter (assignee /
        state / type carried in the POST body) so the loaded list survives the POST."""
        params = {
            "assignee": str(form.get("assignee", "")).strip(),
            "state": str(form.get("state", "all")).strip() or "all",
            "type": str(form.get("type", "all")).strip() or "all",
            **extra,
        }
        # Drop values equal to the GET-route defaults to keep the URL clean.
        query = urlencode(
            {k: v for k, v in params.items() if v not in ("", "all", 0)}
        )
        url = "/dashboard/planning" + (f"?{query}" if query else "")
        return RedirectResponse(url, status_code=303)

    @router.post("/planning/start")
    async def planning_start(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        ids = [int(x) for x in form.getlist("ids") if str(x).strip().isdigit()]
        mode = str(form.get("mode", "now")).strip()
        when_at = str(form.get("when_at", "")).strip()
        if not ids:
            return _planning_redirect(form)
        if mode == "schedule" and when_at:
            try:
                run_at = datetime.fromisoformat(when_at)
            except ValueError:
                run_at = None
            if run_at is not None:
                await c.planned_run_repo.create(ids, run_at, note=f"{len(ids)} item(s)")
                return _planning_redirect(form, scheduled=len(ids))
        n = await planning_analyzer.start_items(c, ids)
        return _planning_redirect(form, started=n)

    @router.post("/planning/cancel")
    async def planning_cancel(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        try:
            run_id = int(str(form.get("run_id", "")))
        except ValueError:
            run_id = 0
        if run_id:
            await c.planned_run_repo.set_status(run_id, "cancelled")
        return _planning_redirect(form)

    @router.get("/board/partial", response_class=HTMLResponse)
    async def board_partial(request: Request):
        """Just the columns — fetched by the page's auto-refresh, no full reload."""
        return _TEMPLATES.TemplateResponse(request, "_board_cols.html", await _board_ctx(request))

    @router.post("/board/move")
    async def board_move(request: Request):
        """Drag & drop: apply the configured tag/state for the target column."""
        c: Container = request.app.state.container
        form = await request.form()
        try:
            item_id = int(str(form.get("item_id", "")))
        except ValueError:
            return Response(status_code=204)
        column = str(form.get("column", "")).strip().lower()
        dmap = parse_drop_map(c.config.board_drop_map)
        action = dmap.get(column)
        if not item_id or action is None or c.config.dry_run:
            return Response(status_code=204)
        kind, value = action
        if kind == "state":
            await c.ado.update_state(item_id, value)
        else:  # tag — set exclusively among the configured drop-tags
            managed = {v for (k, v) in dmap.values() if k == "tag"}
            item = await c.ado.get_work_item(item_id)
            if item is not None:
                for tag in item.tags:
                    if tag in managed and tag != value:
                        await c.ado.remove_tag(item_id, tag)
            await c.ado.add_tag(item_id, value)
        _log.info("board move", id=item_id, column=column, action=action)
        return Response(status_code=204)

    @router.get("/activity/{item_id}", response_class=HTMLResponse)
    async def activity_view(request: Request, item_id: int):
        c: Container = request.app.state.container
        feed = activity.read(c.config.workspace_directory, item_id)
        return _TEMPLATES.TemplateResponse(
            request, "activity.html", _ctx(request, "board", item_id=item_id, feed=feed)
        )

    @router.get("/activity/{item_id}/partial", response_class=PlainTextResponse)
    async def activity_partial(request: Request, item_id: int):
        c: Container = request.app.state.container
        return PlainTextResponse(
            activity.read(c.config.workspace_directory, item_id) or "(no activity yet — waiting for the agent…)"
        )

    @router.get("/history", response_class=HTMLResponse)
    async def history(request: Request):
        c: Container = request.app.state.container
        qp = request.query_params
        status = (qp.get("status") or "").strip()
        cat = (qp.get("cat") or "").strip()
        q = (qp.get("q") or "").strip()
        dfrom = (qp.get("from") or "").strip()
        dto = (qp.get("to") or "").strip()
        page_size = 25
        try:
            page = max(1, int(qp.get("page") or 1))
        except ValueError:
            page = 1

        _, in_scope = scope_of(request, c.config)

        async def _run(pg: int):
            return await c.execution_repo.search(
                status=status or None, category=cat or None, q=q or None,
                dfrom=dfrom or None, dto=dto or None, projects=in_scope,
                offset=(pg - 1) * page_size, limit=page_size,
            )

        rows, total = await _run(page)
        pages = max(1, -(-total // page_size))          # ceil
        if page > pages:                                # out-of-range → clamp + re-query
            page = pages
            rows, total = await _run(page)

        base = {k: v for k, v in {
            "status": status, "cat": cat, "q": q, "from": dfrom, "to": dto,
        }.items() if v}

        def purl(pg: int) -> str:
            return "/dashboard/history?" + urlencode({**base, "page": pg})

        ctx = _ctx(
            request, "history",
            records=rows, total=total, page=page, pages=pages, page_size=page_size,
            status=status, cat=cat, q=q, date_from=dfrom, date_to=dto,
            start_idx=((page - 1) * page_size + 1) if total else 0,
            end_idx=min(page * page_size, total),
            prev_url=purl(page - 1) if page > 1 else "",
            next_url=purl(page + 1) if page < pages else "",
        )
        return _TEMPLATES.TemplateResponse(request, "history.html", ctx)

    @router.post("/history/{record_id}/delete")
    async def delete_history(request: Request, record_id: int):
        c: Container = request.app.state.container
        await c.execution_repo.delete(record_id)
        _log.info("execution record deleted via dashboard", record_id=record_id)
        return RedirectResponse(url="/dashboard/history", status_code=303)

    @router.post("/history/clear")
    async def clear_history(request: Request):
        c: Container = request.app.state.container
        removed = await c.execution_repo.clear_all()
        _log.info("execution history cleared via dashboard", removed=removed)
        return RedirectResponse(url="/dashboard/history", status_code=303)

    @router.get("/queue", response_class=HTMLResponse)
    async def queue_page(request: Request, resumed: int = 0):
        """Review queue: items the autopilot escalated (needs human) with a reason,
        so a human can Resume them (approve/redirect) in one place."""
        c: Container = request.app.state.container
        link_base = work_item_link_base(c.config)
        held = [s for s in await c.state_repo.all() if s.state == PipelineState.NEEDS_HUMAN]
        held.sort(key=lambda s: s.updated_at or datetime.min, reverse=True)
        held = await _scope_rows(request, c, held)
        items = [
            {
                "id": s.work_item_id, "title": s.title or f"#{s.work_item_id}",
                "detail": s.detail or "", "pr_url": s.pr_url or "",
                "url": f"{link_base}/{s.work_item_id}" if link_base else "",
                "updated": s.updated_at,
            }
            for s in held
        ]
        return _TEMPLATES.TemplateResponse(
            request, "queue.html", _ctx(request, "queue", items=items, resumed=resumed)
        )

    @router.post("/queue/resume")
    async def queue_resume(request: Request):
        """Approve/resume held items: clear the hold tag and hand them back to the
        poller (trigger tag + state), so work continues without a manual restart."""
        c: Container = request.app.state.container
        form = await request.form()
        ids = [int(x) for x in form.getlist("ids") if str(x).strip().isdigit()]
        if not ids:
            return RedirectResponse("/dashboard/queue", status_code=303)
        hold = c.config.escalation_tag
        for iid in ids:
            if hold:
                with contextlib.suppress(Exception):  # best-effort — tag may be absent
                    await c.ado.remove_tag(iid, hold)
        started = await planning_analyzer.start_items(c, ids)
        for iid in ids:  # leave the queue immediately; the poller will re-own the state
            await c.state_repo.set(iid, PipelineState.QUEUED)
        _log.info("resumed held items via queue", ids=ids, started=started)
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="item.resumed",
            target=", ".join(f"#{i}" for i in ids)[:300],
        )
        return RedirectResponse(f"/dashboard/queue?resumed={started}", status_code=303)

    @router.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request, action: str = "", limit: int = 100):
        """Append-only audit trail: who did what (config, tickets, resumes, reviews)."""
        c: Container = request.app.state.container
        events = await c.audit_repo.recent(limit=max(1, min(limit, 500)), action=action)
        actions = sorted({e.action.split(".")[0] + "." for e in events})
        return _TEMPLATES.TemplateResponse(
            request, "audit.html",
            _ctx(request, "audit", events=events, action=action, actions=actions),
        )

    @router.get("/quality", response_class=HTMLResponse)
    async def quality_page(request: Request, days: int = 30, kind: str = ""):
        """Rework & review quality: how often each item had to be redone, and what
        humans voted — read from the append-only log that outlives every budget."""
        c: Container = request.app.state.container
        days = max(1, min(days, 365))
        since = datetime.now() - timedelta(days=days)
        rows = await c.quality_events.rework_rows(since=since)
        totals = await c.quality_events.kind_totals(since=since)
        events = await c.quality_events.recent(limit=200, kind=kind, since=since)
        titles = {s.work_item_id: s.title for s in await c.state_repo.all()}
        return _TEMPLATES.TemplateResponse(
            request, "quality.html",
            _ctx(
                request, "quality", rows=rows, totals=totals, events=events,
                titles=titles, days=days, kind=kind,
                kinds=sorted(totals), rework_kinds=set(QualityKind.REWORK),
            ),
        )

    @router.get("/task/{work_item_id}", response_class=HTMLResponse)
    async def task_room(request: Request, work_item_id: int, tab: str = "overview"):
        """One task, one page: what it is, what the agent decided, what it changed.

        The diff is the expensive part (a fetch + a diff per repo), so it is gathered
        only when its tab is being shown — otherwise every glance at a task would pay
        for a network round trip nobody asked to see.
        """
        c: Container = request.app.state.container
        room = await TaskRoomService(c).gather(work_item_id, with_diff=(tab == "code"))
        org = (c.config.ado_organization or "").rstrip("/")
        return _TEMPLATES.TemplateResponse(
            request, "task_room.html",
            _ctx(
                request, "task", room=room, tab=tab,
                item_url=f"{org}/_workitems/edit/{work_item_id}" if org else "",
                drift_label=spec_drift.label_for, drift_icon=spec_drift.icon_for,
            ),
        )

    @router.get("/task/{work_item_id}/preview")
    async def task_preview(request: Request, work_item_id: int, path: str):
        """Serve one HTML artifact the run produced, for the preview pane.

        Two rails, because this turns a path in a query string into a file read:
        the resolved path must stay INSIDE the workspace (``..`` and absolute paths
        cannot escape it), and it must be a ``.html`` file — a repo is full of secrets,
        keys and source, and "render any file" would be an exfiltration endpoint
        wearing a feature's clothes. The page itself is then shown in a sandboxed
        iframe, so its scripts cannot reach the dashboard session around it.
        """
        c: Container = request.app.state.container
        # noqa on the path math below: these are local stats, not blocking reads, and
        # resolving BEFORE any read is the whole point of the containment check.
        root = Path(c.config.workspace_directory or "").resolve()  # noqa: ASYNC240
        try:
            target = (root / path).resolve()  # noqa: ASYNC240 — local path math
            target.relative_to(root)                     # raises if it escaped
        except (ValueError, OSError):
            return PlainTextResponse("Đường dẫn không hợp lệ.", status_code=400)
        if target.suffix.lower() != ".html" or not target.is_file():
            return PlainTextResponse("Chỉ xem được file .html trong workspace.", status_code=400)
        try:
            body = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return PlainTextResponse(f"Không đọc được file: {exc}", status_code=500)
        return HTMLResponse(
            body,
            headers={
                # Belt and braces with the iframe sandbox: even if the artifact is
                # hostile, it gets no network and no frame ancestors but ours.
                "Content-Security-Policy":
                    "default-src 'self' 'unsafe-inline' data:; frame-ancestors 'self'",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/specs", response_class=HTMLResponse)
    async def specs_page(request: Request):
        """Spec drift: what the agent decided that the work item never said.

        Grouped by work item, because that is the unit a BA edits — a flat list of
        deviations would have them opening the same item three times.
        """
        c: Container = request.app.state.container
        org = (c.config.ado_organization or "").rstrip("/")

        def _url(work_item_id: int) -> str:
            return f"{org}/_workitems/edit/{work_item_id}" if org else ""

        open_rows = await c.spec_drift_repo.open_drifts()
        resolved_rows = await c.spec_drift_repo.recent_resolved(limit=30)
        kinds = Counter(r.kind for r in open_rows)
        return _TEMPLATES.TemplateResponse(
            request, "specs.html",
            _ctx(
                request, "specs",
                open_items=_group_drifts(open_rows),
                resolved=_group_drifts(resolved_rows),
                open_count=len(open_rows),
                kind_totals=kinds.most_common(),
                label=spec_drift.label_for, icon=spec_drift.icon_for,
                work_item_url=_url if org else None,
            ),
        )

    @router.post("/specs/resolve")
    async def specs_resolve(request: Request, work_item_id: int = Form(...)):
        """A human says the specification is back in line: clear the tag, say so on the
        item, and tick the rows off."""
        c: Container = request.app.state.container
        # The dashboard authenticates with a shared password, not per-person accounts,
        # so "who" is the surface, not a name — same convention as the audit log.
        await SpecGuard(c).mark_resolved(work_item_id, by="dashboard")
        with contextlib.suppress(Exception):
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="spec_drift.resolved",
                target=str(work_item_id), detail="",
            )
        return RedirectResponse("/dashboard/specs", status_code=303)

    @router.get("/analytics", response_class=HTMLResponse)
    async def analytics_page(request: Request, days: int = 30, tag: str = ""):
        """Exec / ROI dashboard: throughput, success/PR rate, cost per merged PR."""
        from ai_autopilot.analytics import compute_analytics

        c: Container = request.app.state.container
        days = max(1, min(days, 180))
        now = datetime.now()
        dfrom = (now - timedelta(days=days - 1)).date().isoformat()
        records, _ = await c.execution_repo.search(
            dfrom=dfrom, trigger_tag=tag or None,
            projects=scope_of(request, c.config)[1], limit=5000,
        )
        report = compute_analytics(records, days=days, now=now)
        return _TEMPLATES.TemplateResponse(
            request, "analytics.html",
            _ctx(request, "analytics", report=report, days=days, tag=tag),
        )

    @router.get("/delivery", response_class=HTMLResponse)
    async def delivery_page(request: Request, days: int = 0, project: str = "all"):
        """The PM view: throughput, lead time, what is stuck, who is loaded.

        Every other page here reports on the AUTOPILOT (runs, tokens, success rate).
        This one reports on the PROJECT, counting work items rather than runs and
        putting age — the thing a board cannot show — in front.

        Gathering lives in ``services.delivery_report`` because the Teams digest reads
        the same report: two gatherers meant the chat message and this page could
        disagree, and a number that changes depending on where you read it is worse
        than no number."""
        c: Container = request.app.state.container
        cfg = c.config
        _, in_scope = scope_of(request, cfg)
        available = in_scope if in_scope is not None else cfg.effective_ado_projects
        # The page's own project dropdown narrows WITHIN the selected workspace, so an
        # unknown/stale value must not widen the scope back out to every project.
        if project != "all":
            wanted = [p for p in available if p.lower() == project.lower()]
            projects = wanted if wanted else []
        else:
            projects = in_scope
        report, error = await delivery_report.gather(c, days=days, projects=projects)
        return _TEMPLATES.TemplateResponse(
            request, "delivery.html",
            _ctx(
                request, "delivery", report=report, days=report.window_days, error=error,
                projects=available, selected_project=project,
                item_link=work_item_link_base(cfg),
                recording=cfg.delivery_history_enabled,
                bands=delivery.FLOW_BANDS,
                kind_labels=_ACTION_LABELS,
            ),
        )

    @router.get("/workspaces", response_class=HTMLResponse)
    async def workspaces_page(request: Request):
        """Manage the workspaces: which folder builds which ADO project(s), on which
        base branch.

        Replaces three places that had to agree (the global settings fields, a
        ``workspaces:`` YAML list and a one-line textarea) with one list. The first row
        is the default workspace — editable, not removable, because it is also the
        fallback for any project no other workspace claims."""
        c: Container = request.app.state.container
        flash = _take_flash(request)
        rejected = _take_ws_reject(request)
        views = rejected.get("views") or workspaces_mod.resolve(c.config)
        try:
            discovered = {
                ws.id: discover_repos(ws.directory) for ws in views if ws.directory
            }
        except Exception:  # noqa: BLE001 — the page must render on an unreadable disk
            discovered = {}
        response = _TEMPLATES.TemplateResponse(
            request, "workspaces.html",
            _ctx(
                request, "workspaces", flash=flash, views=views,
                errors=rejected.get("errors") or [], discovered=discovered,
                projects=c.config.effective_ado_projects,
            ),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        if rejected:
            response.delete_cookie(_WS_ERROR_COOKIE, path="/dashboard")
        return response

    @router.post("/workspaces")
    async def save_workspaces(request: Request):
        """Save the workspace list.

        Validation BLOCKS the save rather than warning, because every problem it
        catches is invisible at runtime: a project claimed by two workspaces builds in
        whichever one happens to win, and a workspace with no folder quietly runs its
        items in the default one — both look like the config was applied."""
        c: Container = request.app.state.container
        form = await request.form()
        views, errors = workspaces_mod.parse_form(form)
        if errors:
            _log.info("workspace config rejected", count=len(errors))
            return _ws_reject(errors, views)

        updates = workspaces_mod.to_settings_updates(views)
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()   # the polled project set just changed
        _log.info(
            "workspaces updated via dashboard",
            names=[v.label for v in views], projects=c.config.effective_ado_projects,
        )
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.workspaces_updated",
            target=", ".join(v.label for v in views)[:300],
        )
        return _flash("/dashboard/workspaces", "ws_saved")

    @router.post("/workspace/select")
    async def select_workspace(request: Request):
        """Point the dashboard at one workspace (or all of them).

        A POST because it writes a cookie, and a redirect back to where the operator
        was so the selector does not lose their place. It scopes the VIEW only — the
        autopilot keeps polling and running every workspace either way."""
        form = await request.form()
        chosen = str(form.get("workspace", "all") or "all").strip()
        back = str(form.get("back", "/dashboard") or "/dashboard")
        if not back.startswith("/dashboard"):
            back = "/dashboard"   # never bounce off-site on a value from the page
        response = RedirectResponse(back, status_code=303)
        response.set_cookie(
            _WS_COOKIE, chosen, max_age=60 * 60 * 24 * 365, httponly=True,
            samesite="lax", path="/dashboard",
        )
        return response

    @router.get("/learning", response_class=HTMLResponse)
    async def learning_page(request: Request, days: int = 30):
        """The retrospective learning loop, made visible: what the autopilot remembers
        per repo, which of it feeds the next brief, and how often it is being used.

        Without this page the loop is invisible — lessons live in files nobody opens, so
        a wrong lesson keeps poisoning every future brief with no way to notice.
        """
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        cfg = c.config
        workspace = cfg.workspace_directory
        days = max(1, min(days, 180))
        repos = lessons_mod.list_repos(workspace)
        groups = [(repo, lessons_mod.entries(workspace, repo)) for repo in repos]
        limit = max(0, cfg.lessons_max_injected)
        # Per repo: how many of ITS newest lessons a brief on that repo carries.
        injected_from = {repo: min(len(items), limit) for repo, items in groups}
        series = lessons_mod.per_day(workspace)[-days:]
        today = datetime.now().date().isoformat()
        try:
            dfrom = (datetime.now() - timedelta(days=days - 1)).date().isoformat()
            records, _ = await c.execution_repo.search(dfrom=dfrom, limit=5000)
        except Exception:  # noqa: BLE001 — the page must render without history
            records = []
        return _TEMPLATES.TemplateResponse(
            request, "learning.html",
            _ctx(
                request, "learning",
                enabled=cfg.learning_loop_enabled, workspace=workspace,
                repos=repos, groups=groups, injected_from=injected_from,
                total=sum(len(items) for _, items in groups),
                max_injected=limit, series=series,
                peak=max((n for _, n in series), default=0),
                new_today=sum(n for day, n in series if day == today),
                days=days,
                injected_total=sum(r.lessons_injected or 0 for r in records),
                injected_runs=sum(1 for r in records if r.lessons_injected),
            ),
        )

    @router.post("/learning/delete")
    async def learning_delete(request: Request):
        """Prune ONE lesson. A wrong lesson is worse than none — it is re-taught every run."""
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        form = await request.form()
        repo, text = str(form.get("repo") or ""), str(form.get("text") or "")
        if lessons_mod.delete(c.config.workspace_directory, repo, text):
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="lesson.deleted",
                target=f"{repo}: {text}"[:300],
            )
        return RedirectResponse("/dashboard/learning", status_code=303)

    @router.post("/learning/clear")
    async def learning_clear(request: Request):
        """Forget everything learned about one repo (e.g. after a rewrite)."""
        from ai_autopilot import lessons as lessons_mod

        c: Container = request.app.state.container
        form = await request.form()
        repo = str(form.get("repo") or "")
        if lessons_mod.clear(c.config.workspace_directory, repo):
            await c.audit_repo.record(
                actor="dashboard", source="dashboard", action="lesson.cleared", target=repo[:300],
            )
        return RedirectResponse("/dashboard/learning", status_code=303)

    @router.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        c: Container = request.app.state.container
        return _TEMPLATES.TemplateResponse(
            request, "config.html", _ctx(request, "config", cfg=c.config)
        )

    @router.get("/capabilities", response_class=HTMLResponse)
    async def capabilities(request: Request):
        c: Container = request.app.state.container
        skills = discover_skills(c.config.workspace_directory)
        return _TEMPLATES.TemplateResponse(
            request,
            "capabilities.html",
            _ctx(
                request,
                "capabilities",
                skills=skills,
                workspace=c.config.workspace_directory,
                ai_native=bool(c.config.workspace_directory),
            ),
        )

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        c: Container = request.app.state.container
        current = {
            f.key: getattr(c.config, f.key, "")
            for f in settings_form.FIELDS
            if f.key not in settings_form.SECRET_KEYS
        }
        has_pat = bool(getattr(c.config, "ado_pat", ""))
        secrets_set = {
            key: bool(getattr(c.config, key, ""))
            for key in settings_form.SECRET_KEYS
        }
        # dashboard_auth_password is entered raw but stored as a hash — reflect
        # "set" from the hash field, since there is no attr of the raw name.
        secrets_set["dashboard_auth_password"] = bool(
            getattr(c.config, "dashboard_auth_password_hash", "")
        )
        cfg = c.config
        # Read-only overview of every ADO tag the autopilot writes/reads — so the
        # whole tag vocabulary is visible in one place (not scattered across fields).
        tag_overview = [
            {"label": "Trigger", "cls": "chip-accent",
             "tags": cfg.effective_trigger_tags, "hint": "items with these get processed"},
            {"label": "Review", "cls": "chip-amber",
             "tags": [cfg.review_tag], "hint": "draft PR opened, awaiting review"},
            {"label": "Done", "cls": "chip-green",
             "tags": [cfg.processed_tag], "hint": "handled (also report/failed unless overridden)"},
            {"label": "Needs human", "cls": "chip-red",
             "tags": [cfg.escalation_tag], "hint": "escalated & held"},
            {"label": "Live", "cls": "chip-blue",
             "tags": [cfg.live_tag], "hint": "interactive session running"},
            {"label": "Failed", "cls": "chip",
             "tags": [cfg.failed_tag or f"{cfg.processed_tag} (Done tag)"],
             "hint": "gave up after retries"},
        ]
        discovered = discover_repos(c.config.workspace_directory)
        allowed = {r.lower() for r in c.config.allowed_repos}
        try:
            ado_states = await c.ado.get_states()
        except Exception:  # noqa: BLE001 — Settings must render even if ADO is down
            ado_states = []
        # Rows for the Teams channel card — the ONE editor for Teams notifications.
        channels = [
            {"name": str(e.get("name") or ""), "url": str(e.get("url") or ""),
             "active": bool(e.get("active", True))}
            for e in (cfg.teams_webhook_channels or []) if isinstance(e, dict) and e.get("url")
        ] + [
            {"name": "", "url": str(e), "active": True}
            for e in (cfg.teams_webhook_channels or []) if isinstance(e, str) and e.strip()
        ]
        if not channels:
            # Seed from the two legacy settings so an existing setup appears in the editor
            # rather than being invisible in it. `teams_webhook_url` becomes an ordinary row
            # named "primary": a row that behaved differently from its neighbours would be
            # the same inconsistency this card was meant to remove.
            legacy_urls = [
                (name, url) for name, url in
                [("primary", cfg.teams_webhook_url), *(("", u) for u in cfg.teams_webhook_urls)]
                if (url or "").strip()
            ]
            channels = [
                {"name": name, "url": url.strip(), "active": True}
                for name, url in legacy_urls
            ]

        flash = _take_flash(request)
        response = _TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            _ctx(
                request,
                "settings",
                sections=settings_form.sections(),
                current=current,
                has_pat=has_pat,
                secrets_set=secrets_set,
                restart_keys=settings_form.RESTART_REQUIRED,
                flash=flash,
                webhook_channels=channels,
                webhook_active_count=len(cfg.teams_webhook_targets),
                muted_channels=cfg.muted_teams_channels,
                config_path=str(config_file_path()),
                # Drives the full-export panel: without it the download is refused, so the
                # UI says why up front instead of after a click that produces nothing.
                has_export_password=bool(c.config.config_export_password),
                repos=discovered,
                allowed_repos=allowed,
                ado_states=ado_states,
                tag_overview=tag_overview,
            ),
        )
        if flash is not None:
            # One-shot: clear it now so a refresh shows the page without the banner.
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        return response

    @router.post("/settings")
    async def save_settings(request: Request):
        c: Container = request.app.state.container
        form = await request.form()
        updates = settings_form.parse_form(form)
        updates["allowed_repos"] = settings_form.parse_repos(form)
        updates["teams_webhook_channels"] = settings_form.parse_webhook_channels(form)
        # The card is seeded from both legacy settings, so once it has been saved they have
        # been absorbed — clearing them stops the same channel existing in two places.
        # Delivery would be unaffected either way (targets dedup by URL), but editing would
        # not: removing a row would not remove the channel.
        if updates["teams_webhook_channels"]:
            if c.config.teams_webhook_urls:
                updates["teams_webhook_urls"] = []
            if c.config.teams_webhook_url:
                updates["teams_webhook_url"] = ""

        # The dashboard password is entered raw but stored ONLY as a PBKDF2 hash.
        # Pop the raw value so it never reaches config.yaml or the live config.
        raw_password = updates.pop("dashboard_auth_password", None)
        if raw_password:
            updates["dashboard_auth_password_hash"] = security.hash_password(raw_password)

        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()  # re-read org URL if it changed
        _log.info("settings updated via dashboard", keys=sorted(updates.keys()))
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.updated",
            target=", ".join(sorted(updates.keys()))[:300],
        )

        return _flash("/dashboard/settings", "saved")

    # ── Flow editor: per-work-item-type state transitions ────────────────────

    async def _flow_context(request: Request, flows: list | None = None) -> dict:
        """Everything flow.html renders, built from the project's REAL types + states.

        ``flows`` overrides what is shown, so a rejected POST re-renders the values the
        operator just typed instead of throwing their work away.
        """
        c: Container = request.app.state.container
        cfg = c.config
        try:
            states_by_type = await c.ado.get_states_by_type()
        except Exception:  # noqa: BLE001 — the page must render with ADO down
            states_by_type = {}
        current = flows if flows is not None else list(cfg.work_item_flows or [])
        groups = [f for f in current if isinstance(f, dict)]
        # Which flow (if any) already claims each type, so a chip can say who holds it
        # rather than letting the operator create the ambiguity validation then rejects.
        claimed_by: dict[str, str] = {}
        for group in groups:
            for type_name in (group.get("types") or []):
                claimed_by.setdefault(str(type_name).strip().lower(),
                                      str(group.get("name") or ""))

        # Per rendered slot (existing groups + one blank), computed HERE rather than in the
        # template: Jinja's `{% set %}` doesn't survive a loop iteration, so intersecting
        # state lists in the markup silently produced the wrong answer.
        by_lower = {t.lower(): t for t in states_by_type}
        choices: list[list[str]] = []
        child_states: list[list[str]] = []
        rollup_rows: list[list[dict]] = []
        for group in [*groups, {}]:
            names = [str(t).strip() for t in (group.get("types") or [])]
            resolved = [by_lower[n.lower()] for n in names if n.lower() in by_lower]
            common: set[str] | None = None
            for type_name in resolved:
                states = set(states_by_type[type_name])
                common = states if common is None else (common & states)
            # Board order of the first ticked type, so the dropdown reads like the board.
            order = states_by_type.get(resolved[0], []) if resolved else []
            picked = common or set()
            choices.append([s for s in order if s in picked])
            # A parent's children are of OTHER types, so a roll-up line keys on their states.
            # Every project type would mean 17 rows here, 10 of them from Test Plan / Shared
            # Steps / Code Review — types that are never a child of anything the autopilot
            # runs. Since a roll-up is HELD until every listed state is mapped, showing those
            # implies they must be mapped, which is both noise and wrong. So the rows the
            # editor leads with are the states of types the autopilot actually manages (the
            # ones in some other flow group); the rest stay reachable behind a toggle.
            others = {t for f in groups for t in (f.get("types") or [])} - set(resolved)
            likely = sorted({
                s for t, st in states_by_type.items() if t in others for s in st
            })
            everything = sorted({
                s for t, st in states_by_type.items() if t not in resolved for s in st
            })
            if not likely:      # only one group configured — nothing to narrow to yet
                likely, everything = everything, []
            child_states.append(likely)

            mapped = dict(
                flows_mod.parse_rollup_entry(line) for line in (group.get("rollup") or [])
            )
            rows = [
                {"child": k, "parent": mapped.get(k, ""), "unknown": False, "secondary": False}
                for k in likely
            ]
            # A mapped state the project no longer has is kept and flagged rather than
            # silently dropped on the next save — that is how the "Ready for Testing" typo
            # survived unnoticed in the first place.
            rows += [
                {"child": k, "parent": v, "unknown": True, "secondary": False}
                for k, v in mapped.items() if k not in likely and k not in everything
            ]
            rows += [
                {"child": k, "parent": mapped.get(k, ""), "unknown": False, "secondary": True}
                for k in everything if k not in likely
            ]
            rollup_rows.append(rows)

        every_state = {s for st in states_by_type.values() for s in st}
        return {
            "types": sorted(states_by_type),
            "groups": groups,
            "claimed_by": claimed_by,
            "choices": choices,
            "child_states": child_states,
            "rollup_rows": rollup_rows,
            "stages": flows_mod.STAGES,
            "stage_groups": flows_mod.STAGE_GROUPS,
            "stage_labels": flows_mod.STAGE_LABELS,
            "uncovered": flows_mod.uncovered_types(groups, states_by_type),
            "enabled": cfg.auto_transition_enabled,
            "assignee": cfg.auto_transition_assignee,
            "legacy": {
                stage: getattr(cfg, legacy, "") for stage, _, legacy in flows_mod.STAGES
            },
            # Flat states that exist on NO type always fail — the same class of dead
            # config as the roll-up typo, so it gets called out instead of just listed.
            "legacy_bad": (legacy_bad := {
                stage: bool(states_by_type and getattr(cfg, legacy, "")
                            and getattr(cfg, legacy) not in every_state)
                for stage, _, legacy in flows_mod.STAGES
            }),
            # A plain flag rather than `legacy_bad.values()|select|list` in the template —
            # that filter chain does work, it is just harder to read than `any()`.
            "legacy_has_dead": any(legacy_bad.values()),
            "legacy_rollup": list(cfg.parent_rollup_map or []),
            "ado_down": not states_by_type,
        }

    @router.get("/flow", response_class=HTMLResponse)
    async def flow_page(request: Request):
        flash = _take_flash(request)
        rejected = _take_flow_reject(request)
        response = _TEMPLATES.TemplateResponse(
            request, "flow.html",
            _ctx(request, "flow", flash=flash, errors=rejected.get("errors") or [],
                 **await _flow_context(request, rejected.get("flows"))),
        )
        if flash is not None:
            response.delete_cookie(_FLASH_COOKIE, path="/dashboard")
        if rejected:
            response.delete_cookie(_FLOW_ERROR_COOKIE, path="/dashboard")
        return response

    @router.post("/flow")
    async def save_flow(request: Request):
        """Save the per-type flows — refusing anything ADO would reject.

        Validation blocks the save rather than warning, because that is exactly how the
        original bug survived: a state that existed on no work-item type sat in the
        config for months, failing silently on every item it touched.
        """
        c: Container = request.app.state.container
        form = await request.form()
        try:
            states_by_type = await c.ado.get_states_by_type()
        except Exception:  # noqa: BLE001
            states_by_type = {}
        parsed = flows_mod.parse_flow_form(form, sorted(states_by_type))
        errors = flows_mod.validate_flows(parsed, states_by_type)
        if errors:
            _log.info("flow config rejected", count=len(errors))
            return _flow_reject(errors, parsed)

        updates = {"work_item_flows": parsed}
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        _log.info("work-item flows updated via dashboard",
                  groups=[f.get("name") for f in parsed])
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.flows_updated",
            target=", ".join(str(f.get("name")) for f in parsed)[:300],
        )
        return _flash("/dashboard/flow", "flow_saved")

    @router.post("/settings/reload")
    async def reload_settings(request: Request):
        """Re-read config.yaml from disk into the running app (no restart)."""
        c: Container = request.app.state.container
        changed = settings_form.reload_from_file(c.config)
        c.ado.refresh()  # re-read org URL if it changed
        _log.info("config reloaded from file via dashboard", changed=changed)
        return _flash("/dashboard/settings", "reloaded")

    @router.get("/settings/export")
    async def export_config(request: Request):
        """Download the current config as YAML, minus secrets + machine-specific keys."""
        c: Container = request.app.state.container
        body = settings_form.export_yaml(c.config)
        _log.info("config exported via dashboard")
        return Response(
            content=body,
            media_type="application/x-yaml",
            headers={"Content-Disposition": 'attachment; filename="autopilot-config.yaml"'},
        )

    @router.get("/settings/export-full")
    async def export_config_full(request: Request):
        """Download the FULL config (INCLUDING secrets), encrypted with the
        configured full-export password. Decrypt with ai_autopilot.security."""
        c: Container = request.app.state.container
        if not c.config.config_export_password:
            # Refusing is the only safe answer. Encrypting under "" still produces a
            # valid-looking .enc file, but the key derives from an empty password that
            # anyone can reproduce — so the download would carry the ADO PAT and every
            # token with no real protection, while looking protected.
            _log.warning("full export refused — config_export_password is not set")
            return _flash("/dashboard/settings", "err_no_export_password")
        blob = settings_form.export_full_encrypted(c.config, c.config.config_export_password)
        # Audit only the event — never the secret payload or the password.
        _log.warning("FULL config (with secrets) exported via dashboard — encrypted download")
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.exported_full",
            detail="encrypted download incl. secrets",
        )
        return Response(
            content=blob,
            media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="autopilot-config-full.enc"'},
        )

    @router.post("/settings/import-full")
    async def import_config_full(request: Request):
        """Restore a FULL encrypted config (.enc from export-full), INCLUDING secrets.

        The password is entered on the form (a restore often lands on a fresh host
        whose own config_export_password differs from the source). Simple fields
        (incl. secrets) apply live and are persisted to config.yaml; nested structures
        (tenants, repos) are fully typed after a restart — same as the shareable
        import."""
        c: Container = request.app.state.container
        form = await request.form()
        upload = form.get("file")
        password = str(form.get("password", ""))
        if upload is None or not hasattr(upload, "read"):
            return _flash("/dashboard/settings", "err_no_file")
        if not password:
            return _flash("/dashboard/settings", "err_password")
        blob = await upload.read()
        try:
            updates = settings_form.import_full_settings(
                blob, password, set(type(c.config).model_fields)
            )
        except (ValueError, yaml.YAMLError) as exc:
            _log.warning("full config import failed", error=str(exc))
            return _flash("/dashboard/settings", "err_wrong_password")
        if not updates:
            return _flash("/dashboard/settings", "err_nothing")
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()
        _log.warning("FULL config restored via dashboard", keys=sorted(updates.keys()))
        await c.audit_repo.record(
            actor="dashboard", source="dashboard", action="config.imported_full",
            detail=f"{len(updates)} keys restored (incl. secrets)",
        )
        return _flash("/dashboard/settings", "imported_full")

    @router.post("/settings/import")
    async def import_config(request: Request):
        """Apply an uploaded YAML config (shared by a teammate). PAT is never imported."""
        c: Container = request.app.state.container
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return _flash("/dashboard/settings", "err_no_file")
        raw = (await upload.read()).decode("utf-8", errors="replace")
        try:
            updates = settings_form.import_settings(raw, set(type(c.config).model_fields))
        except (ValueError, yaml.YAMLError) as exc:
            _log.warning("config import failed", error=str(exc))
            return _flash("/dashboard/settings", "err_invalid")
        if not updates:
            return _flash("/dashboard/settings", "err_nothing")
        settings_form.save_to_yaml(config_file_path(), updates)
        settings_form.apply_to_config(c.config, updates)
        c.ado.refresh()
        _log.info("config imported via dashboard", keys=sorted(updates.keys()))
        return _flash("/dashboard/settings", "imported")

    return router


def files_changed_count(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return len(json.loads(raw))
    except (ValueError, TypeError):
        return 0
