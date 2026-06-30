"""Editable-settings form: field specs, form parsing, and persistence.

Drives the ``/dashboard/settings`` page. Pure helpers (parsing/coercion/merge)
are kept here so they can be unit-tested without a running server.
"""

from __future__ import annotations

import contextlib
import re
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
    Field("workspace_directory", "Workspace directory", "text", "Workspace & Repository",
          "Folder holding the shared .claude (skills/rules/MCP). Claude runs HERE and the agent "
          "picks which repo subfolder to edit. Blank = legacy mode (run inside one repo)."),
    Field("base_branch", "Base branch", "text", "Workspace & Repository",
          "Branch new feature branches are cut from."),
    # ── Azure DevOps connection ──
    Field("ado_organization", "Organization URL", "text", "Azure DevOps Connection",
          "e.g. https://dev.azure.com/your-org"),
    Field("ado_project", "Project", "text", "Azure DevOps Connection"),
    Field("ado_pat", "Personal Access Token", "password", "Azure DevOps Connection",
          "Leave blank to keep the current token."),
    # ── Tags & trigger ──
    Field("trigger_tag", "Trigger tag", "text", "Tags & Trigger",
          "Work items with this tag get processed."),
    Field("trigger_states", "Trigger states", "list", "Tags & Trigger",
          "ADO states eligible for processing (comma or newline separated) — match your board."),
    Field("processed_tag", "Processed tag", "text", "Tags & Trigger",
          "Added after a work item is handled (Done)."),
    Field("review_tag", "Review tag", "text", "Tags & Trigger",
          "Marks items whose draft PR awaits review (In review)."),
    Field("escalation_tag", "Escalation tag", "text", "Tags & Trigger",
          "Added when the agent needs a human (Needs human); held items are skipped."),
    Field("resolved_state", "Resolved state", "text", "Tags & Trigger",
          "ADO state set when an item is resolved — match your board (Resolved / Closed / Done)."),
    Field("poll_interval_seconds", "Poll interval (seconds)", "int", "Tags & Trigger"),
    # ── Execution & autonomy ──
    Field("execution_mode", "Execution mode", "select", "Execution & Autonomy",
          "interactive = launch a Remote-Control Claude session per task you can /rc into and steer; "
          "headless = autonomous SDK run (no human attach).",
          ("interactive", "headless")),
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
        elif f.kind == "list":
            raw = str(form.get(f.key, ""))
            updates[f.key] = [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]
        else:  # text, select
            updates[f.key] = str(form.get(f.key, "")).strip()
    return updates


def parse_repos(form: Mapping[str, Any]) -> list[str]:
    """Collect the ticked repo whitelist from ``repo__<name>`` checkboxes.

    The form carries the full discovered set in ``_all_repos`` (comma-joined); a
    repo is allowed when its checkbox is present. None ticked → ``[]`` (= all).
    """
    all_repos = [r for r in str(form.get("_all_repos", "")).split(",") if r]
    return [r for r in all_repos if f"repo__{r}" in form]


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


def reload_from_file(config: Any) -> list[str]:
    """Re-read config.yaml + env into the live Settings object, in place.

    Lets a direct edit of ``config.yaml`` (including fields not on the form, such
    as ``trigger_states`` and ``repos``) take effect without restarting the app.
    Returns the sorted list of keys whose value changed. Code changes (e.g. the
    skill router) still require a restart — only configuration is reloaded.
    """
    from ai_autopilot.config import load_settings

    fresh = load_settings()
    changed: list[str] = []
    for key in type(config).model_fields:
        new_value = getattr(fresh, key)
        if getattr(config, key) != new_value:
            changed.append(key)
        setattr(config, key, new_value)
    return sorted(changed)
