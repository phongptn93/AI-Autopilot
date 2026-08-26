"""``ai-autopilot doctor`` — a self-audit of whether the configuration is COHERENT.

Distinct from ``health.py``, which asks whether the running system can reach ADO, Claude
and the disk. Those checks pass happily on a configuration that can never work: a Teams
bot switched on with no app id, a messaging endpoint bound to loopback, ``max_concurrent``
raised without worktrees, a version that drifted across the three files that declare it.

Every check here came from a real failure that cost time to diagnose by hand — so this is
deliberately offline, deterministic and fast: no network, no writes, safe to run anywhere.
It reports what is wrong and what to do about it, then names the few things worth doing
first, because a wall of findings is its own kind of useless.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ai_autopilot import flows as flows_mod
from ai_autopilot.config import Settings, describe_users, is_ambiguous_user, parse_hhmm
from ai_autopilot.scheduling import resolve_tz

ERROR, WARN, OK = "error", "warn", "ok"
ERROR_ICON, WARN_ICON, OK_ICON = "❌", "⚠️ ", "✅"
_LEVEL_ICON = {ERROR: ERROR_ICON, WARN: WARN_ICON, OK: OK_ICON}
_EFFORTS = {"", "low", "medium", "high", "xhigh", "max"}
# Teams caps a bot's commandList at 10; exceeding it makes Teams reject the whole package.
_TEAMS_COMMAND_LIMIT = 10


@dataclass(frozen=True)
class Finding:
    level: str
    title: str
    detail: str = ""
    fix: str = ""


def _repo_root() -> Path | None:
    """The source checkout this package lives in, or None when installed as a wheel.

    Some checks compare files that only exist in a checkout (pyproject, the Teams app
    manifest); they must simply be skipped for an installed copy rather than fail."""
    root = Path(__file__).resolve().parent.parent
    return root if (root / "pyproject.toml").is_file() else None


# ── individual checks ────────────────────────────────────────────────────────


def check_ado(config: Settings) -> list[Finding]:
    missing = [
        name for name, value in (
            ("ado_organization", config.ado_organization),
            ("ado_project", config.ado_project),
            ("ado_pat", config.ado_pat),
        ) if not value
    ]
    if missing:
        return [Finding(
            ERROR, "Azure DevOps connection incomplete",
            "Missing: " + ", ".join(missing),
            "Fill these in on /dashboard/settings → Azure DevOps Connection. "
            "Nothing will be polled until all three are set.",
        )]
    return [Finding(OK, "Azure DevOps connection configured")]


def check_trigger(config: Settings) -> list[Finding]:
    if not config.effective_trigger_tags and not config.trigger_states:
        return [Finding(
            ERROR, "Nothing can ever be picked up",
            "Both the trigger tag(s) and trigger states are empty, so no work item "
            "matches the poller's candidate rule.",
            "Set a trigger tag (Tags & Trigger) or tick at least one trigger state.",
        )]
    return [Finding(OK, "Trigger configured")]


def check_workspace(config: Settings) -> list[Finding]:
    ws = (config.workspace_directory or "").strip()
    if not ws:
        return [Finding(
            WARN, "No workspace directory",
            "Running in legacy single-repo mode: the agent cannot pick between repos and "
            "the shared .claude (skills/rules/MCP) is not loaded.",
            "Set workspace_directory to the folder holding .claude and your repo subfolders.",
        )]
    root = Path(ws)
    if not root.is_dir():
        return [Finding(
            ERROR, "Workspace directory does not exist", ws,
            "Fix workspace_directory — every run uses it as its working directory.",
        )]
    out: list[Finding] = []
    if not (root / ".claude").is_dir():
        out.append(Finding(
            WARN, "No .claude in the workspace", str(root / ".claude"),
            "Skills, rules and MCP servers live here; without it /skill commands are inert.",
        ))
    absent = [r for r in config.allowed_repos if not (root / r).is_dir()]
    if absent:
        out.append(Finding(
            ERROR, "allowed_repos not found in the workspace", ", ".join(absent),
            "Clone them under the workspace, or remove them from allowed_repos — a run "
            "targeting a missing repo fails after doing the rest of the work.",
        ))
    return out or [Finding(OK, "Workspace looks complete")]


def check_workspaces(config: Settings) -> list[Finding]:
    """Validate the extra project → workspace routes (multi-workspace setups).

    Silent when nothing extra is configured. A mis-typed line here is invisible at
    runtime — the work items simply run in the ROOT workspace and edit the wrong
    repos — so every failure mode is reported rather than defaulted away."""
    # Reported by the workspace's display NAME, because that is what the operator sees
    # on the page and in the selector — telling them "workspace 2" would send them
    # hunting through YAML for a row this tool already identified.
    from ai_autopilot import workspaces as workspaces_mod

    named = {
        tuple(sorted(p.lower() for p in view.projects)): view.name
        for view in workspaces_mod.resolve(config)
    }

    def _name(ws) -> str:
        return named.get(tuple(sorted(p.lower() for p in ws.ado_projects)), ws.label)

    workspaces = config.effective_workspaces
    if not workspaces:
        # A line that failed to parse leaves no workspace behind, so a non-empty map
        # producing nothing is the loudest possible signal that the syntax is wrong.
        if config.workspace_map:
            return [Finding(
                ERROR, "No workspace route could be parsed",
                f"{len(config.workspace_map)} line(s) in workspace_map, none usable.",
                "Rebuild them on /dashboard/workspaces — saving there replaces these "
                "lines with structured entries and validates them as you go.",
            )]
        return []
    out: list[Finding] = []
    known = {p.lower() for p in config.effective_ado_projects}
    for ws in workspaces:
        directory = (ws.workspace_directory or "").strip()
        if not directory:
            out.append(Finding(
                ERROR, f"Workspace '{_name(ws)}' has no directory",
                f"projects: {', '.join(ws.ado_projects)}",
                "Give it a folder, or drop the line — its work items currently run in the "
                "root workspace, editing another project's repos.",
            ))
        elif not Path(directory).is_dir():
            out.append(Finding(
                ERROR, f"Workspace '{_name(ws)}' directory does not exist", directory,
                "Every run for these projects uses it as its working directory.",
            ))
        elif not (Path(directory) / ".claude").is_dir():
            out.append(Finding(
                WARN, f"No .claude in workspace '{_name(ws)}'",
                str(Path(directory) / ".claude"),
                "Skills, rules and MCP servers live here; without it /skill commands are inert.",
            ))
        unknown = [p for p in ws.ado_projects if p.lower() not in known]
        if unknown:  # defensive — effective_ado_projects folds these in, so this is a bug net
            out.append(Finding(
                WARN, f"Workspace '{_name(ws)}' names unpolled project(s)",
                ", ".join(unknown), "Add them under Azure DevOps Connection → More projects.",
            ))
    if not out:
        out.append(Finding(
            OK, f"{len(workspaces)} extra workspace(s) routed",
            "; ".join(f"{_name(w)} → {w.workspace_directory}" for w in workspaces),
        ))
    return out


def check_projects(config: Settings) -> list[Finding]:
    """Every polled work-item project should be reachable and unambiguous."""
    projects = config.effective_ado_projects
    if len(projects) <= 1:
        return []
    routed = {
        p.lower() for ws in config.effective_workspaces for p in ws.ado_projects
    }
    unrouted = [p for p in projects if p.lower() not in routed]
    findings = [Finding(
        OK, f"{len(projects)} work-item projects polled", ", ".join(projects),
    )]
    if unrouted and config.effective_workspaces:
        findings.append(Finding(
            WARN, "Projects with no workspace of their own", ", ".join(unrouted),
            "They run in the default workspace. That is fine when they share it — "
            "otherwise give each one its own entry on /dashboard/workspaces.",
        ))
    return findings


def check_delivery(config: Settings) -> list[Finding]:
    """The Delivery page's clock — recording is the part that cannot be caught up later."""
    if not config.delivery_history_enabled:
        return [Finding(
            WARN, "Delivery history recording is off",
            "No work-item state transitions are being recorded.",
            "Lead time, cycle time and the flow chart stay empty — and the period spent "
            "switched off can never be reconstructed, because ADO's ChangedDate is bumped "
            "by any edit. Enable it under Settings → Delivery.",
        )]
    if config.delivery_history_interval_minutes > 60:
        return [Finding(
            WARN, "Delivery history sampled coarsely",
            f"Checking every {config.delivery_history_interval_minutes} minutes.",
            "A state the item passes through and leaves between two checks is never "
            "recorded, so short stages vanish from the flow chart. 5–15 minutes is "
            "cheap: two API calls per check.",
        )]
    return [Finding(OK, "Delivery history recording on")]


