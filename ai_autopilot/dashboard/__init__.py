"""Server-rendered dashboard (replaces the Blazor dashboard).

Routes live in :mod:`ai_autopilot.dashboard.routes`, one module per area, each with
its own ``create_router()`` so an area can be mounted — and tested — on its own.
Shared helpers (templates, flash messages, formatting, caches) are in
:mod:`ai_autopilot.dashboard.common`.
"""

from __future__ import annotations

from fastapi import APIRouter

from ai_autopilot import activity
from ai_autopilot.dashboard.common import (
    _PR_OUTCOMES,
    _TEMPLATES,
    FLASH_MESSAGES,
    _feed_key,
    _filter_reviews,
    _fmt_ago,
    _fmt_duration,
    _model_label,
    _pr_outcomes,
    _scan_pr_outcomes,
    _ScanCache,
    _tokens_detail,
    forget_scans,
)
from ai_autopilot.dashboard.routes import (
    analytics,
    auth,
    board,
    fleet,
    flow,
    learning,
    loops,
    planning,
    reports,
    reviews,
    runs,
    security,
    settings,
    setup,
    task,
    workspaces,
)

__all__ = [
    "FLASH_MESSAGES",
    "_PR_OUTCOMES",
    "_TEMPLATES",
    "_ScanCache",
    "_feed_key",
    "_filter_reviews",
    "_fmt_ago",
    "_fmt_duration",
    "_model_label",
    "_pr_outcomes",
    "_scan_pr_outcomes",
    "_tokens_detail",
    "activity",
    "create_dashboard_router",
    "forget_scans",
]

# Registration order is the order routes were declared in before the split. No two
# areas share a path pattern today, but keeping it means a future overlap resolves
# the way it always did instead of by import order.
_AREAS = (
    auth, board, settings, loops, security, reports, reviews, planning, runs, task,
    analytics, fleet, workspaces, learning, flow, setup,
)


def create_dashboard_router() -> APIRouter:
    router = APIRouter()
    for area in _AREAS:
        router.include_router(area.create_router(), prefix="/dashboard")
    return router
