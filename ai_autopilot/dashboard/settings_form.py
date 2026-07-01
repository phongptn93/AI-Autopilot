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
    kind: str  # text | password | int | bool | select | list | stateset | stateone
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
    Field("ado_project", "Project (work items)", "text", "Azure DevOps Connection"),
    Field("code_project", "Code project (repos/PRs)", "text", "Azure DevOps Connection",
          "Project where the git repos, PRs and build pipelines live, if different from the "
          "work-item project. Blank = same. (Cross-project setup.)"),
    Field("ado_pat", "Personal Access Token", "password", "Azure DevOps Connection",
          "Leave blank to keep the current token."),
    # ── Tags & trigger ──
    Field("trigger_tag", "Trigger tag", "text", "Tags & Trigger",
          "Work items with this tag get processed."),
    Field("trigger_tags", "Additional trigger tags", "list", "Tags & Trigger",
          "Extra tags to also process (comma or newline separated). Board/Overview can filter by tag."),
    Field("trigger_states", "Trigger states", "stateset", "Tags & Trigger",
          "ADO states eligible for processing — tick from your board, or add custom ones below."),
    Field("reprocess_on_reopen", "Reprocess when reopened", "bool", "Tags & Trigger",
          "When a handled item is dragged back to a trigger state, clear its autopilot "
          "tags so it runs again. (Only trigger states the autopilot doesn't set itself.)"),
    Field("poll_interval_seconds", "Poll interval (seconds)", "int", "Tags & Trigger"),
    # ── Outcomes → tag + state ──
    # The policy table: for each outcome, the ADO tag to add and the ADO state to
    # set. Blank = skip. This is the single source of truth for tagging + state.
    Field("state_in_progress", "⏳ In progress — ADO state", "stateone", "Outcomes → tag + state",
          "State when the autopilot starts working an item (no tag)."),
    Field("review_tag", "🔍 Review — tag", "text", "Outcomes → tag + state",
          "Tag added when a draft PR opens (awaiting review); item is held."),
    Field("state_in_review", "🔍 Review — ADO state", "stateone", "Outcomes → tag + state",
          "State when a draft PR opens (awaiting human review)."),
    Field("processed_tag", "✅ Done — tag", "text", "Outcomes → tag + state",
          "Tag added when an item is handled (also used for report / failed unless overridden)."),
    Field("resolved_state", "✅ Done — ADO state", "stateone", "Outcomes → tag + state",
          "State when an item is resolved with a PR (Resolved / Closed / Done)."),
    Field("state_report", "📝 Report — ADO state", "stateone", "Outcomes → tag + state",
          "State when a plan is commented in report mode (tag = Done tag)."),
    Field("escalation_tag", "🙋 Needs human — tag", "text", "Outcomes → tag + state",
          "Tag added when the agent escalates; held items are skipped."),
    Field("state_needs_human", "🙋 Needs human — ADO state", "stateone", "Outcomes → tag + state",
          "State when the agent escalates and holds the item for a human."),
    Field("failed_tag", "⛔ Failed — tag", "text", "Outcomes → tag + state",
          "Tag added when the autopilot gives up after retries. Blank = use the Done tag."),
    Field("state_failed", "⛔ Failed — ADO state", "stateone", "Outcomes → tag + state",
          "State when the autopilot gives up after exhausting retries."),
    # ── Board columns (extra, read-only by ADO state) ──
    Field("board_review_state", "Column: Ready for review", "stateone", "Board columns",
          "Items in this ADO state show in a 'Ready for review' board column. Blank = no column. "
          "Use a state the autopilot doesn't set."),
    Field("board_deploy_state", "Column: Ready to deploy", "stateone", "Board columns",
          "Items in this ADO state show in a 'Ready to deploy' board column. Blank = no column. "
          "Use a state the autopilot doesn't set."),
    Field("done_states", "Done states (→ Done column)", "stateset", "Board columns",
          "ADO states that count as Done on the board (e.g. Ready to Testing, Closed). "
          "Items a human moved to any of these show in the Done column."),
    # ── Auto transitions ──
    Field("auto_transition_enabled", "Enable auto transitions", "bool", "Auto transitions",
          "Move the work item when its PR is merged, and roll a parent forward when all its "
          "children are done."),
    Field("auto_transition_assignee", "Only for assignee (auto transitions)", "text", "Auto transitions",
          "Restrict auto transitions to work items assigned to this person (name/email substring). "
          "Blank = any assignee. Does not affect normal task processing."),
    Field("on_merge_state", "On PR merged → state", "stateone", "Auto transitions",
          "State to set when a PR the autopilot opened is merged (also marks it done). Blank = "
          "only tag done, don't change state."),
    Field("parent_rollup_map", "Parent roll-up (child = parent)", "list", "Auto transitions",
          "One 'Child state = Parent state' per line, in progression order, e.g. "
          "'Ready for Testing = Impl Done'. The parent follows its least-advanced child; when all "
          "children reach a child-state, the parent moves to the mapped parent-state."),
    Field("on_deploy_state", "On deploy success → state", "stateone", "Auto transitions",
          "When a deploy pipeline build succeeds, move items sitting in 'On PR merged → state' to "
          "this state. Blank = deploy monitor off."),
    Field("deploy_pipeline_id", "Deploy pipeline id", "int", "Auto transitions",
          "ADO build definition id of the deploy pipeline. 0 = watch any successful build on the branch."),
    Field("deploy_branch", "Deploy branch", "text", "Auto transitions",
          "Branch the deploy builds run on (blank = base branch)."),
    # ── Execution & autonomy ──
    Field("execution_mode", "Execution mode", "select", "Execution & Autonomy",
          "interactive = launch a Remote-Control Claude session per task you can /rc into and steer; "
          "headless = autonomous SDK run (no human attach).",
          ("interactive", "headless")),
    Field("autonomy_level", "Autonomy level", "select", "Execution & Autonomy",
          "report = comment only, assisted = draft PR, unattended = auto PR.",
          ("report", "assisted", "unattended")),
    Field("use_worktrees", "Isolate tasks (git worktree)", "bool", "Execution & Autonomy",
          "Run each task in its own git worktree so concurrent tasks never touch your main "
          "checkout. Turn off to run in-place in the shared workspace."),
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