def check_concurrency(config: Settings) -> list[Finding]:
    if config.max_concurrent > 1 and not config.use_worktrees:
        return [Finding(
            ERROR, "Concurrent tasks share one checkout",
            f"max_concurrent={config.max_concurrent} with use_worktrees=false.",
            "Enable 'Isolate tasks (git worktree)', or set max_concurrent back to 1. "
            "Parallel runs in one checkout corrupt each other's branches.",
        )]
    return [Finding(OK, "Concurrency and isolation agree")]


def check_effort(config: Settings) -> list[Finding]:
    bad = {
        name: value for name, value in (
            ("claude_effort_chat", config.claude_effort_chat),
            ("claude_effort_agentic", config.claude_effort_agentic),
            ("claude_effort_task", config.claude_effort_task),
        ) if value not in _EFFORTS
    }
    if bad:
        return [Finding(
            WARN, "Unknown reasoning-effort value",
            ", ".join(f"{k}={v!r}" for k, v in bad.items()),
            "Use one of low / medium / high / xhigh / max, or leave blank for the model "
            "default. An unknown value is ignored at runtime, so the call silently runs "
            "at the default instead of what you intended.",
        )]
    return [Finding(OK, "Reasoning effort valid")]


def check_autonomy(config: Settings) -> list[Finding]:
    if config.autonomy_level != "unattended":
        return [Finding(OK, f"Autonomy level: {config.autonomy_level}")]
    missing = []
    if not config.test_gate_enabled:
        missing.append("no auto-test-gate")
    if not config.policy_protected_paths:
        missing.append("no protected paths")
    if not config.auto_review_enabled:
        missing.append("no auto security review")
    if missing:
        return [Finding(
            WARN, "Unattended autonomy with few guardrails", "; ".join(missing),
            "At this level nobody reviews before the branch moves. Turn on the test gate "
            "and set protected paths so a bad run cannot touch what matters.",
        )]
    return [Finding(OK, "Unattended autonomy has guardrails")]


