"""Two-way Microsoft Teams bot — chat commands + Adaptive Card button replies.

Optional feature, additive to the existing one-way Teams webhook
(``notifications/teams.py``, ``teams_webhook_url``) which keeps working unchanged.
This registers ``/api/messages`` so the bot can reply in Teams and act on button
clicks — e.g. "🔍 Review lại ngay" on a reminder card.

Requires the ``teams-bot`` extra (``pip install .[teams-bot]``) and
``teams_agent_enabled`` + the Agent ID / secret / tenant configured. Degrades to a
no-op (returns ``None``, the caller skips mounting the route) otherwise — nothing
else in the app changes.

Identity note: the bot only holds APP-ONLY credentials (client id/secret), never a
per-user token. It can act as ITSELF (e.g. cast its own reviewer vote, same as the
reviewer tracker's auto-review), but it can never cast a vote *as the human who
clicked the button* — that needs Teams SSO (delegated OAuth), which this does not
implement. Buttons are scoped to actions the bot can honestly perform as itself.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from ai_autopilot.config import Settings
from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger
from ai_autopilot.services.reviewer_tracker import ReviewerTrackerService

_log = get_logger("teams_agent")

_HELP_TEXT = (
    "🤖 **AI Autopilot**\n\n"
    "- `/items` — work item của bạn (khớp theo email Teams ↔ ADO assignee)\n"
    "- `/prs` — PR bạn là author hoặc reviewer, kèm tình trạng vote\n"
    "- `/review <repo> <pr-id>` — yêu cầu bot review lại PR ngay (bot phải đã là "
    "reviewer trên PR đó)\n"
    "- `/status` — tình trạng hoạt động\n"
    "- `/help` — bảng lệnh này\n\n"
    "Muốn `/ai /spec /test /qc /security /impact /summary` trên một PR cụ thể — "
    "reply ngay trên PR đó trong Azure DevOps (bot review ở đâu, trả lời ở đó)."
)

# ADO reviewer vote → short label (mirrors reviewer_tracker.VOTE_LABELS, compact form).
_VOTE_SHORT = {10: "✅ approved", 5: "✅ suggestions", 0: "⏳ chưa vote", -5: "⏸️ waiting", -10: "❌ rejected"}


def build_agent(config: Settings, container: Container, reviewer_tracker: ReviewerTrackerService):
    """Build the (agent_application, adapter) pair for ``/api/messages``, or ``None``
    if the Teams bot isn't configured/installed — the caller then skips the route
    entirely, leaving the rest of the app unaffected."""
    if not (
        config.teams_agent_enabled
        and config.teams_agent_app_id
        and config.teams_agent_app_secret
        and config.teams_agent_tenant_id
    ):
        return None
    try:
        from microsoft_agents.activity import ActivityTypes
        from microsoft_agents.authentication.msal import MsalConnectionManager
        from microsoft_agents.hosting.core import (
            AgentApplication,
            AgentAuthConfiguration,
            ApplicationOptions,
            AuthTypes,
            MemoryStorage,
            TurnContext,
        )
        from microsoft_agents.hosting.fastapi import CloudAdapter
    except ImportError as exc:
        _log.warning(
            "Teams bot packages not installed (pip install .[teams-bot]) — "
            "/api/messages disabled",
            error=str(exc),
        )
        return None

    auth_config = AgentAuthConfiguration(
        auth_type=AuthTypes.client_secret,
        client_id=config.teams_agent_app_id,
        client_secret=config.teams_agent_app_secret,
        tenant_id=config.teams_agent_tenant_id,
    )
    connections = MsalConnectionManager(
        connections_configurations={"SERVICE_CONNECTION": auth_config}
    )
    adapter = CloudAdapter(connection_manager=connections)
    app = AgentApplication(
        options=ApplicationOptions(
            adapter=adapter, bot_app_id=config.teams_agent_app_id, storage=MemoryStorage()
        ),
        connection_manager=connections,
    )

    @app.activity(ActivityTypes.message)
    async def on_message(context: TurnContext, _state) -> None:
        try:
            await _handle_turn(context, container, reviewer_tracker)
        except Exception as exc:  # noqa: BLE001 — a bot turn must not crash the process
            _log.error("Teams turn failed", error=str(exc))
            with contextlib.suppress(Exception):
                await context.send_activity("⚠️ Có lỗi khi xử lý — thử lại giúp mình nhé.")

    _log.info("Teams bot configured", app_id=config.teams_agent_app_id)
    return app, adapter


async def _handle_turn(context, container: Container, reviewer_tracker: ReviewerTrackerService) -> None:
    activity = context.activity
    payload: dict[str, Any] | None = getattr(activity, "value", None)
    if payload:
        await _handle_action(context, reviewer_tracker, payload)
        return
    await _handle_command(context, container, reviewer_tracker, (activity.text or "").strip())


async def _handle_action(context, reviewer_tracker: ReviewerTrackerService, payload: dict) -> None:
    """An Adaptive Card ``Action.Submit`` — currently only ``reverify`` (re-run the
    bot's own review on demand). See module docstring: never impersonates the human
    who clicked."""
    action = str(payload.get("action") or "").lower()
    repo_id, pr_id = payload.get("repo_id"), payload.get("pr_id")
    if action != "reverify" or not (repo_id and pr_id):
        await context.send_activity("Không nhận diện được hành động trên card này.")
        return
    status = await reviewer_tracker.trigger_review_now(str(repo_id), int(pr_id))
    await context.send_activity(status)


_REVIEW_RE = re.compile(r"^/review\s+(\S+)\s+(\d+)\s*$", re.IGNORECASE)


async def _handle_command(
    context, container: Container, reviewer_tracker: ReviewerTrackerService, text: str
) -> None:
    low = text.lower()
    if low in ("", "/help", "help"):
        await context.send_activity(_HELP_TEXT)
        return
    if low.startswith("/status"):
        await context.send_activity(
            "📊 Đang hoạt động. Xem chi tiết trên dashboard `/dashboard/reviews`."
        )
        return
    if low.startswith("/items"):
        await _reply_items(context, container)
        return
    if low.startswith("/prs"):
        await _reply_prs(context, reviewer_tracker)
        return
    m = _REVIEW_RE.match(text.strip())
    if m:
        repo_name, pr_id = m.group(1), int(m.group(2))
        repo_id = await _resolve_repo_id(container, repo_name)
        if repo_id is None:
            await context.send_activity(f"Không tìm thấy repo `{repo_name}`.")
            return
        status = await reviewer_tracker.trigger_review_now(repo_id, pr_id)
        await context.send_activity(status)
        return
    await context.send_activity(
        "Lệnh này chưa hỗ trợ trong chat Teams — reply trực tiếp trên PR trong Azure "
        "DevOps để dùng `/ai /spec /test /qc /security /impact /summary`, hoặc gõ "
        "`/help`."
    )


async def _resolve_repo_id(container: Container, repo_name: str) -> str | None:
    for repo in await container.ado.get_repositories():
        if (repo.get("name") or "").lower() == repo_name.lower():
            return repo.get("id")
    return None


async def _teams_email(context) -> str | None:
    """The email/UPN of whoever sent this turn — resolved via the Teams-specific
    member lookup (the base Activity only carries a Teams/AAD object id, not email).
    None if the lookup fails (e.g. running outside a real Teams tenant) — callers
    then report "identity chưa xác định" rather than silently showing everyone's data."""
    user_id = getattr(context.activity.from_property, "id", None)
    if not user_id:
        return None
    try:
        from microsoft_agents.hosting.teams import TeamsInfo

        member = await TeamsInfo.get_member(context, user_id)
        return member.email or member.user_principal_name
    except Exception as exc:  # noqa: BLE001 — identity lookup must not crash the turn
        _log.warning("Teams identity lookup failed", error=str(exc))
        return None


async def _reply_items(context, container: Container) -> None:
    email = await _teams_email(context)
    if not email:
        await context.send_activity(
            "⚠️ Không xác định được email của bạn trong Teams — không thể lọc work "
            "item riêng."
        )
        return
    items = await container.ado.get_work_items_by_assignee(email, top=20)
    if not items:
        await context.send_activity(f"📋 Không có work item nào gán cho `{email}`.")
        return
    lines = [f"📋 **Work item của bạn** ({len(items)})"]
    for it in items:
        lines.append(f"- #{it.id} [{it.work_item_type}] {it.title} — **{it.state}**")
    await context.send_activity("\n".join(lines))


async def _reply_prs(context, reviewer_tracker: ReviewerTrackerService) -> None:
    email = await _teams_email(context)
    if not email:
        await context.send_activity(
            "⚠️ Không xác định được email của bạn trong Teams — không thể lọc PR riêng."
        )
        return
    prs = await reviewer_tracker.prs_for_person(email)
    if not prs:
        await context.send_activity(f"🔀 Không có PR nào liên quan tới `{email}`.")
        return
    lines = [f"🔀 **PR của bạn** ({len(prs)})"]
    for pr in prs:
        draft = " (draft)" if pr["is_draft"] else ""
        vote = _VOTE_SHORT.get(pr["vote"], "") if pr["role"] == "reviewer" else ""
        role = "✍️ author" if pr["role"] == "author" else "👀 reviewer"
        bits = " · ".join(x for x in (role, vote) if x)
        lines.append(f"- !{pr['id']} {pr['repo']}{draft} — {pr['title']} — {bits}")
    await context.send_activity("\n".join(lines))
