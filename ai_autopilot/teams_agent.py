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

import asyncio
import contextlib
import json
import re
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_autopilot.config import Settings
from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger
from ai_autopilot.services.reviewer_tracker import ReviewerTrackerService

_log = get_logger("teams_agent")


class _DbConversationStorage:
    """Persistent ``Storage`` backend (SDK protocol: read/write/delete) for the
    proactive-conversation store, backed by the same async DB as everything else.

    ``MemoryStorage`` (the SDK default) forgets every conversation on restart —
    fine for per-turn state, but it would silently break a DAILY digest the moment
    the process restarts. ``all_keys`` is our own addition (not part of the SDK
    protocol) so the digest loop can enumerate every channel to broadcast to."""

    def __init__(self, database) -> None:
        self._db = database

    async def read(self, keys: list[str], *, target_cls=None, **kwargs) -> dict:
        from ai_autopilot.data.entities import TeamsConversation

        out: dict = {}
        async with self._db.session() as session:
            for key in keys:
                row = await session.get(TeamsConversation, key)
                if row is None:
                    continue
                data = json.loads(row.value_json)
                out[key] = target_cls.from_json_to_store_item(data) if target_cls else data
        return out

    async def write(self, changes: dict) -> None:
        from ai_autopilot.data.entities import TeamsConversation

        async with self._db.session() as session:
            for key, item in changes.items():
                data = (
                    item.store_item_to_json() if hasattr(item, "store_item_to_json") else item
                )
                row = await session.get(TeamsConversation, key)
                if row is None:
                    row = TeamsConversation(key=key)
                    session.add(row)
                row.value_json = json.dumps(data)
                row.updated_at = datetime.now(UTC)
            await session.commit()

    async def delete(self, keys: list[str]) -> None:
        from ai_autopilot.data.entities import TeamsConversation

        async with self._db.session() as session:
            for key in keys:
                row = await session.get(TeamsConversation, key)
                if row is not None:
                    await session.delete(row)
            await session.commit()

    async def all_keys(self) -> list[str]:
        from sqlalchemy import select

        from ai_autopilot.data.entities import TeamsConversation

        async with self._db.session() as session:
            rows = await session.execute(select(TeamsConversation.key))
            return list(rows.scalars().all())

_HELP_TEXT = (
    "🤖 **AI Autopilot**\n\n"
    "- `/items` — work item của bạn (khớp theo email Teams ↔ ADO assignee)\n"
    "- `/prs` — PR bạn là author hoặc reviewer, kèm tình trạng vote\n"
    "- `/review <repo> <pr-id>` — yêu cầu bot review lại PR ngay (bot phải đã là "
    "reviewer trên PR đó)\n"
    "- `/pr <repo> <pr-id>` — xem chi tiết BẤT KỲ PR nào (không chỉ của bạn)\n"
    "- `/item <id>` — xem chi tiết BẤT KỲ work item nào\n"
    "- `/team` — tổng quan PR của cả team, cũ nhất trước\n"
    "- `/log <mô tả>` — tạo nhanh 1 Requirement trong ADO (có xác nhận trước khi tạo)\n"
    "- `/status` — tình trạng hoạt động\n"
    "- `/help` — bảng lệnh này\n\n"
    "💬 Cũng có thể gõ tự nhiên để hỏi (chỉ tra cứu, KHÔNG sửa/vote/merge được qua "
    "chat), vd: *\"PR nào của tôi đang bị block?\"*, *\"work item nào còn active?\"*\n\n"
    "Muốn `/ai /spec /test /qc /security /impact /summary` trên một PR cụ thể — "
    "reply ngay trên PR đó trong Azure DevOps (bot review ở đâu, trả lời ở đó)."
)

# ADO reviewer vote → short label (mirrors reviewer_tracker.VOTE_LABELS, compact form).
_VOTE_SHORT = {10: "✅ approved", 5: "✅ suggestions", 0: "⏳ chưa vote", -5: "⏸️ waiting", -10: "❌ rejected"}


