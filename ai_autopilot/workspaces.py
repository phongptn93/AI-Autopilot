"""Workspaces: the unit an operator actually thinks in.

A *workspace* is one folder holding a shared ``.claude`` and its source repos, bound to
the Azure DevOps project(s) whose work items are built there, with its own base branch.
One instance can run several; one workspace can serve several projects.

Why this module exists
----------------------
The configuration for a workspace was spread across three places that had to agree:
the global ``workspace_directory`` / ``base_branch`` / ``ado_project`` fields, a
``workspaces:`` list in YAML, and a one-line ``workspace_map`` textarea. Nothing named
the workspace, so the dashboard could not offer "show me this one", and an operator had
to hold the mapping in their head.

Here they collapse into a single ordered list. The FIRST entry is always the default
workspace, synthesised from the global fields — it is editable but not removable,
because it is also the fallback for any project no workspace claims. Everything after
it comes from ``Settings.workspaces`` (and, for backward compatibility, from any
``workspace_map`` lines still in the config).

Like ``flows.py`` this is a leaf module (no package imports) so the dashboard, the
poller and the executor can all read it without an import cycle. It is also pure:
resolution and validation take a config and return values, which is what lets the
whole thing be tested without a server.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# The default workspace's stable id. Not a user-chosen slug: the selector cookie stores
# an id, and a rename must not silently point it at nothing.
DEFAULT_ID = "default"
# What the default workspace is called when the operator has not named it.
DEFAULT_NAME = "Workspace mặc định"
# Bound on how many workspaces the form will accept, purely to stop a malformed POST
# from allocating without limit.
MAX_WORKSPACES = 50


@dataclass
class WorkspaceView:
    """One workspace, resolved and ready to display or scope a query by.

    ``is_default`` marks the entry backed by the global settings fields: it is edited
    in place rather than appended to ``Settings.workspaces``, and it cannot be deleted.
    """

    id: str
    name: str
    projects: list[str] = field(default_factory=list)
    directory: str = ""
    base_branch: str = ""
    code_project: str = ""
    repo_working_directory: str = ""
    allowed_repos: list[str] = field(default_factory=list)
    repo_descriptions: list[str] = field(default_factory=list)
    trigger_tag: str = ""
    enabled: bool = True
    is_default: bool = False

    @property
    def label(self) -> str:
        return self.name or (self.projects[0] if self.projects else self.id)

    def serves(self, project: str) -> bool:
        target = (project or "").strip().lower()
        return bool(target) and any(
            (p or "").strip().lower() == target for p in self.projects
        )

    def to_config(self) -> dict[str, Any]:
        """Shape stored in ``Settings.workspaces`` (a plain dict, so YAML round-trips
        and a live ``setattr`` applies it without a restart — same reason as flows)."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "ado_projects": list(self.projects),
            "code_project": self.code_project,
            "workspace_directory": self.directory,
            "repo_working_directory": self.repo_working_directory,
            "base_branch": self.base_branch,
            "allowed_repos": list(self.allowed_repos),
            "repo_descriptions": list(self.repo_descriptions),
            "trigger_tag": self.trigger_tag,
        }


def slugify(name: str, taken: set[str]) -> str:
    """A stable, url-safe id for a workspace name, unique within ``taken``.

    Vietnamese names are the norm here, so accents are folded rather than dropped —
    "Nhà máy 2" becoming "-2" would give two differently-named workspaces the same id.
    """
    folded = unicodedata.normalize("NFKD", name or "")
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    base = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-") or "workspace"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def resolve(config: Any) -> list[WorkspaceView]:
    """Every workspace, default first.

    The default entry carries the projects that belong to NO other workspace, so the
    list partitions the polled projects rather than double-counting them: a project
    claimed by a real workspace must not also appear under the default, or the
    selector would show its items twice.
    """
    configured: list[WorkspaceView] = []
    taken: set[str] = {DEFAULT_ID}
    for index, ws in enumerate(getattr(config, "workspaces", None) or []):
        view = _from_config(ws, index, taken)
        taken.add(view.id)
        configured.append(view)
    # Backward compatibility: one-line entries that predate this page. A structured
    # workspace claiming the same project wins (see Settings.effective_workspaces).
    claimed = {p.lower() for w in configured for p in w.projects}
    for ws in _legacy_lines(config):
        remaining = [p for p in ws.projects if p.lower() not in claimed]
        if not remaining:
            continue
        ws.projects = remaining
        ws.id = slugify(ws.label, taken)
        taken.add(ws.id)
        claimed.update(p.lower() for p in remaining)
        configured.append(ws)

    default = _default_view(config, claimed)
    return [default, *configured]


