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

from ai_autopilot.config import Settings

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
    channels = len(config.teams_webhooks)
    if channels or config.teams_agent_enabled:
        label = f"{channels} Teams webhook(s)" if channels else "no webhook"
        bot = "two-way bot on" if config.teams_agent_enabled else "bot off"
        return [Finding(OK, f"Notifications: {label}, {bot}")]
    return [Finding(
        WARN, "The autopilot cannot tell anyone anything",
        "No Teams webhook and no two-way bot.",
        "Add a Teams Workflows webhook URL (Notifications), or enable the bot. Otherwise "
        "results only appear on the dashboard and in ADO.",
    )]


def check_teams_bot(config: Settings) -> list[Finding]:
    """The trap that cost the most time: the bot switched on but unable to work."""
    if not config.teams_agent_enabled:
        return []
    out: list[Finding] = []
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
    if config.teams_agent_digest_interval_hours > 0 and not config.teams_agent_enabled:
        out.append(Finding(
            WARN, "Digest scheduled with the bot off", "",
            "The digest posts through the bot; enable it or set the interval to 0.",
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


CHECKS = (
    check_ado, check_trigger, check_workspace, check_concurrency, check_effort,
    check_autonomy, check_dashboard_security, check_notifications, check_teams_bot,
    check_command_hints, check_reviewer_reminders,
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