def check_dashboard_security(config: Settings) -> list[Finding]:
    exposed = config.health_host not in ("127.0.0.1", "localhost", "::1")
    has_auth = bool(config.dashboard_auth_password_hash or config.dashboard_auth_token)
    out: list[Finding] = []
    if exposed and not has_auth:
        out.append(Finding(
            ERROR, "Dashboard reachable from the network with no password",
            f"health_host={config.health_host}",
            "Anyone who can reach it can read and rewrite the config, including the ADO "
            "PAT. Set a dashboard password, or bind health_host to 127.0.0.1.",
        ))
    if exposed and not config.webhook_secret:
        out.append(Finding(
            WARN, "Webhook endpoint exposed with no shared secret", "",
            "Set webhook_secret so /api/webhook/* rejects unauthenticated calls.",
        ))
    return out or [Finding(OK, "Web exposure looks sane")]


def check_notifications(config: Settings) -> list[Finding]:
    targets = config.teams_webhook_targets
    muted = config.muted_teams_channels
    out: list[Finding] = []

    # A row with a name but no URL notifies nothing while looking configured — the same
    # class of dead config as a state that exists on no work-item type.
    nameless = [
        str(e.get("name") or "(unnamed)")
        for e in (config.teams_webhook_channels or [])
        if isinstance(e, dict) and not str(e.get("url") or "").strip()
    ]
    if nameless:
        out.append(Finding(
            WARN, f"{len(nameless)} Teams channel(s) have no URL",
            ", ".join(nameless),
            "Paste the Workflows URL, or remove the row — as it stands the channel is "
            "listed but nothing is ever posted to it.",
        ))
    broken = [
        str(e.get("name") or "(unnamed)")
        for e in (config.teams_webhook_channels or [])
        if isinstance(e, dict) and (url := str(e.get("url") or "").strip())
        and not url.lower().startswith("https://")
    ]
    if broken:
        out.append(Finding(
            WARN, f"{len(broken)} Teams channel URL(s) are not https",
            ", ".join(broken),
            "A Workflows webhook is always an https URL — check for a truncated paste.",
        ))
    if muted and not targets:
        out.append(Finding(
            WARN, "Every Teams channel is muted",
            f"All of {', '.join(muted)} have Active off, and no other webhook is set.",
            "Tick Active on the channel you want notified, or accept that Teams is off.",
        ))

    if targets or config.teams_agent_enabled:
        named = [t.label for t in targets]
        label = f"{len(targets)} Teams channel(s)" + (
            f" ({', '.join(named)})" if named else ""
        ) if targets else "no webhook"
        if muted:
            label += f" · {len(muted)} muted"
        bot = "two-way bot on" if config.teams_agent_enabled else "bot off"
        out.append(Finding(OK, f"Notifications: {label}, {bot}"))
        return out
    out.append(Finding(
        WARN, "The autopilot cannot tell anyone anything",
        "No Teams webhook and no two-way bot.",
        "Add a Teams Workflows webhook URL (Notifications), or enable the bot. Otherwise "
        "results only appear on the dashboard and in ADO.",
    ))
    return out


