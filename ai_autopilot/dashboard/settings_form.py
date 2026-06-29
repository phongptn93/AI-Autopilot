"""Editable-settings form: field specs, form parsing, and persistence.

Drives the ``/dashboard/settings`` page. Pure helpers (parsing/coercion/merge)
are kept here so they can be unit-tested without a running server.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # text | password | int | bool | select
    section: str
    help: str = ""
    options: tuple[str, ...] = field(default_factory=tuple)


# Order here is the order rendered on the page. Sections group consecutive fields.
FIELDS: tuple[Field, ...] = (
    # ── Workspace & repository ──
    Field("repo_working_directory", "Workspace directory", "text", "Workspace & Repository",
          "Absolute path to the git repo Claude works in."),
    Field("base_branch", "Base branch", "text", "Workspace & Repository",
          "Branch new feature branches are cut from."),
    Field("use_worktrees", "Use git worktrees", "bool", "Workspace & Repository",
          "Isolate each run in its own worktree (safe parallelism)."),
    Field("worktrees_dir", "Worktrees directory", "text", "Workspace & Repository",
          "Where worktrees are created. Blank = system temp."),
    # ── Azure DevOps connection ──
    Field("ado_organization", "Organization URL", "text", "Azure DevOps Connection",
          "e.g. https://dev.azure.com/your-org"),
    Field("ado_project", "Project", "text", "Azure DevOps Connection"),
    Field("ado_pat", "Personal Access Token", "password", "Azure DevOps Connection",
          "Leave blank to keep the current token."),
    # ── Tags & trigger ──
    Field("trigger_tag", "Trigger tag", "text", "Tags & Trigger",
          "Work items with this tag get processed."),
    Field("processed_tag", "Processed tag", "text", "Tags & Trigger",
          "Added after a work item is handled."),
    Field("review_tag", "Review tag", "text", "Tags & Trigger",
          "Marks items whose draft PR awaits review."),
    Field("poll_interval_seconds", "Poll interval (seconds)", "int", "Tags & Trigger"),
    # ── Execution & autonomy ──
    Field("autonomy_level", "Autonomy level", "select", "Execution & Autonomy",
          "report = comment only, assisted = draft PR, unattended = auto PR.",
          ("report", "assisted", "unattended")),
    Field("max_concurrent", "Max concurrent", "int", "Execution & Autonomy",
          "Restart required to take effect."),
    Field("task_timeout_minutes", "Task timeout (minutes)", "int", "Execution & Autonomy"),
    Field("auto_review_enabled", "Auto security review", "bool", "Execution & Autonomy"),
    Field("dry_run", "Dry run", "bool", "Execution & Autonomy",
          "Log only — never execute or write to ADO."),
)

# Fields that only take effect after a restart (the value is captured at startup).
RESTART_REQUIRED = frozenset({"max_concurrent"})

# Never echo these values back into the form.
SECRET_KEYS = frozenset({"ado_pat"})


def sections() -> list[tuple[str, list[Field]]]:
    """Return fields grouped by section, preserving declaration order."""
    grouped: dict[str, list[Field]] = {}
    for f in FIELDS:
        grouped.setdefault(f.section, []).append(f)
    return list(grouped.items())


def parse_form(form: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce a submitted form into a typed updates dict.

    - checkboxes: present → True, absent → False
    - password: blank → omitted (keeps the existing secret)
    - int: blank/invalid → omitted
    """
    updates: dict[str, Any] = {}
    for f in FIELDS:
        if f.kind == "bool":
            updates[f.key] = f.key in form
        elif f.kind == "password":
            value = str(form.get(f.key, "")).strip()
            if value:
                updates[f.key] = value
        elif f.kind == "int":
            value = str(form.get(f.key, "")).strip()
            if value:
                with contextlib.suppress(ValueError):
                    updates[f.key] = int(value)
        else:  # text, select
            updates[f.key] = str(form.get(f.key, "")).strip()
    return updates


def save_to_yaml(path: Path, updates: Mapping[str, Any]) -> None:
    """Merge ``updates`` into the YAML config file (creating it if needed)."""
    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    data.update(updates)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def apply_to_config(config: Any, updates: Mapping[str, Any]) -> None:
    """Apply updates to the live Settings object (so running services see them)."""
    for key, value in updates.items():
        setattr(config, key, value)