def build_agent(config: Settings, container: Container, reviewer_tracker: ReviewerTrackerService):
    """Build ``(agent_application, adapter, digest_task)`` for ``/api/messages``, or
    ``None`` if the Teams bot isn't configured/installed — the caller then skips the
    route entirely, leaving the rest of the app unaffected. ``digest_task`` is the
    background asyncio task for the proactive daily digest, or ``None`` if
    ``teams_agent_digest_interval_hours`` is 0 (off) — the caller cancels it on
    shutdown alongside every other background service."""
    if not (
        config.teams_agent_enabled
        and config.teams_agent_app_id
        and config.teams_agent_app_secret
        and config.teams_agent_tenant_id
    ):
        return None
    try:
        from microsoft_agents.activity import ActivityTypes, ConversationUpdateTypes
        from microsoft_agents.authentication.msal import MsalConnectionManager
        from microsoft_agents.hosting.core import (
            AgentApplication,
            AgentAuthConfiguration,
            ApplicationOptions,
            AuthTypes,
            MemoryStorage,
            MessageFactory,
            TurnContext,
        )
        from microsoft_agents.hosting.core.app.proactive.proactive_options import (
            ProactiveOptions,
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
    conversation_storage = _DbConversationStorage(container.database)
    app = AgentApplication(
        options=ApplicationOptions(
            adapter=adapter, bot_app_id=config.teams_agent_app_id, storage=MemoryStorage(),
            proactive=ProactiveOptions(storage=conversation_storage),
        ),
        connection_manager=connections,
    )

    @app.activity(ActivityTypes.message)
    async def on_message(context: TurnContext, _state) -> None:
        try:
            await _handle_turn(context, config, container, reviewer_tracker)
        except Exception as exc:  # noqa: BLE001 — a bot turn must not crash the process
            _log.error("Teams turn failed", error=str(exc))
            with contextlib.suppress(Exception):
                await context.send_activity("⚠️ Có lỗi khi xử lý — thử lại giúp mình nhé.")

    @app.conversation_update(ConversationUpdateTypes.MEMBERS_ADDED)
    async def on_members_added(context: TurnContext, _state) -> None:
        """Remember this conversation ONLY when the BOT ITSELF was the member added
        (not some other user joining a channel it's already in) — that's the moment
        we can first proactively message it later."""
        try:
            bot_id = getattr(context.activity.recipient, "id", None)
            added = getattr(context.activity, "members_added", None) or []
            if bot_id and any(str(getattr(m, "id", "")) == str(bot_id) for m in added):
                await app.proactive.store_conversation(context)
                _log.info("Teams conversation stored for proactive digest")
        except Exception as exc:  # noqa: BLE001 — must not crash the turn
            _log.warning("storing conversation for digest failed", error=str(exc))

    digest_task = None
    if config.teams_agent_digest_interval_hours > 0:
        digest_task = asyncio.create_task(
            _digest_loop(config, container, app, adapter, reviewer_tracker,
                         conversation_storage, MessageFactory),
            name="teams-digest",
        )

    _log.info("Teams bot configured", app_id=config.teams_agent_app_id)
    return app, adapter, digest_task


async def _digest_loop(
    config: Settings, container: Container, app, adapter,
    reviewer_tracker: ReviewerTrackerService, storage: _DbConversationStorage,
    message_factory,
) -> None:
    interval = config.teams_agent_digest_interval_hours
    while True:
        try:
            await asyncio.sleep(interval * 3600)
            # The stats window MUST equal the send interval — not a fixed 24h —
            # otherwise a longer interval leaves a silent gap between digests, and a
            # shorter one double-counts the overlap. Single source of truth: interval.
            await _send_digest(
                container, app, adapter, reviewer_tracker, storage, message_factory,
                window_hours=interval,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad cycle must not kill the loop
            _log.error("Teams digest cycle failed", error=str(exc))


# ── Full daily digest — activity stats + team standup + PR sections ──────────

def _fmt_wi_list(items: list, limit: int = 5) -> str:
    shown = items[:limit]
    text = ", ".join(f"#{it.id} {it.title}" for it in shown)
    extra = len(items) - limit
    return text + (f" (+{extra} nữa)" if extra > 0 else "")


async def _active_pr_work_item_map(container: Container) -> dict[int, dict]:
    """work_item_id → its (non-draft) active PR, for every PR whose branch encodes a
    work item id — the signal for "code done, waiting on PR merge" that a work item's
    own ADO state can't reliably tell you (the poller may not have moved it yet)."""
    from ai_autopilot.services.pr_feedback import parse_work_item_id

    mapping: dict[int, dict] = {}
    for repo in await container.ado.get_repositories():
        repo_id = repo.get("id")
        if not repo_id:
            continue
        for pr in await container.ado.get_active_pull_requests(repo_id):
            if pr.get("isDraft"):
                continue
            wid = parse_work_item_id(pr.get("sourceRefName", ""))
            if wid is not None:
                mapping[wid] = pr
    return mapping


async def _team_standup(container: Container, cutoff: datetime) -> dict[str, dict[str, list]]:
    """Every work item in the project changed within ``cutoff``, grouped by assignee
    and bucketed: done / merge_pending (done but its PR hasn't merged yet) /
    in-progress / not-started. Best-effort — returns {} on any fetch failure so the
    digest degrades to its other sections."""
    try:
        items = await container.ado.get_all_active_work_items(top=300)
        categories = await container.ado.get_state_categories()
        pr_map = await _active_pr_work_item_map(container)
    except Exception as exc:  # noqa: BLE001 — one failed section must not break the digest
        _log.warning("team standup fetch failed", error=str(exc))
        return {}
    by_person: dict[str, dict[str, list]] = {}
    for it in items:
        changed = it.changed_date.replace(tzinfo=UTC) if (
            it.changed_date and it.changed_date.tzinfo is None
        ) else it.changed_date
        if changed is None or changed < cutoff:
            continue
        person = it.assigned_to or "(chưa gán)"
        bucket = by_person.setdefault(
            person, {"done": [], "merge_pending": [], "active": [], "todo": []}
        )
        category = categories.get(it.state, "")
        if it.id in pr_map:
            bucket["merge_pending"].append(it)
        elif category in ("Resolved", "Completed"):
            bucket["done"].append(it)
        elif category == "InProgress":
            bucket["active"].append(it)
        elif category == "Proposed":
            bucket["todo"].append(it)
    return by_person


def _format_standup(by_person: dict[str, dict[str, list]]) -> str:
    lines = []
    for person, buckets in sorted(by_person.items()):
        done = buckets["done"]
        merge_pending = buckets["merge_pending"]
        active, todo = buckets["active"], buckets["todo"]
        if not (done or merge_pending or active or todo):
            continue
        lines.append(f"**{person}**")
        if done:
            lines.append(f"  ✅ Hoàn thành: {_fmt_wi_list(done)}")
        if merge_pending:
            lines.append(f"  ⏳ Hoàn thành, chờ merge PR: {_fmt_wi_list(merge_pending)}")
        if active:
            lines.append(f"  🔧 Đang làm: {_fmt_wi_list(active)}")
        if todo:
            lines.append(f"  📋 Chưa làm: {_fmt_wi_list(todo)}")
    return "\n".join(lines) if lines else "(không có work item nào thay đổi trong khoảng này)"


def _format_pr_stub_list(prs: list[dict]) -> str:
    if not prs:
        return "(không có)"
    return "\n".join(f"- !{p['id']} {p['repo']} — {p['title']} · {p['author']}" for p in prs)


async def _send_digest(
    container: Container, app, adapter, reviewer_tracker: ReviewerTrackerService,
    storage: _DbConversationStorage, message_factory, *, window_hours: int = 24,
) -> None:
    """``window_hours`` MUST equal the actual send interval (the caller,
    ``_digest_loop``, always passes it) — a fixed 24h window regardless of how often
    the digest fires would leave a silent gap (interval > 24h) or double-count
    (interval < 24h). The default here only covers direct/manual calls."""
    cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
    window_label = f"{window_hours}h"
    parts: list[str] = [f"📊 **Digest — AI Autopilot ({window_label} qua)**"]

    # 1. Autopilot execution activity.
    try:
        stats = await container.execution_repo.get_stats(since=cutoff)
        parts.append(
            f"🤖 **Hoạt động autopilot ({window_label})**: {stats.total} task · "
            f"✅ {stats.success} thành công · ❌ {stats.failed} thất bại"
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("digest: execution stats failed", error=str(exc))

    # 2. Auto-review + reminders sent — from the reviewer tracker's own records.
    try:
        reviewer_repo = getattr(container, "pr_reviewer_repo", None)
        if reviewer_repo is not None:
            reviewed_n = await reviewer_repo.count_reviewed_since(cutoff)
            reminded_n = await reviewer_repo.count_reminded_since(cutoff)
            parts.append(
                f"🔍 **Reviewer tracking ({window_label})**: bot tự review {reviewed_n} PR · "
                f"👋 nhắc {reminded_n} reviewer quá hạn"
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("digest: reviewer stats failed", error=str(exc))

    # 3. New / merged PRs.
    try:
        new_prs = await reviewer_tracker.new_prs_since(cutoff)
        merged_prs = await reviewer_tracker.merged_prs_since(cutoff)
        parts.append(
            f"🔀 **PR ({window_label})**: {len(new_prs)} mới mở · {len(merged_prs)} đã merge"
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("digest: PR activity failed", error=str(exc))

    # 4. Tickets logged via /log.
    try:
        logged = await reviewer_tracker.tickets_logged_since(cutoff)
        if logged:
            parts.append(
                f"📝 **Ticket log qua Teams ({window_label})**: {_fmt_wi_list(logged, limit=10)}"
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("digest: logged tickets failed", error=str(exc))

    # 5. PRs ready to merge (not time-boxed — this is a current snapshot).
    try:
        ready = await reviewer_tracker.prs_ready_to_merge()
        parts.append(f"✅ **PR sẵn sàng merge** ({len(ready)})\n{_format_pr_stub_list(ready)}")
    except Exception as exc:  # noqa: BLE001
        _log.warning("digest: ready-to-merge failed", error=str(exc))

    # 6. Oldest active PRs (current snapshot — what's stuck).
    try:
        oldest = await reviewer_tracker.team_overview(limit=10)
        parts.append(
            f"🕰️ **PR active cũ nhất** ({len(oldest)})\n{_format_team_overview(oldest)}"
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("digest: team overview failed", error=str(exc))

    # 7. Work items by person, changed within the window — done / merge-pending /
    #    active / todo. Same cutoff as every other section above.
    try:
        standup = await _team_standup(container, cutoff)
        parts.append(f"👤 **Work item theo người ({window_label})**\n{_format_standup(standup)}")
    except Exception as exc:  # noqa: BLE001
        _log.warning("digest: standup failed", error=str(exc))

    text = "\n\n".join(parts)
    keys = await storage.all_keys()
    sent, failed = 0, 0
    for key in keys:
        try:
            conversation = await app.proactive.get_conversation(key)
            if conversation is None:
                continue
            await app.proactive.send_activity(adapter, conversation, message_factory.text(text))
            sent += 1
        except Exception as exc:  # noqa: BLE001 — one bad conversation must not stop the rest
            failed += 1
            _log.warning("digest send failed for one conversation", error=str(exc))
    _log.info("Teams digest sent", sent=sent, failed=failed, total=len(keys))


async def _handle_turn(
    context, config: Settings, container: Container, reviewer_tracker: ReviewerTrackerService
) -> None:
    activity = context.activity
    payload: dict[str, Any] | None = getattr(activity, "value", None)
    if payload:
        await _handle_action(context, container, reviewer_tracker, payload)
        return
    await _handle_command(context, config, container, reviewer_tracker, (activity.text or "").strip())


async def _handle_action(
    context, container: Container, reviewer_tracker: ReviewerTrackerService, payload: dict
) -> None:
    """An Adaptive Card ``Action.Submit`` — ``reverify`` (re-run the bot's own review
    on demand) or ``log_confirm``/``log_cancel`` (the /log ticket confirmation card).
    See module docstring: never impersonates the human who clicked — reverify votes as
    the bot itself, and log_confirm creates an administrative record, not code/votes."""
    action = str(payload.get("action") or "").lower()
    if action == "reverify":
        repo_id, pr_id = payload.get("repo_id"), payload.get("pr_id")
        if not (repo_id and pr_id):
            await context.send_activity("Không nhận diện được hành động trên card này.")
            return
        status = await reviewer_tracker.trigger_review_now(str(repo_id), int(pr_id))
        await context.send_activity(status)
        return
    if action == "log_confirm":
        await _create_logged_ticket(context, container, str(payload.get("title") or ""))
        return
    if action == "log_cancel":
        await context.send_activity("Đã hủy — không tạo ticket.")
        return
    await context.send_activity("Không nhận diện được hành động trên card này.")


async def _create_logged_ticket(context, container: Container, title: str) -> None:
    title = title.strip()
    if not title:
        await context.send_activity("⚠️ Thiếu nội dung ticket.")
        return
    email = await _teams_email(context) or "Teams"
    wid = await container.ado.create_work_item(
        title=title, item_type="Requirement", parent_id=None, tag="teams-logged",
        description=f"Logged via Microsoft Teams by {email}.",
    )
    if not wid:
        await context.send_activity("⚠️ Không tạo được work item — kiểm tra log server.")
        return
    org = container.config.ado_organization.rstrip("/")
    project = container.config.ado_project
    await context.send_activity(
        f"✅ Đã tạo **Requirement #{wid}** — {org}/{project}/_workitems/edit/{wid}"
    )


async def _send_log_confirm_card(context, title: str) -> None:
    """Confirmation card before creating a work item — this IS a real reply within the
    bot's own conversation (unlike the one-way Teams-webhook reminder card), so
    Action.Submit on it correctly round-trips back to /api/messages."""
    from microsoft_agents.hosting.core import CardFactory, MessageFactory

    card = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {
                "type": "TextBlock", "weight": "Bolder", "wrap": True,
                "text": "📝 Xác nhận tạo Requirement mới?",
            },
            {"type": "TextBlock", "wrap": True, "text": title},
        ],
        "actions": [
            {
                "type": "Action.Submit", "title": "✅ Xác nhận",
                "data": {"action": "log_confirm", "title": title},
            },
            {"type": "Action.Submit", "title": "❌ Hủy", "data": {"action": "log_cancel"}},
        ],
    }
    await context.send_activity(MessageFactory.attachment(CardFactory.adaptive_card(card)))


_REVIEW_RE = re.compile(r"^/review\s+(\S+)\s+(\d+)\s*$", re.IGNORECASE)
_LOG_RE = re.compile(r"^/log\s+(.+)$", re.IGNORECASE | re.DOTALL)
_PR_LOOKUP_RE = re.compile(r"^/pr\s+(\S+)\s+(\d+)\s*$", re.IGNORECASE)
_ITEM_LOOKUP_RE = re.compile(r"^/item\s+(\d+)\s*$", re.IGNORECASE)


def _format_team_overview(prs: list[dict]) -> str:
    if not prs:
        return "(không có PR active nào)"
    lines = []
    for pr in prs:
        age = f"{pr['age_days']}d" if pr["age_days"] is not None else "?"
        draft = " (draft)" if pr["is_draft"] else ""
        lines.append(
            f"- !{pr['id']} {pr['repo']}{draft} — {pr['title']} · {pr['author']} · "
            f"{age} tuổi · ✅{pr['approved']} ⏳{pr['pending']} ⛔{pr['blocked']}"
        )
    return "\n".join(lines)


def _format_pr_detail(detail: dict) -> str:
    draft = " (draft)" if detail["is_draft"] else ""
    lines = [
        f"🔀 **PR !{detail['id']}{draft}** — {detail['title']}",
        f"Author: {detail['author']} · `{detail['source']}` → `{detail['target']}`",
    ]
    if detail["reviewers"]:
        lines.append("Reviewers:")
        lines.extend(
            f"- {r['name']}: {_VOTE_SHORT.get(r['vote'], str(r['vote']))}"
            for r in detail["reviewers"]
        )
    else:
        lines.append("Chưa có reviewer nào.")
    return "\n".join(lines)


async def _handle_command(
    context, config: Settings, container: Container,
    reviewer_tracker: ReviewerTrackerService, text: str,
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
    if low.startswith("/team"):
        prs = await reviewer_tracker.team_overview()
        bullets = _format_team_overview(prs)
        await context.send_activity(
            f"👥 **Team overview — PR active, cũ nhất trước** ({len(prs)})\n{bullets}"
        )
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
    m = _LOG_RE.match(text.strip())
    if m:
        await _send_log_confirm_card(context, m.group(1).strip())
        return
    m = _PR_LOOKUP_RE.match(text.strip())
    if m:
        repo_name, pr_id = m.group(1), int(m.group(2))
        repo_id = await _resolve_repo_id(container, repo_name)
        if repo_id is None:
            await context.send_activity(f"Không tìm thấy repo `{repo_name}`.")
            return
        detail = await reviewer_tracker.pr_detail(repo_id, pr_id)
        if detail is None:
            await context.send_activity(
                f"PR !{pr_id} không tìm thấy hoặc không còn active trong `{repo_name}`."
            )
            return
        await context.send_activity(_format_pr_detail(detail))
        return
    m = _ITEM_LOOKUP_RE.match(text.strip())
    if m:
        wid = int(m.group(1))
        item = await container.ado.get_work_item(wid)
        if item is None:
            await context.send_activity(f"Không tìm thấy work item #{wid}.")
            return
        await context.send_activity(
            f"#{item.id} [{item.work_item_type}] {item.title}\n"
            f"- Trạng thái: **{item.state}**\n"
            f"- Assigned to: {item.assigned_to or '(chưa gán)'}"
        )
        return
    await _handle_free_text(context, config, container, reviewer_tracker, text)


def _as_int(value: Any) -> int | None:
    """Best-effort int parse of the classifier's ``filter`` value (a string like
    "2261", occasionally already a number) — None on anything unparseable."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


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


async def _items_data(context, container: Container) -> tuple[str | None, list]:
    """Resolve the caller's identity and fetch their raw (unfiltered) work items."""
    email = await _teams_email(context)
    if not email:
        return None, []
    items = await container.ado.get_work_items_by_assignee(email, top=20)
    return email, items


def _format_items(items: list, state_filter: str | None = None) -> tuple[list, str]:
    """Filter + render as bullets. Returns (filtered_items, bullet_text)."""
    filtered = [
        it for it in items
        if not state_filter or state_filter.lower() in (it.state or "").lower()
    ]
    lines = [f"- #{it.id} [{it.work_item_type}] {it.title} — {it.state}" for it in filtered]
    return filtered, "\n".join(lines) if lines else "(không có work item nào khớp)"


async def _prs_data(context, reviewer_tracker: ReviewerTrackerService) -> tuple[str | None, list]:
    """Resolve the caller's identity and fetch their raw (unfiltered) PRs."""
    email = await _teams_email(context)
    if not email:
        return None, []
    prs = await reviewer_tracker.prs_for_person(email)
    return email, prs


def _format_prs(prs: list, vote_filter: str | None = None) -> tuple[list, str]:
    """Filter + render as bullets. Returns (filtered_prs, bullet_text)."""
    filtered = prs
    if vote_filter == "blocked":
        filtered = [p for p in prs if p["vote"] is not None and p["vote"] < 0]
    elif vote_filter == "pending":
        filtered = [p for p in prs if p["role"] == "reviewer" and p["vote"] == 0]
    elif vote_filter == "approved":
        filtered = [p for p in prs if p["vote"] is not None and p["vote"] >= 5]
    lines = []
    for pr in filtered:
        draft = " (draft)" if pr["is_draft"] else ""
        vote = _VOTE_SHORT.get(pr["vote"], "") if pr["role"] == "reviewer" else ""
        role = "✍️ author" if pr["role"] == "author" else "👀 reviewer"
        bits = " · ".join(x for x in (role, vote) if x)
        lines.append(f"- !{pr['id']} {pr['repo']}{draft} — {pr['title']} — {bits}")
    return filtered, "\n".join(lines) if lines else "(không có PR nào khớp)"


async def _reply_items(context, container: Container, state_filter: str | None = None) -> None:
    """Plain, structured reply for the explicit ``/items`` command — a command
    deserves a scannable list, not phrased prose (and costs no extra Claude call)."""
    email, items = await _items_data(context, container)
    if email is None:
        await context.send_activity(
            "⚠️ Không xác định được email của bạn trong Teams — không thể lọc work "
            "item riêng."
        )
        return
    filtered, bullets = _format_items(items, state_filter)
    if not filtered:
        await context.send_activity(f"📋 Không có work item nào khớp cho `{email}`.")
        return
    await context.send_activity(f"📋 **Work item của bạn** ({len(filtered)})\n{bullets}")


async def _reply_prs(
    context, reviewer_tracker: ReviewerTrackerService, vote_filter: str | None = None
) -> None:
    """Plain, structured reply for the explicit ``/prs`` command (see _reply_items)."""
    email, prs = await _prs_data(context, reviewer_tracker)
    if email is None:
        await context.send_activity(
            "⚠️ Không xác định được email của bạn trong Teams — không thể lọc PR riêng."
        )
        return
    filtered, bullets = _format_prs(prs, vote_filter)
    if not filtered:
        await context.send_activity(f"🔀 Không có PR nào khớp cho `{email}`.")
        return
    await context.send_activity(f"🔀 **PR của bạn** ({len(filtered)})\n{bullets}")


# ── Free-text understanding (read-only queries only) ─────────────────────────
#
# SAFETY: this is a two-layer guard, not a single point of trust in the model.
#   1. A deterministic keyword pre-filter refuses anything that reads like a mutating
#      request BEFORE any Claude call — cheap, and doesn't depend on the model behaving.
#   2. Even past that filter, the classifier's OUTPUT SCHEMA only has four intents —
#      items / prs / status / help — and _dispatch_intent below has no branch that
#      calls anything but the existing read-only reply functions. There is no code
#      path from free text to /ai, cast_pull_request_vote, or any write — the model's
#      only power is picking among reply functions that were already safe to call.
_MUTATION_HINTS = (
    "sửa", "sửa code", "fix", "thay đổi code", "cập nhật code", "update code",
    "chỉnh sửa", "edit code", "change code", "commit", "push", " merge ",
    "approve", "reject", "revert", "xoá pr", "delete", "vote giúp", "cast vote",
)

_REDIRECT_TO_ADO = (
    "Việc sửa code / vote / merge chỉ thực hiện được khi reply trực tiếp trên PR "
    "trong Azure DevOps — không hỗ trợ qua chat Teams. Gõ `/help` để xem lệnh đọc "
    "hỗ trợ ở đây."
)

_INTENT_PROMPT = """Bạn là bộ phân loại Ý ĐỊNH cho một bot Teams CHỈ ĐỌC dữ liệu — \
không có khả năng và không được phép sửa code, vote, merge hay thay đổi bất cứ gì.

Phân loại tin nhắn của người dùng vào ĐÚNG MỘT trong các intent sau, và trả về \
CHÍNH XÁC một dòng JSON, không kèm giải thích, không markdown:
{{"intent": "items|prs|pr_lookup|item_lookup|team_overview|status|help|unknown", \
"filter": null hoặc giá trị tương ứng bên dưới}}

- "items": người dùng muốn xem WORK ITEM CỦA CHÍNH HỌ. filter = null hoặc từ khoá \
trạng thái.
- "prs": người dùng muốn xem PULL REQUEST họ là author/reviewer (KHÔNG chỉ định số PR \
cụ thể). filter = "blocked" (bị từ chối/waiting), "pending" (chưa vote), "approved", \
hoặc null.
- "pr_lookup": người dùng hỏi về MỘT PR CỤ THỂ theo số (vd "PR 2261", "PR !2261 sao \
rồi"), không phân biệt của ai. filter = số PR đó dạng chuỗi (vd "2261").
- "item_lookup": người dùng hỏi về MỘT work item CỤ THỂ theo số (vd "work item #6753", \
"ticket 6753"), không phân biệt của ai. filter = số đó dạng chuỗi.
- "team_overview": người dùng hỏi về TÌNH TRẠNG CỦA CẢ TEAM/dự án, không riêng của họ \
(vd "PR nào bị treo lâu nhất", "PR nào của team đang chờ review lâu"). filter = null.
- "status": hỏi tình trạng hoạt động chung.
- "help": muốn xem danh sách lệnh.
- "unknown": MỌI yêu cầu khác — đặc biệt là bất kỳ yêu cầu THAY ĐỔI/SỬA/VOTE/MERGE/\
APPROVE/REJECT/COMMIT/PUSH nào — luôn phân vào "unknown", không tự bịa intent mới.

Tin nhắn: {text}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_ALLOWED_INTENTS = {
    "items", "prs", "pr_lookup", "item_lookup", "team_overview", "status", "help", "unknown",
}


def _fmt_exc(exc: Exception) -> str:
    """``str(asyncio.TimeoutError())`` (and several other stdlib exceptions) is "" —
    log the type name too, or a timeout is silently indistinguishable from any other
    failure in the logs."""
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def _classify_intent(config: Settings, text: str) -> dict:
    """Best-effort intent classification via a single, tool-less Claude call. Any
    failure (timeout, bad JSON, disallowed intent) safely falls back to "unknown"."""
    from ai_autopilot.execution.claude_client import run_claude

    try:
        run = await run_claude(
            _INTENT_PROMPT.format(text=text[:500]),
            tempfile.gettempdir(),  # no tool use below → cwd content is irrelevant,
                                     # just needs to exist (workspace_directory may not
                                     # on this host — see teams_agent module notes)
            timeout_seconds=45,  # a plain "text only" call still measured 8-11s under
                                  # light load — 20s left little headroom under real
                                  # concurrency, and asyncio.TimeoutError's str() is ""
                                  # (empty), which made timeouts indistinguishable from
                                  # other failures in the log until _fmt_exc below.
            model=config.claude_model or None,
            max_turns=1,
            allowed_tools=[],  # no tool use — pure text classification
        )
        m = _JSON_RE.search(run.text or "")
        if not m:
            return {"intent": "unknown", "filter": None}
        data = json.loads(m.group(0))
        intent = data.get("intent") if data.get("intent") in _ALLOWED_INTENTS else "unknown"
        return {"intent": intent, "filter": data.get("filter")}
    except Exception as exc:  # noqa: BLE001 — classification failure must not crash the turn
        _log.warning("intent classification failed", error=_fmt_exc(exc))
        return {"intent": "unknown", "filter": None}


_PHRASE_PROMPT = """Người dùng vừa hỏi một bot Teams CHỈ ĐỌC dữ liệu (không sửa code, \
không vote, không merge được qua chat):

"{question}"

Dưới đây là dữ liệu THẬT đã tra cứu sẵn — không được thêm, bớt, hay đổi bất kỳ mục \
nào, chỉ diễn đạt lại thành một câu trả lời NGẮN GỌN, TỰ NHIÊN bằng tiếng Việt (giữ \
markdown nhẹ như in đậm số PR/work item nếu hợp lý):

{bullets}

Nếu dữ liệu là "(không có ... nào khớp)", nói rõ ràng là không có gì phù hợp — đừng \
bịa thêm. KHÔNG đề xuất sửa code / vote / merge hay bất kỳ thao tác nào khác."""


async def _phrase_natural(config: Settings, question: str, bullets: str) -> str:
    """Ask Claude to reword already-fetched, already-filtered data into a natural
    reply. Tool-less — Claude only rewords what Python already looked up, it never
    fetches more data or picks an action. Falls back to the plain bullets on any
    failure so the user always gets an answer."""
    from ai_autopilot.execution.claude_client import run_claude

    try:
        run = await run_claude(
            _PHRASE_PROMPT.format(question=question[:300], bullets=bullets),
            tempfile.gettempdir(),  # no tool use below → cwd content is irrelevant,
                                     # just needs to exist (workspace_directory may not
                                     # on this host — see teams_agent module notes)
            timeout_seconds=45,  # see _classify_intent — same headroom reasoning
            model=config.claude_model or None,
            max_turns=1,
            allowed_tools=[],
        )
        return (run.text or "").strip() or bullets
    except Exception as exc:  # noqa: BLE001 — phrasing failure must not crash the turn
        _log.warning("reply phrasing failed — falling back to plain list", error=_fmt_exc(exc))
        return bullets


async def _handle_free_text(
    context, config: Settings, container: Container,
    reviewer_tracker: ReviewerTrackerService, text: str,
) -> None:
    if any(hint in text.lower() for hint in _MUTATION_HINTS):
        await context.send_activity(_REDIRECT_TO_ADO)
        return
    if not config.teams_agent_nlu_enabled:
        await context.send_activity(
            "Lệnh này chưa hỗ trợ — gõ `/help` để xem lệnh có sẵn."
        )
        return
    result = await _classify_intent(config, text)
    intent, flt = result["intent"], result.get("filter")
    if intent == "items":
        email, items = await _items_data(context, container)
        if email is None:
            await context.send_activity(
                "⚠️ Không xác định được email của bạn trong Teams — không thể lọc "
                "work item riêng."
            )
            return
        _, bullets = _format_items(items, flt if isinstance(flt, str) else None)
        await context.send_activity(await _phrase_natural(config, text, bullets))
    elif intent == "prs":
        email, prs = await _prs_data(context, reviewer_tracker)
        if email is None:
            await context.send_activity(
                "⚠️ Không xác định được email của bạn trong Teams — không thể lọc "
                "PR riêng."
            )
            return
        vf = flt if flt in ("blocked", "pending", "approved") else None
        _, bullets = _format_prs(prs, vf)
        await context.send_activity(await _phrase_natural(config, text, bullets))
    elif intent == "pr_lookup":
        pr_id = _as_int(flt)
        if pr_id is None:
            await context.send_activity("Không nhận diện được số PR — gõ rõ hơn nhé.")
            return
        detail = await reviewer_tracker.find_pr_by_id(pr_id)
        bullets = _format_pr_detail(detail) if detail else f"(không tìm thấy PR !{pr_id} đang active)"
        await context.send_activity(await _phrase_natural(config, text, bullets))
    elif intent == "item_lookup":
        wid = _as_int(flt)
        if wid is None:
            await context.send_activity("Không nhận diện được số work item — gõ rõ hơn nhé.")
            return
        item = await container.ado.get_work_item(wid)
        bullets = (
            f"#{item.id} [{item.work_item_type}] {item.title} — {item.state}, "
            f"assigned to {item.assigned_to or '(chưa gán)'}"
        ) if item else f"(không tìm thấy work item #{wid})"
        await context.send_activity(await _phrase_natural(config, text, bullets))
    elif intent == "team_overview":
        prs = await reviewer_tracker.team_overview()
        bullets = _format_team_overview(prs)
        await context.send_activity(await _phrase_natural(config, text, bullets))
    elif intent == "status":
        await context.send_activity(
            "📊 Đang hoạt động. Xem chi tiết trên dashboard `/dashboard/reviews`."
        )
    elif intent == "help":
        await context.send_activity(_HELP_TEXT)
    else:
        await context.send_activity(
            "Chưa hiểu ý bạn — gõ `/help` để xem lệnh, hoặc hỏi cụ thể hơn (vd \"PR "
            "nào của tôi đang bị block?\")."
        )