def check_alerts(config: Settings) -> list[Finding]:
    """Whether the alert policy will actually let anything through.

    Every finding here is a way to be silent WITHOUT it looking like a fault: nothing
    errors, nothing logs, the dashboard is green, and a stuck PR simply never gets
    mentioned. That is the failure this check exists to make visible.
    """
    from ai_autopilot.notifications.base import ALL_EVENTS, Severity

    out: list[Finding] = []
    raw = (config.alert_events or "").strip()
    if raw:
        named = {p.strip().lower() for p in raw.split(",") if p.strip()}
        unknown = sorted(named - set(ALL_EVENTS))
        if unknown:
            out.append(Finding(
                WARN, "Alert events that match nothing",
                ", ".join(unknown),
                "These are dropped, so they look enabled on the Settings page and fire "
                f"for nothing. Valid: {', '.join(ALL_EVENTS)}.",
            ))
        if named and not (named & set(ALL_EVENTS)):
            out.append(Finding(
                ERROR, "No valid alert event is enabled",
                f"alert_events={raw!r}",
                "Not one name is recognised, so NOTHING will ever be notified. Clear the "
                "field to allow everything, or fix the names.",
            ))

    floor = (config.alert_min_severity or "").strip()
    if floor and floor.upper() not in Severity.__members__:
        out.append(Finding(
            WARN, "Alert severity floor is not a severity",
            f"alert_min_severity={floor!r}",
            'Falling back to "info" (deliver everything). Use info / warning / critical.',
        ))
    elif Severity.parse(floor, Severity.INFO) is Severity.CRITICAL:
        out.append(Finding(
            WARN, "Only blocking alerts will be delivered",
            'alert_min_severity="critical"',
            "Failed runs and reviewer reminders are WARNING, so they are suppressed. "
            'Use "warning" unless total quiet is what you want.',
        ))

    if config.digest_respect_quiet_hours and config.notify_hours_start \
            and not resolve_tz(config.timezone):
        out.append(Finding(
            WARN, "Quiet hours are set but have no timezone",
            f"notify_hours={config.notify_hours_start}-{config.notify_hours_end}",
            "Quiet hours need `timezone` (e.g. Asia/Ho_Chi_Minh) or they do not apply "
            "at all — the host clock is usually UTC, so an evening window would mean "
            "the middle of the afternoon.",
        ))

    if config.alert_dedup_enabled and config.alert_repeat_hours == 0:
        out.append(Finding(
            OK, "Alerts are reported once and never repeated",
            "alert_repeat_hours=0",
            "An item nobody acts on is mentioned once, then only if the wait doubles. "
            "Fine if the team works the Delivery page; set a value if chat is the only "
            "place anyone looks.",
        ))
    return out


