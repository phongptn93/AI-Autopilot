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
    kind: str  # text | password | int | bool | select | list | stateset | stateone | map
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
    Field("repo_descriptions", "Repo descriptions", "list", "Workspace & Repository",
          "What each repo is, so the agent picks the right one. One 'RepoName = description' per "
          "line, e.g. 'Backend-Fresh = .NET API', 'Dxfac-gitops = deploy manifests, don't edit'."),
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
    Field("board_drop_map", "Drag & drop (column => tag/state)", "list", "Board columns",
          "Enable dragging cards: one 'Column => value' per line. Value is a tag, or an ADO state "
          "if prefixed with @. E.g. 'In review => autopilot-review', 'Ready to deploy => @Ready to Deploy'."),
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
    Field("pr_scoring_enabled", "Score each run (0–100)", "bool", "Execution & Autonomy",
          "Grade each run from objective signals; below the review threshold → hold for human."),
    Field("pr_score_auto_min", "Score ≥ this → auto-resolve", "int", "Execution & Autonomy",
          "Only at unattended autonomy. Default 85."),
    Field("pr_score_review_min", "Score < this → escalate", "int", "Execution & Autonomy",
          "Below this the run is held for a human instead of review/done. Default 60."),
    Field("dry_run", "Dry run", "bool", "Execution & Autonomy",
          "Log only — never execute or write to ADO."),
    # ── Dependency scheduling ──
    Field("dependency_scheduling_enabled", "Order by link graph", "bool",
          "Dependency scheduling",
          "Wait on Predecessor links, never run Related items together (0 tokens). "
          "Off = plain priority order."),
    Field("sibling_conflict_scheduling", "Sibling soft-conflict", "bool",
          "Dependency scheduling",
          "Treat same-Parent + same-category siblings as a soft conflict even without a link."),
    Field("scheduler_max_dispatch", "Max dispatch / cycle", "int", "Dependency scheduling",
          "Cap items marked ready per poll cycle. 0 = no cap (max_concurrent still throttles)."),
    Field("scheduler_use_ai_conflicts", "Use AI-found conflicts", "bool", "Dependency scheduling",
          "Feed hidden conflicts the Planning Analyze confirmed back into scheduling as "
          "soft-conflicts, so the poller won't run those items concurrently."),
    Field("scheduler_ai_conflict_min_score", "AI conflict min score", "int", "Dependency scheduling",
          "Only AI conflicts scoring at least this (0–100) affect scheduling. Default 60."),
    Field("scheduler_history_limit", "History to keep", "int", "Dependency scheduling",
          "How many recent scheduling decisions (that held work back) to keep for the "
          "Planning history panel. 0 = keep only the live view."),
    # ── Closed-loop SDLC engine (v2) ──
    Field("sdlc_loop_enabled", "Enable SDLC loop", "bool", "Closed-loop SDLC (v2)",
          "Drive items through profile-selected SDLC stages (gate + revise + escalate + handoff). "
          "Off = one-shot behaviour, unchanged. Headless only."),
    Field("sdlc_profile", "This machine's profile", "select", "Closed-loop SDLC (v2)",
          "Role this machine runs. Blank = fall through to type-map / default.",
          ("", "ba", "dev", "qc", "review", "design", "full")),
    Field("sdlc_default_profile", "Default profile", "select", "Closed-loop SDLC (v2)",
          "Used when neither a per-item sdlc:* tag nor this machine's profile resolves.",
          ("full", "dev", "ba", "qc", "review", "design")),
    Field("sdlc_max_iterations", "Max revise iterations", "int", "Closed-loop SDLC (v2)",
          "Shared budget across all stages of one item before escalating to a human. Default 3."),
    Field("sdlc_advance_on_draft", "Advance on draft PR", "bool", "Closed-loop SDLC (v2)",
          "Apply the handoff state even for a draft PR. Off = a draft awaits human review."),
    Field("sdlc_profile_states", "Handoff (profile => state)", "map",
          "Closed-loop SDLC (v2)",
          "One 'profile => ADO state' per line — set when that profile completes, so the next "
          "machine's trigger_states picks it up. E.g. 'ba => Ready for Dev'."),
    Field("sdlc_type_profiles", "Type → profile", "map",
          "Closed-loop SDLC (v2)",
          "Optional: map a work-item type to a profile, e.g. 'Bug => dev', 'User Story => full'."),
    # ── Planning workbench ──
    Field("planning_ai_analysis", "AI conflict analysis", "bool", "Planning workbench",
          "The Analyze action runs bounded Claude judges over keyword-overlapping pairs "
          "(tokens). Off = link-graph grouping only (0 tokens)."),
    Field("planning_ai_max_pairs", "AI max pairs / analyze", "int", "Planning workbench",
          "Cap on how many suspicious pairs get an AI judge per Analyze click. Default 6."),
    Field("planning_ai_min_score", "AI min score to flag", "int", "Planning workbench",
          "A judge verdict must score at least this (0–100) to be shown. Default 50."),
    Field("planning_ai_timeout_seconds", "AI judge timeout (s)", "int", "Planning workbench",
          "Per-judge Claude timeout during Analyze. Default 120."),
    Field("conflict_ai_min_token_len", "Keyword min length", "int", "Planning workbench",
          "Shortest keyword the pre-filter considers when pairing items. Default 4."),
    Field("conflict_ai_extra_stopwords", "Extra stopwords", "list", "Planning workbench",
          "Project-specific noise words to ignore when matching keywords (comma/newline)."),
    Field("planning_schedule_default_hour", "Schedule default hour", "int", "Planning workbench",
          "Hour (0–23, local) pre-filled in the Schedule date-time picker. Default 21."),
    Field("planning_load_limit", "Load limit", "int", "Planning workbench",
          "Max work items the Load button fetches for an assignee. Default 200."),
    Field("planning_live_refresh_seconds", "Live schedule refresh (s)", "int", "Planning workbench",
          "Auto-refresh the read-only Live schedule panel every N seconds. 0 = off."),
    Field("planning_start_state", "Start → state", "stateone", "Planning workbench",
          "State the Start action moves an item to (so the poller picks it up) if it isn't "
          "already in a trigger state. Blank = the first trigger state."),
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
        elif f.kind == "map":
            updates[f.key] = parse_map(form.get(f.key, ""))
        else:  # text, select
            updates[f.key] = str(form.get(f.key, "")).strip()
    return updates


def parse_map(raw: Any) -> dict[str, str]:
    """Parse a ``key => value`` textarea into a dict (one pair per line).

    Lines without ``=>`` or with a blank key are ignored; later duplicates win.
    """
    out: dict[str, str] = {}
    for line in str(raw).splitlines():
        if "=>" not in line:
            continue
        key, value = line.split("=>", 1)
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


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
