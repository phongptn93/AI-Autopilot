"""Gathers everything :func:`ai_autopilot.delivery.compute_delivery` needs.

The Delivery page and the Teams digest answer the same questions, so they read from
this ONE function. When they each did their own gathering the two disagreed — the chat
message said three PRs were waiting while the page said one — and a PM cannot act on a
number that changes depending on where they read it.

Everything here is best-effort: a page that renders with a missing section beats a page
that 500s, and a digest that goes out with fewer numbers beats a digest that never
arrives. Failures are logged, never raised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from ai_autopilot import delivery
from ai_autopilot.data.entities import ExecutionStatus, PipelineState
from ai_autopilot.logging_config import get_logger
from ai_autopilot.services.pr_feedback import parse_work_item_id

_log = get_logger("services.delivery_report")


def _parse_iso(value: str | None) -> datetime | None:
    """ADO timestamp → aware datetime, or None. Never raises: one malformed date must
    not blank out a whole report."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def collect_prs(container, project_of: dict[int, str]) -> list[delivery.PrView]:
    """Every active PR in scope, reduced to what the report needs.

    One pass over the repos, unlike the reviewer tracker's per-question helpers
    (``prs_ready_to_merge`` / ``new_prs_since`` / …) which each re-fetch every repo:
    this report asks four questions at once, and four full sweeps of the PR API is a
    page nobody waits for.

    ``approved_at`` comes from the tracker's stored votes, because the PR payload says
    only THAT a reviewer approved, never when — and "ready to merge for 3 days" is
    precisely a statement about when."""
    config = container.config
    approved_at: dict[int, datetime] = {}
    reviewer_repo = getattr(container, "pr_reviewer_repo", None)
    if reviewer_repo is not None:
        try:
            for row in await reviewer_repo.all_reviewers():
                if row.vote >= 5 and row.last_vote_at:
                    seen = approved_at.get(row.pr_id)
                    if seen is None or row.last_vote_at > seen:
                        approved_at[row.pr_id] = row.last_vote_at
        except Exception as exc:  # noqa: BLE001
            _log.warning("delivery: reviewer state load failed", error=str(exc))

    org = (config.ado_organization or "").rstrip("/")
    out: list[delivery.PrView] = []
    for repo in await container.ado.get_repositories():
        repo_id, repo_name = repo.get("id"), repo.get("name") or ""
        if not repo_id:
            continue
        for pr in await container.ado.get_active_pull_requests(repo_id):
            if pr.get("isDraft") or not config.target_in_scope(pr.get("targetRefName", "")):
                continue
            pr_id = pr.get("pullRequestId") or 0
            reviewers = [
                r for r in (pr.get("reviewers") or [])
                if r.get("id") and not r.get("isContainer")
            ]
            votes = [(r, int(r.get("vote") or 0)) for r in reviewers]
            wid = parse_work_item_id(pr.get("sourceRefName", "")) or 0
            item_project = project_of.get(wid, "")
            code_project = config.code_project_for(item_project)
            out.append(delivery.PrView(
                id=pr_id,
                repo=repo_name,
                title=pr.get("title") or "",
                author=(pr.get("createdBy") or {}).get("displayName") or "",
                url=(
                    f"{org}/{quote(code_project)}/_git/{quote(repo_name)}/pullrequest/{pr_id}"
                ) if org and code_project else "",
                work_item_id=wid,
                project=item_project,
                created_at=_parse_iso(pr.get("creationDate")),
                approved_at=approved_at.get(pr_id),
                approved=sum(1 for _, v in votes if v >= 5),
                blocked=sum(1 for _, v in votes if v < 0),
                pending=sum(1 for _, v in votes if v == 0),
                pending_reviewers=tuple(
                    (r.get("displayName") or "").strip()
                    for r, v in votes if v == 0 and r.get("displayName")
                ),
            ))
    return out