def _check_digest_schedule(config: Settings) -> list[Finding]:
    """Checked BEFORE the bot-enabled gate, because "scheduled against a bot that is off"
    is one of the things worth reporting — and it was unreachable while it lived after the
    early return in check_teams_bot: the one config it describes is the one config that
    returned before reaching it.
    """
    out: list[Finding] = []
    digest_at = config.teams_agent_digest_at.strip()
    if digest_at and parse_hhmm(digest_at) is None:
        out.append(Finding(
            ERROR, "Digest time is not a time of day",
            f"teams_agent_digest_at={digest_at!r}",
            'Use 24-hour "HH:MM", e.g. "09:00". The digest loop refuses to run on a '
            "value it can't parse rather than guess an hour nobody chose.",
        ))
    elif digest_at and config.teams_agent_digest_interval_hours > 0:
        out.append(Finding(
            WARN, "Digest has both a fixed time and an interval",
            f"digest_at={digest_at} · interval={config.teams_agent_digest_interval_hours}h",
            "The fixed time wins and the interval is ignored. Clear the interval so the "
            "config says what actually happens.",
        ))
    if (digest_at or config.teams_agent_digest_interval_hours > 0) \
            and not config.teams_agent_enabled:
        out.append(Finding(
            WARN, "Digest scheduled with the bot off", "",
            "The digest posts through the bot; enable it, or clear both the fixed time "
            "and the interval.",
        ))
    return out


def check_teams_bot(config: Settings) -> list[Finding]:
    """The trap that cost the most time: the bot switched on but unable to work."""
    out: list[Finding] = _check_digest_schedule(config)
    if not config.teams_agent_enabled:
        return out
    missing = [
        name for name, value in (
            ("teams_agent_app_id", config.teams_agent_app_id),
            ("teams_agent_app_secret", config.teams_agent_app_secret),
            ("teams_agent_tenant_id", config.teams_agent_tenant_id),
        ) if not value
    ]
    if missing:
        out.append(Finding(
            ERROR, "Two-way Teams bot enabled but not configured",
            "Missing: " + ", ".join(missing),
            "build_agent() returns None without these, so /api/messages is never "
            "registered — the bot is silently absent rather than broken. Fill them from "
            "the Azure Bot resource, or turn the bot off.",
        ))
    if config.health_host in ("127.0.0.1", "localhost", "::1"):
        out.append(Finding(
            ERROR, "Teams can never reach the bot",
            f"health_host={config.health_host} is loopback only.",
            "Teams calls the messaging endpoint from the internet. Expose the app over "
            "HTTPS and point the Azure Bot's endpoint at https://<host>/api/messages.",
        ))
    return out or [Finding(OK, "Teams bot configuration complete")]


def check_versions() -> list[Finding]:
    """Version declared in more than one place drifts, and nothing notices.

    Exactly what happened here: ``__init__.__version__`` sat at 2.0.0 through five
    releases while pyproject and app.py moved on."""
    root = _repo_root()
    if root is None:
        return []
    found: dict[str, str] = {}
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    if m:
        found["pyproject.toml"] = m.group(1)
    init = root / "ai_autopilot" / "__init__.py"
    if init.is_file():
        m = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
        if m:
            found["__init__.py"] = m.group(1)
    app = root / "ai_autopilot" / "app.py"
    if app.is_file():
        m = re.search(r'FastAPI\([^)]*version="([^"]+)"', app.read_text(encoding="utf-8"))
        if m:
            found["app.py"] = m.group(1)
    manifest = root / "teams-app" / "manifest.json"
    if manifest.is_file():
        with open(manifest, encoding="utf-8") as fh:
            found["teams-app/manifest.json"] = json.load(fh).get("version", "")
    if len(set(found.values())) > 1:
        return [Finding(
            ERROR, "Version drifted between files",
            ", ".join(f"{k}={v}" for k, v in sorted(found.items())),
            "Bump them together. A release built from a drifted tree ships one version in "
            "its metadata and reports another at runtime.",
        )]
    if found:
        return [Finding(OK, f"Version consistent ({next(iter(found.values()))})")]
    return []


