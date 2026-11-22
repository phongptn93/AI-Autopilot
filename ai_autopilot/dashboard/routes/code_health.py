"""Code health page — hotspots, knowledge concentration, debt markers per repo.

The repo is chosen BY NAME from the git repos found in the configured workspaces,
never by a path in the query string: this page runs git in whatever directory it is
handed, so a typed path would let anyone who can open the dashboard point it at any
folder on the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ai_autopilot import code_health
from ai_autopilot import workspaces as workspaces_mod
from ai_autopilot.container import Container
from ai_autopilot.dashboard.common import _TEMPLATES
from ai_autopilot.dashboard.routes._shared import _ctx
from ai_autopilot.workspace import discover_repos

WINDOWS = (30, 90, 180, 365)


@dataclass(frozen=True)
class RepoChoice:
    key: str     # what the query string carries
    label: str
    path: str


def repo_choices(cfg) -> list[RepoChoice]:
    """Every git repo this machine is configured to work in, each once."""
    roots: list[tuple[str, str]] = []   # (workspace label, directory)
    with contextlib.suppress(Exception):
        for ws in workspaces_mod.resolve(cfg):
            if getattr(ws, "directory", ""):
                roots.append((getattr(ws, "name", "") or ws.id, ws.directory))
    if getattr(cfg, "workspace_directory", ""):
        roots.append(("", cfg.workspace_directory))

    found: dict[str, tuple[str, str]] = {}    # resolved path → (name, workspace)
    for ws_label, root in roots:
        for name in discover_repos(root):
            full = str(Path(root) / name)
            found.setdefault(str(Path(full).resolve()), (name, ws_label))
        # A workspace that is itself one repo (single-repo setup).
        if (Path(root) / ".git").exists():
            found.setdefault(str(Path(root).resolve()), (Path(root).name, ws_label))
    single = getattr(cfg, "repo_working_directory", "") or ""
    if single and (Path(single) / ".git").exists():
        found.setdefault(str(Path(single).resolve()), (Path(single).name, ""))

    names = [n for n, _ in found.values()]
    out: list[RepoChoice] = []
    for path, (name, ws_label) in found.items():
        clash = names.count(name) > 1
        key = f"{ws_label}/{name}" if clash and ws_label else name
        label = f"{name} ({ws_label})" if clash and ws_label else name
        out.append(RepoChoice(key, label, path))
    out.sort(key=lambda r: r.label.lower())
    return out


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/code-health", response_class=HTMLResponse)
    async def code_health_page(request: Request, repo: str = "", days: int = 90):
        c: Container = request.app.state.container
        choices = repo_choices(c.config)
        chosen = next((r for r in choices if r.key == repo), None)
        if chosen is None and choices and not repo:
            chosen = choices[0]
        days = days if days in WINDOWS else 90
        report = None
        if chosen is not None:
            # git can take seconds on a big repo; keep the event loop free.
            report = await asyncio.to_thread(code_health.cached_analyze, chosen.path, days)
        return _TEMPLATES.TemplateResponse(
            request, "code_health.html",
            _ctx(request, "code_health", choices=choices, chosen=chosen,
                 unknown_repo=bool(repo) and chosen is None, requested=repo,
                 days=days, windows=WINDOWS, report=report,
                 share_limit=int(code_health.CONCENTRATION_SHARE * 100),
                 away_days=code_health.AWAY_DAYS,
                 min_commits=code_health.CONCENTRATION_MIN_COMMITS),
        )

    return router