async def gather(
    container, *, days: int = 0, project: str = "all", now: datetime | None = None,
) -> tuple[delivery.DeliveryReport, str | None]:
    """Build the delivery report. Returns ``(report, error)``.

    ``error`` is set only when Azure DevOps itself could not be reached — the caller
    decides whether that is worth showing. Every other failure degrades one section.
    """
    config = container.config
    now = now or datetime.now(UTC)
    days = max(1, min(days or config.delivery_window_days, 180))
    error: str | None = None
    items: list = []
    categories: dict[str, str] = {}
    prs: list[delivery.PrView] = []

    try:
        items = await container.ado.get_all_active_work_items(top=config.delivery_max_items)
        categories = await container.ado.get_state_categories()
    except Exception as exc:  # noqa: BLE001
        error = str(exc)

    if project != "all":
        items = [i for i in items if (i.project or "").lower() == project.lower()]
    project_of = {i.id: i.project or "" for i in items}

    if error is None:
        try:
            prs = await collect_prs(container, project_of)
        except Exception as exc:  # noqa: BLE001
            _log.warning("delivery: PR collection failed", error=str(exc))
    if project != "all":
        # A PR with no linked work item cannot be attributed to a project; keep it only
        # on the unfiltered view rather than assigning it to one arbitrarily.
        prs = [p for p in prs if (p.project or "").lower() == project.lower()]

    changes: list = []
    history_since: datetime | None = None
    history = getattr(container, "state_history", None)
    if history is not None:
        try:
            # The window plus the window it is compared against.
            changes = await history.changes_since(now - timedelta(days=days * 2))
            history_since = await history.first_seen()
        except Exception as exc:  # noqa: BLE001
            _log.warning("delivery: state history load failed", error=str(exc))
    if project != "all":
        changes = [c for c in changes if (c.project or "").lower() == project.lower()]

    ai_item_ids: set[int] = set()
    execution_repo = getattr(container, "execution_repo", None)
    if execution_repo is not None:
        try:
            ai_item_ids = await execution_repo.work_item_ids()
        except Exception:  # noqa: BLE001
            ai_item_ids = set()

    held: list = []
    state_repo = getattr(container, "state_repo", None)
    if state_repo is not None:
        try:
            held = [
                s for s in await state_repo.all() if s.state == PipelineState.NEEDS_HUMAN
            ]
        except Exception:  # noqa: BLE001
            held = []

    failed: list = []
    if execution_repo is not None:
        try:
            cutoff = now - timedelta(days=days)
            failed = [
                r for r in await execution_repo.get_recent(400)
                if r.status == ExecutionStatus.FAILED
                and _as_utc(r.completed_at or r.started_at) >= cutoff
            ]
        except Exception:  # noqa: BLE001
            failed = []

    if project != "all":
        held = [h for h in held if project_of.get(h.work_item_id, "").lower() == project.lower()]
        failed = [
            f for f in failed
            if project_of.get(f.work_item_id, "").lower() == project.lower()
        ]

    report = delivery.compute_delivery(
        items=items, categories=categories, prs=prs, changes=changes,
        ai_item_ids=ai_item_ids, held=held, failed=failed,
        history_since=history_since, days=days, now=now,
        thresholds=delivery.Thresholds(
            merge_hours=config.delivery_merge_hours,
            review_hours=config.delivery_review_hours,
            stale_days=config.delivery_stale_days,
        ),
    )
    return report, error


async def delivered_since(container, since: datetime, *, now: datetime | None = None) -> int:
    """How many work items reached a done state since ``since``.

    Read from the recorded transitions rather than the PR API: it is one local query
    instead of a sweep of every repository, and it counts what actually finished rather
    than what merged (not the same thing when several PRs land for one item)."""
    history = getattr(container, "state_history", None)
    if history is None:
        return 0
    try:
        changes = await history.changes_since(since)
    except Exception as exc:  # noqa: BLE001
        _log.warning("delivery: delivered_since failed", error=str(exc))
        return 0
    return len(delivery.delivered_between(changes, since, now or datetime.now(UTC)))