def check_teams_manifest() -> list[Finding]:
    """The Teams app package is rejected wholesale for small schema violations."""
    root = _repo_root()
    if root is None:
        return []
    path = root / "teams-app" / "manifest.json"
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return [Finding(ERROR, "teams-app/manifest.json unreadable", str(exc),
                        "Teams rejects the package before looking at anything else.")]
    out: list[Finding] = []
    bots = manifest.get("bots") or []
    commands = [
        cmd for bot in bots for cl in (bot.get("commandLists") or [])
        for cmd in (cl.get("commands") or [])
    ]
    if len(commands) > _TEAMS_COMMAND_LIMIT:
        out.append(Finding(
            ERROR, "Teams command menu over the limit",
            f"{len(commands)} commands; Teams allows {_TEAMS_COMMAND_LIMIT} per list.",
            "Drop the least useful from commandLists. Teams refuses the whole package, "
            "not just the extra entry.",
        ))
    no_slash = [c["title"] for c in commands if not str(c.get("title", "")).startswith("/")]
    if no_slash:
        out.append(Finding(
            ERROR, "Teams command titles missing the leading '/'",
            ", ".join(no_slash),
            "Teams inserts the title verbatim into the chat box and the bot dispatches on "
            "/command, so tapping these runs nothing.",
        ))
    # Drift against the commands the bot actually implements.
    try:
        from ai_autopilot.teams_agent import _COMMANDS

        declared = {str(c.get("title", "")).lower() for c in commands}
        implemented = set(_COMMANDS)
        unknown = declared - implemented
        if unknown:
            out.append(Finding(
                WARN, "Teams menu advertises commands the bot does not implement",
                ", ".join(sorted(unknown)),
                "Remove them from commandLists, or implement them in teams_agent._COMMANDS.",
            ))
    except Exception:  # noqa: BLE001 — the manifest check must not depend on the bot extra
        pass
    return out or [Finding(OK, "Teams app manifest valid")]


def check_command_hints(config: Settings) -> list[Finding]:
    if not config.comment_commands:
        return [Finding(
            WARN, "PR /command trigger is off", "comment_command is empty.",
            "Nobody can address the autopilot from a pull request comment.",
        )]
    slash = [c for c in config.comment_commands if c.startswith("/")]
    advisory = set(config.advisory_commands)
    unknown_advisory = advisory - set(config.comment_commands)
    out: list[Finding] = []
    if unknown_advisory:
        out.append(Finding(
            WARN, "Advisory command not in the accepted command list",
            ", ".join(sorted(unknown_advisory)),
            "It will never match, so those commands are treated as ACTION (they change "
            "code). Add them to comment_command or remove them from the advisory list.",
        ))
    if not advisory & set(slash):
        out.append(Finding(
            WARN, "No advisory command configured", "",
            "Every command — and every inferred @mention — is then an ACTION that revises "
            "the branch. Keep at least /review advisory.",
        ))
    return out or [Finding(OK, f"{len(slash)} PR commands configured")]


def check_pr_review(config: Settings) -> list[Finding]:
    """The PR-review group: settings that read as configured but can never take effect."""
    out: list[Finding] = []
    if config.pr_auto_review_on_added and not config.pr_reviewer_tracking_enabled:
        out.append(Finding(
            WARN, "Auto-review can never fire",
            "pr_auto_review_on_added=true but pr_reviewer_tracking_enabled=false.",
            "Detecting that the bot was added as a reviewer is the tracker's job, and the "
            "tracker never starts. Enable tracking, or turn auto-review off.",
        ))
    identity = config.pr_bot_identity
    if identity and identity != identity.strip().strip("\"'"):
        out.append(Finding(
            WARN, "pr_bot_identity has stray whitespace or quotes",
            f"Configured as {identity!r}.",
            "It is compared verbatim against the reviewer's email / display name, so a "
            "pasted quote or trailing space means the bot never recognises itself and "
            "silently never auto-reviews. Store the bare value.",
        ))
    if not (config.feedback_loop_enabled or config.pr_reviewer_tracking_enabled):
        out.append(Finding(
            OK, "PR review & feedback: off (both services disabled)"
        ))
        return out
    enabled = [
        name for name, on in (
            ("feedback loop", config.feedback_loop_enabled),
            ("reviewer tracking", config.pr_reviewer_tracking_enabled),
            ("auto-review", config.pr_auto_review_on_added),
        ) if on
    ]
    concurrency = config.pr_review_max_concurrent or config.max_concurrent
    shared = "" if config.pr_review_max_concurrent else " (shared with execution)"
    out.append(Finding(
        OK, f"PR review: {', '.join(enabled)} · max {concurrency} parallel{shared}"
    ))
    return out