def _from_config(ws: Any, index: int, taken: set[str]) -> WorkspaceView:
    get = (lambda k, d=None: ws.get(k, d)) if isinstance(ws, Mapping) else (
        lambda k, d=None: getattr(ws, k, d)
    )
    name = str(get("name", "") or "")
    projects = [str(p).strip() for p in (get("ado_projects", None) or []) if str(p).strip()]
    view = WorkspaceView(
        id="",
        name=name or (projects[0] if projects else f"Workspace {index + 2}"),
        projects=projects,
        directory=str(get("workspace_directory", "") or ""),
        base_branch=str(get("base_branch", "") or ""),
        code_project=str(get("code_project", "") or ""),
        repo_working_directory=str(get("repo_working_directory", "") or ""),
        allowed_repos=[str(r) for r in (get("allowed_repos", None) or [])],
        repo_descriptions=[str(r) for r in (get("repo_descriptions", None) or [])],
        trigger_tag=str(get("trigger_tag", "") or ""),
        enabled=bool(get("enabled", True)),
    )
    view.id = slugify(view.label, taken)
    return view


def _legacy_lines(config: Any) -> list[WorkspaceView]:
    """``workspace_map`` one-liners, as views — so an existing config keeps working
    and its entries show up on the page ready to be saved in structured form."""
    from ai_autopilot.config import parse_workspace_map

    out: list[WorkspaceView] = []
    for ws in parse_workspace_map(getattr(config, "workspace_map", None) or []):
        out.append(WorkspaceView(
            id="", name=ws.name or (ws.ado_projects[0] if ws.ado_projects else ""),
            projects=list(ws.ado_projects), directory=ws.workspace_directory,
            base_branch=ws.base_branch, code_project=ws.code_project,
            repo_working_directory=ws.repo_working_directory,
            allowed_repos=list(ws.allowed_repos),
            repo_descriptions=list(ws.repo_descriptions),
            trigger_tag=ws.trigger_tag, enabled=ws.enabled,
        ))
    return out


def _default_view(config: Any, claimed: set[str]) -> WorkspaceView:
    """The workspace backed by the global settings fields."""
    projects = [
        p for p in (getattr(config, "effective_ado_projects", None) or [])
        if p.lower() not in claimed
    ]
    return WorkspaceView(
        id=DEFAULT_ID,
        name=getattr(config, "default_workspace_name", "") or DEFAULT_NAME,
        projects=projects,
        directory=getattr(config, "workspace_directory", "") or "",
        base_branch=getattr(config, "base_branch", "") or "",
        code_project=getattr(config, "code_project", "") or "",
        repo_working_directory=getattr(config, "repo_working_directory", "") or "",
        allowed_repos=list(getattr(config, "allowed_repos", None) or []),
        repo_descriptions=list(getattr(config, "repo_descriptions", None) or []),
        trigger_tag=getattr(config, "trigger_tag", "") or "",
        enabled=True,
        is_default=True,
    )


def find(config: Any, workspace_id: str) -> WorkspaceView | None:
    """The workspace with this id, or ``None`` — including for ``"all"``/blank, which
    the callers treat as "no filter" rather than as a missing workspace."""
    wanted = (workspace_id or "").strip().lower()
    if not wanted or wanted == "all":
        return None
    for ws in resolve(config):
        if ws.id == wanted:
            return ws
    return None


def scope_projects(config: Any, workspace_id: str) -> list[str] | None:
    """The ADO projects a view scoped to ``workspace_id`` should show, or ``None`` for
    "everything".

    ``None`` and ``[]`` mean opposite things and the difference matters: no selection
    shows all projects, while a workspace that names no project shows none — silently
    widening the latter to "all" would tell an operator their empty workspace is busy.
    """
    ws = find(config, workspace_id)
    return None if ws is None else list(ws.projects)


# ── form parsing ─────────────────────────────────────────────────────────────


def _split_lines(raw: Any) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]", str(raw or "")) if x.strip()]