# Keys excluded from an exported/shared config: secrets + machine-specific values.
# Sharing these would leak credentials or pin a teammate to this host's paths/tag.
EXPORT_EXCLUDE = frozenset(
    {"ado_pat", "oauth_app_id", "oauth_app_secret", "workspace_directory", "trigger_tag"}
)


def export_settings(config: Any) -> dict[str, Any]:
    """Shareable settings dict: the editable fields minus secrets and machine-
    specific values (PAT, OAuth app, workspace path, the per-host trigger tag)."""
    return {
        f.key: getattr(config, f.key, None)
        for f in FIELDS
        if f.key not in EXPORT_EXCLUDE
    }


def export_yaml(config: Any) -> str:
    """Serialise :func:`export_settings` to a YAML document for download."""
    return yaml.safe_dump(export_settings(config), sort_keys=False, allow_unicode=True)


def import_settings(raw: str, valid_keys: set[str]) -> dict[str, Any]:
    """Parse an uploaded YAML config into an updates dict.

    Keeps only known Settings keys, and defensively drops secrets / machine-
    specific keys (``EXPORT_EXCLUDE``) even if the file contains them.
    """
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError("Config file must be a YAML mapping")
    return {
        k: v for k, v in data.items() if k in valid_keys and k not in EXPORT_EXCLUDE
    }


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
        elif f.kind == "stateset":
            updates[f.key] = parse_states(form, f.key)
        else:  # text, select
            updates[f.key] = str(form.get(f.key, "")).strip()
    return updates


def parse_states(form: Mapping[str, Any], key: str) -> list[str]:
    """Collect a state set: ticked ``<key>__<state>`` checkboxes plus any custom
    states typed into the ``<key>__manual`` textarea, de-duplicated in order.

    The full candidate set is carried in ``_all_states__<key>`` (comma-joined), so
    a state is selected when its checkbox is present in the form.
    """
    candidates = [s for s in str(form.get(f"_all_states__{key}", "")).split(",") if s]
    picked = [s for s in candidates if f"{key}__{s}" in form]
    manual = [x.strip() for x in re.split(r"[,\n]", str(form.get(f"{key}__manual", ""))) if x.strip()]
    out: list[str] = []
    for s in [*picked, *manual]:
        if s not in out:
            out.append(s)
    return out


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
