"""Helpers more than one dashboard route module needs: the page context every
template renders with, and the setup-flow steps shared by Setup and Settings."""

from __future__ import annotations

from datetime import datetime

from fastapi import Request

from ai_autopilot import workspaces as workspaces_mod
from ai_autopilot.dashboard.common import (
    _category_badge,
    _fmt_ago,
    _fmt_duration,
    _mmss,
    _model_label,
    _status_class,
    _tokens_detail,
    selected_workspace,
)


def _ctx(request: Request, active: str, **extra) -> dict:
    cfg = getattr(getattr(request.app.state, "container", None), "config", None)
    # The workspace selector lives in the sidebar, so every page needs the list and
    # the current choice. Resolved here rather than per-route so a page can never
    # render the shell with a selector that disagrees with its own data.
    all_workspaces = workspaces_mod.resolve(cfg) if cfg else []
    updater = getattr(request.app.state, "updater", None)
    current = selected_workspace(request)
    if current != "all" and not any(w.id == current for w in all_workspaces):
        current = "all"   # a renamed or deleted workspace must not strand the view
    return {
        "request": request,
        "active": active,
        "workspaces": all_workspaces,
        "current_workspace": current,
        # Only worth showing a selector when there is something to choose between.
        "show_workspace_picker": len(all_workspaces) > 1,
        # Drives the sidebar's logout button: with no password there is no session to
        # end, so offering "Đăng xuất" would be a control that does nothing.
        "dashboard_locked": bool(
            cfg and (cfg.dashboard_auth_password_hash or cfg.dashboard_auth_token)
        ),
        "version": request.app.version,  # single source of truth: FastAPI(version=...)
        # A newer release, surfaced on EVERY page rather than on one somebody has to
        # think to visit. Read from the service's cached result — never a network
        # call on a page load. `update_*` stays falsy when the checker is off or has
        # not run, so the banner simply is not there.
        "update_ready": bool(updater and updater.available),
        "update_version": (updater.latest.version if updater and updater.latest else ""),
        "update_notes": (updater.latest.notes_url if updater and updater.latest else ""),
        "update_block": (updater.blocked() if updater and updater.available else ""),
        "update_job": (updater.job if updater else None),
        # For the manual check on Settings: what the last check found (even when it is
        # not newer) and when — "up to date" is only true as of a time.
        "update_latest": (updater.latest.version if updater and updater.latest else ""),
        "update_checked_at": (
            datetime.fromtimestamp(updater.checked_at).strftime("%Y-%m-%d %H:%M")
            if updater and updater.checked_at else ""
        ),
        "update_installable": bool(updater and updater.latest
                                   and getattr(updater.latest, "installable", False)),
        "update_service": updater is not None,
        # The Fleet page only means anything on the central VM — a worker's own
        # table is empty by definition, and a link to an empty page reads as a bug.
        "fleet_role": getattr(cfg, "fleet_role", "") if cfg else "",
        "mmss": _mmss,
        "fmt_duration": _fmt_duration,
        "fmt_ago": _fmt_ago,
        "category_badge": _category_badge,
        "status_class": _status_class,
        "model_label": _model_label,
        "tokens_detail": _tokens_detail,
        **extra,
    }


# ── First-run setup ──────────────────────────────────────────────────────
# The Settings page is ~240 fields across 20 sections, ordered by subject. That is
# the right shape for changing ONE thing and the wrong shape for the first hour:
# nothing there says which eight fields a machine cannot run without, and the eight
# are scattered across five sections. Three roles need three different eights, which
# is why the first question is which role this machine is.
#
# Deliberately NOT a parallel settings store: each step writes through the same
# save_to_yaml/apply_to_config as the Settings page, so there is no wizard state to
# lose, leaving halfway keeps what you answered, and coming back later is just
# opening the page again.
_SETUP_STEP_ADO = ("ado", "Kết nối Azure DevOps", (
    "ado_organization", "ado_project", "ado_pat",
))


# Same step, said differently. On a Jira machine this connection buys the PR half of
# the pipeline and nothing else, and doctor's own finding says a Jira team's code
# usually lives in Bitbucket or GitHub — so for most of them it is not needed at
# all. Naming it in the rail as an ordinary numbered step made the product look like
# it still requires Azure DevOps.
_SETUP_STEP_ADO_OPTIONAL = ("ado", "Kết nối Azure DevOps · tuỳ chọn",
                            _SETUP_STEP_ADO[2])