def parse_form(form: Mapping[str, Any]) -> tuple[list[WorkspaceView], list[str]]:
    """Parse the Workspaces page into ``(views, errors)``.

    Rows are numbered by the form (``ws{i}_*``) with ``ws_count`` bounding ``i``; a row
    whose delete box is ticked is dropped, except the default, which is edited in place
    and has no delete box to tick.

    Validation is deliberately about the mistakes that are INVISIBLE at runtime — a
    project claimed by two workspaces (whichever wins is arbitrary), or a workspace
    with a folder but no project (nothing will ever route to it). A row that is merely
    incomplete is kept and reported, never silently dropped.
    """
    try:
        count = min(int(str(form.get("ws_count", "0"))), MAX_WORKSPACES)
    except ValueError:
        count = 0
    views: list[WorkspaceView] = []
    errors: list[str] = []
    taken: set[str] = {DEFAULT_ID}
    seen_projects: dict[str, str] = {}

    for index in range(max(0, count)):
        prefix = f"ws{index}_"
        is_default = str(form.get(f"{prefix}is_default", "")) == "1"
        if not is_default and form.get(f"{prefix}delete"):
            continue
        name = str(form.get(f"{prefix}name", "") or "").strip()
        projects = _split_lines(form.get(f"{prefix}projects", ""))
        directory = str(form.get(f"{prefix}directory", "") or "").strip()
        if not (name or projects or directory) and not is_default:
            continue  # an untouched blank row is not an error
        view = WorkspaceView(
            id=DEFAULT_ID if is_default else "",
            name=name or (DEFAULT_NAME if is_default else ""),
            projects=projects,
            directory=directory,
            base_branch=str(form.get(f"{prefix}base_branch", "") or "").strip(),
            code_project=str(form.get(f"{prefix}code_project", "") or "").strip(),
            repo_working_directory=str(form.get(f"{prefix}repo_dir", "") or "").strip(),
            allowed_repos=_split_lines(form.get(f"{prefix}repos", "")),
            repo_descriptions=[
                line.strip()
                for line in str(form.get(f"{prefix}repo_descriptions", "") or "").splitlines()
                if line.strip()
            ],
            trigger_tag=str(form.get(f"{prefix}trigger_tag", "") or "").strip(),
            enabled=is_default or bool(form.get(f"{prefix}enabled")),
            is_default=is_default,
        )
        if not view.is_default:
            view.id = slugify(view.label, taken)
            taken.add(view.id)
        label = view.label or f"dòng {index + 1}"
        if not view.name:
            errors.append(f"«{label}»: chưa đặt tên workspace.")
        if not view.projects and not view.is_default:
            errors.append(
                f"«{label}»: chưa gán ADO project nào — sẽ không có work item nào chạy "
                "trong workspace này."
            )
        if view.projects and not view.directory and not view.is_default:
            errors.append(
                f"«{label}»: có project nhưng chưa có thư mục — item của nó sẽ chạy "
                "nhầm trong workspace mặc định."
            )
        for project in view.projects:
            key = project.lower()
            if key in seen_projects:
                errors.append(
                    f"Project «{project}» bị gán cho cả «{seen_projects[key]}» và "
                    f"«{label}» — mỗi project chỉ thuộc một workspace."
                )
            else:
                seen_projects[key] = label
        views.append(view)

    if not any(v.is_default for v in views):
        errors.append("Thiếu workspace mặc định — không thể lưu.")
    return views, errors


def to_settings_updates(views: list[WorkspaceView]) -> dict[str, Any]:
    """Turn parsed rows into a ``Settings`` update dict.

    The default row writes back to the GLOBAL fields it came from; every other row goes
    into ``workspaces``. ``workspace_map`` is emptied on save: its lines have just been
    migrated into the structured list, and leaving them would resurrect stale routes
    the operator thinks they deleted.
    """
    default = next((v for v in views if v.is_default), None)
    others = [v for v in views if not v.is_default]
    updates: dict[str, Any] = {
        "workspaces": [v.to_config() for v in others],
        "workspace_map": [],
    }
    if default is not None:
        # The default's FIRST project stays ado_project (where new items are created);
        # the rest become ado_projects.
        projects = list(default.projects)
        updates.update({
            "default_workspace_name": default.name,
            "ado_project": projects[0] if projects else "",
            "ado_projects": projects[1:],
            "workspace_directory": default.directory,
            "base_branch": default.base_branch,
            "code_project": default.code_project,
            "repo_working_directory": default.repo_working_directory,
            "allowed_repos": list(default.allowed_repos),
            "repo_descriptions": list(default.repo_descriptions),
            "trigger_tag": default.trigger_tag,
        })
    return updates