def check_reviewer_reminders(config: Settings) -> list[Finding]:
    """Reminder settings that read as configured but can never fire."""
    first = config.pr_reviewer_reminder_hours
    repeat = config.pr_reviewer_reminder_repeat_hours
    if repeat and not first:
        return [Finding(
            WARN, "Repeat reminders can never fire",
            f"pr_reviewer_reminder_repeat_hours={repeat} but "
            "pr_reviewer_reminder_hours=0, which turns reminders off entirely.",
            "The repeat clock only starts after a first reminder, so this setting does "
            "nothing. Set pr_reviewer_reminder_hours, or clear the repeat value.",
        )]
    if first and not config.pr_reviewer_tracking_enabled:
        return [Finding(
            WARN, "Reviewer reminders configured but tracking is off",
            f"pr_reviewer_reminder_hours={first} with "
            "pr_reviewer_tracking_enabled=false.",
            "The tracker never starts, so nobody is ever reminded. Enable tracking, or "
            "set the reminder hours to 0 so the intent is clear.",
        )]
    if not first:
        return []
    cadence = f"every {repeat}h after" if repeat else "once only"
    return [Finding(OK, f"Reviewer reminders: first at {first}h, then {cadence}")]


def check_assignee_scoping(config: Settings) -> list[Finding]:
    """Assignee values that cannot identify one person.

    These fields decide whose work items THIS machine acts on. A lone first name matched by
    substring claimed every colleague who shares it; matching is now strict for such a
    value, which means it very likely matches NOBODY — visible here rather than as an
    autopilot that mysteriously stopped picking anything up.
    """
    # (label, what it scopes, value) — the value is carried rather than re-derived from the
    # label, so the list entries don't need their index parsed back out of a string.
    fields: list[tuple[str, str, str]] = [
        (key, what, (getattr(config, key, "") or "").strip())
        for key, what in (
            ("auto_transition_assignee", "auto transitions"),
            ("assignee_trigger_user", "the shared assignee trigger tag"),
            ("command_user", "/commands and @mentions"),
        )
    ]
    # Every extra commander is checked too — an unusable entry there fails the same way,
    # and is easier to miss because the list is edited less often than the owner.
    fields += [
        (f"command_users[{i}]", "/commands and @mentions", (value or "").strip())
        for i, value in enumerate(config.command_users or [])
    ]
    out: list[Finding] = []
    for key, what, value in fields:
        if not is_ambiguous_user(value):
            continue
        out.append(Finding(
            WARN, f'{key}="{value}" cannot identify one person',
            f"It scopes {what}. A single bare word appears in every colleague who shares "
            f'it ("Phong" is in both "Phong Pham" and "Phong Nguyen"), so it is matched '
            "strictly — it must equal the display name or the email local part, and will "
            "otherwise match nobody.",
            "Use the full email (e.g. phong.pham@nois.vn), or the full display name.",
        ))
    # What the COMMAND gate actually enforces — not the ownership roster. With
    # commands_from_anyone on they differ, and printing the roster there would describe a
    # restriction that is no longer applied.
    roster = config.command_allowlist
    if config.commands_from_anyone:
        out.append(Finding(
            OK, "Commands accepted from anyone (commands_from_anyone: true)"
        ))
    elif roster:
        out.append(Finding(
            OK, f"Commands accepted from: {describe_users(roster, limit=6)}"
        ))
    else:
        out.append(Finding(
            OK, "Commands accepted from anyone (no owner or command_users configured)"
        ))
    return out