# Jira lives on a WORKSPACE, not on the root settings, so this step has no setting
# keys of its own — `_setup_save_jira` writes the workspace instead.
_SETUP_STEP_JIRA = ("jira", "Kết nối Jira", ())


_SETUP_STEP_SOURCE = ("source", "Nguồn work item", ())


# The wizard asked only for the trigger TAG, and asked for it on the step about
# source code. Both were wrong. doctor's own rule is that an item is picked up when
# it carries a trigger tag OR sits in a trigger state — leave the states blank and
# half the ways in simply do not exist, which is the ERROR it reports as "Nothing
# can ever be picked up". And the run-now tag is the answer to "how do I make THIS
# item go now", a question every new operator asks on day one; it lived only in
# Settings, 180 fields down, so people never learned it was there.
#
# Its own step rather than three more boxes under "Mã nguồn": this is a different
# question. One is where the code is, this is when the machine takes work on.
_SETUP_STEP_PICKUP = ("pickup", "Khi nào autopilot nhận việc", (
    "trigger_tag", "trigger_states", "stage_entry_tag",
))


def _setup_flow(role: str, source: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """The steps for this machine, in the order the answers depend on each other.

    The tracker question comes before the connection step because it decides which
    connection is being asked for. It used to be skipped entirely and the page went
    straight to "Kết nối Azure DevOps" — which reads as "this product is for ADO
    teams", while a Jira team's path existed the whole time on the Workspaces page,
    two clicks away and unmentioned.
    """
    # Jira answers "where do the work items come from"; ADO answers "where do the
    # pull requests live". A Jira team needs the second only if its code is in
    # Azure DevOps, which doctor's own finding says is usually not the case — so on
    # that branch the connection step is still OFFERED, but at the END and named as
    # optional. Sitting mid-rail between two required steps, it read as something
    # setup could not finish without, which is the opposite of true.
    jira = source == "jira"
    tracker = [_SETUP_STEP_JIRA] if jira else [_SETUP_STEP_ADO]
    trailing = [_SETUP_STEP_ADO_OPTIONAL] if jira else []
    if role == "worker":
        return [
            ("connect", "Nối về trung tâm", ("fleet_central_url", "fleet_token")),
            ("identity", "Máy này là ai", (
                "fleet_worker_name", "trigger_tag", "assignee_trigger_user",
                "sdlc_profile",
            )),
            _SETUP_STEP_SOURCE,
            *tracker,
            ("workspace", "Mã nguồn", ("workspace_directory",)),
            *trailing,
        ]
    steps = [
        _SETUP_STEP_SOURCE,
        *tracker,
        ("workspace", "Mã nguồn", ("workspace_directory", "base_branch")),
        # After the tracker connection, because its state list is read from the
        # project — asking which states mean "start here" before we can name them
        # leaves the operator typing them from memory.
        _SETUP_STEP_PICKUP,
    ]
    if role == "central":
        steps.append(
            ("fleet", "Phát cấu hình cho đội",
             ("fleet_token", "fleet_offline_after_minutes"))
        )
    steps.append((
        "policy",
        "Chính sách chung của đội" if role == "central" else "Cách máy này làm việc",
        ("autonomy_level", "claude_model", "execution_mode", "max_concurrent"),
    ))
    steps.extend(trailing)
    return steps


def _ws_attr(ws, key: str) -> str:
    """One field of a workspace entry, whichever shape it is in right now.

    A saved workspace is a plain dict until the config is re-read from disk —
    ``to_settings_updates`` writes dicts and ``apply_to_config`` assigns them
    without validation — so anything reading the LIVE config has to accept both.
    """
    if isinstance(ws, dict):
        return str(ws.get(key, "") or "")
    return str(getattr(ws, key, "") or "")


def _setup_source(request: Request, cfg) -> str:
    """Which tracker this machine's work items come from.

    Read from the query string while the wizard is walking, and otherwise derived
    from what is configured — so reopening the page later lands a Jira team back on
    their own path instead of on the ADO one.
    """
    asked = (request.query_params.get("src") or "").strip().lower()
    if asked in ("ado", "jira"):
        return asked
    return "jira" if any(
        _ws_attr(ws, "provider").strip().lower() == "jira"
        for ws in (cfg.workspaces or [])
    ) else "ado"


def _setup_role(cfg) -> str:
    return (cfg.fleet_role or "").strip() or "standalone"