def check_state_flows(config: Settings) -> list[Finding]:
    """Auto-transition config that reads as working but can't be.

    Deliberately offline, like every check here — whether a state exists on a given
    work-item type is a question only Azure DevOps can answer, and /dashboard/flow asks
    it there. What CAN be settled from the config alone is checked: structure, a type
    claimed twice, and a roll-up map too short to ever match.
    """
    flows = [f for f in (config.work_item_flows or []) if isinstance(f, dict)]
    out: list[Finding] = []

    structural = flows_mod.validate_flows(config.work_item_flows or [], {})
    out.extend(
        Finding(ERROR, "State flow config is malformed", reason,
                "Fix it at /dashboard/flow, which validates against the project's real "
                "work-item types.")
        for reason in structural
    )

    if flows and not config.auto_transition_enabled:
        out.append(Finding(
            WARN, f"{len(flows)} state flow(s) configured but auto transitions are off",
            "auto_transition_enabled=false, so state_sync never starts.",
            "Enable auto transitions in Settings, or delete the flows so the intent is clear.",
        ))

    if config.auto_transition_enabled and not flows and config.on_merge_state:
        out.append(Finding(
            WARN, "Merge transition applies one state to every work-item type",
            f'on_merge_state="{config.on_merge_state}" is used for Bug, Requirement, '
            "Feature — every type. An ADO state belongs to a type, so this is rejected "
            "for every type that doesn't define it, and the item is then tagged done "
            "without having moved.",
            "Group your types at /dashboard/flow and give each group a state it has.",
        ))

    # A roll-up holds unless EVERY child state has a line, so a one-line map can only
    # match if all children are always in that single state. That is how a map with one
    # wrong entry stayed silently dead: it looked configured.
    for label, entries in [
        ("parent_rollup_map", list(config.parent_rollup_map or [])),
        *[(f'flow "{f.get("name")}"', [str(x) for x in (f.get("rollup") or [])])
          for f in flows],
    ]:
        if len(entries) == 1:
            child, _ = flows_mod.parse_rollup_entry(entries[0])
            out.append(Finding(
                WARN, f"Parent roll-up in {label} has a single line",
                f'Only "{child}" is mapped, so the roll-up is held whenever any child is '
                "in any other state — which is nearly always.",
                "Add a line for every state a child can be in (the Flow page lists them).",
            ))

    if flows and not out:
        covered = sorted({str(t) for f in flows for t in (f.get("types") or [])})
        out.append(Finding(
            OK, f"State flows: {len(flows)} group(s) covering {', '.join(covered)}"
        ))
    return out


CHECKS = (
    check_ado, check_trigger, check_projects, check_workspace, check_workspaces,
    check_concurrency, check_delivery, check_effort,
    check_autonomy, check_dashboard_security, check_notifications, check_alerts,
    check_teams_bot,
    check_command_hints, check_reviewer_reminders, check_pr_review, check_state_flows,
    check_assignee_scoping,
)


def diagnose(config: Settings) -> list[Finding]:
    """Run every check. Config-only: no network, no writes."""
    out: list[Finding] = []
    for check in CHECKS:
        out.extend(check(config))
    out.extend(check_versions())
    out.extend(check_teams_manifest())
    return out


def render(findings: list[Finding]) -> str:
    """Human-readable report, problems first, ending with what to do next."""
    errors = [f for f in findings if f.level == ERROR]
    warns = [f for f in findings if f.level == WARN]
    oks = [f for f in findings if f.level == OK]

    lines = ["AI Autopilot — configuration doctor", ""]
    for group, label in ((errors, "Must fix"), (warns, "Worth fixing")):
        if not group:
            continue
        lines.append(f"{label} ({len(group)})")
        for f in group:
            lines.append(f"  {_LEVEL_ICON[f.level]} {f.title}")
            if f.detail:
                lines.append(f"       {f.detail}")
            if f.fix:
                lines.append(f"       → {f.fix}")
        lines.append("")
    if oks:
        lines.append(f"Passing ({len(oks)})")
        lines.extend(f"  {_LEVEL_ICON[OK]} {f.title}" for f in oks)
        lines.append("")

    todo = (errors + warns)[:3]
    if todo:
        lines.append("Do these first")
        lines.extend(f"  {i}. {f.title}" for i, f in enumerate(todo, 1))
    else:
        lines.append("Nothing to fix — configuration is coherent.")
    lines += [
        "",
        "This checks CONFIGURATION only. For runtime reachability (ADO, Claude, disk) "
        "see /health.",
    ]
    return "\n".join(lines)


def _emit(text: str) -> None:
    """Print the report on a console that may not speak Unicode.

    A stock Windows console is cp1252, where printing "✅" raises UnicodeEncodeError and the
    whole command dies with a traceback instead of a report — a diagnostic tool that crashes
    while diagnosing is worse than useless. Try UTF-8 first, then fall back to ASCII markers
    rather than losing the content."""
    try:
        print(text)
        return
    except UnicodeEncodeError:
        pass
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
        return
    for fancy, plain in ((ERROR_ICON, "[X]"), (WARN_ICON, "[!]"), (OK_ICON, "[ok]"),
                         ("→", "->")):
        text = text.replace(fancy, plain)
    print(text.encode("ascii", "replace").decode("ascii"))


def run() -> int:
    """CLI body. Exit code 1 when something must be fixed, else 0."""
    from ai_autopilot.config import load_settings

    findings = diagnose(load_settings())
    _emit(render(findings))
    return 1 if any(f.level == ERROR for f in findings) else 0
