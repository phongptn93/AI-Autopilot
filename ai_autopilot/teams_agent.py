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
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

from ai_autopilot.config import Settings
from ai_autopilot.container import Container
from ai_autopilot.data.entities import PipelineState
from ai_autopilot.logging_config import get_logger
from ai_autopilot.services import planning_analyzer
from ai_autopilot.services.reviewer_tracker import ReviewerTrackerService

_log = get_logger("teams_agent")
_MIN_DT = datetime.min  # sort fallback for items with no updated_at
_REVIEW_TASKS: set = set()  # keep background review tasks referenced (no GC)
_BG_TASKS: set = set()  # same, for deferred (slow) turn work

# Activity ids already handled, so a channel redelivery can't run the same message twice
# (and, worse, spawn a second Claude process for it). Bounded FIFO — this only needs to
# cover the retry window, not history.
_SEEN_ACTIVITIES: OrderedDict[str, None] = OrderedDict()
_SEEN_LIMIT = 512


async def cancel_background_work() -> None:
    """Cancel deferred replies / background PR reviews still in flight.

    Called from the app's teardown: these tasks are detached by design (that's what keeps
    the Teams turn fast), so without an explicit cancel the process exits with a
    "Task was destroyed but it is pending" warning for every reply still composing."""
    tasks = list(_BG_TASKS) + list(_REVIEW_TASKS)
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _already_handled(activity_id: str | None) -> bool:
    """True if this exact activity was already processed (channel redelivery)."""
    if not activity_id:
        return False  # no id to key on — treat as new rather than dropping the turn
    if activity_id in _SEEN_ACTIVITIES:
        return True
    _SEEN_ACTIVITIES[activity_id] = None
    while len(_SEEN_ACTIVITIES) > _SEEN_LIMIT:
        _SEEN_ACTIVITIES.popitem(last=False)
    return False


def _spawn_review(container: Container, repo_name: str, pr_id: int, pr_url: str = "") -> None:
    """Kick off the real skill-based PR review in the background — it runs for minutes
    and posts its findings on the PR itself, so the chat turn only acknowledges it."""
    audit = asyncio.create_task(container.audit_repo.record(
        actor="teams", source="teams", action="pr.review_requested",
        target=f"{repo_name} !{pr_id}",
    ))
    _REVIEW_TASKS.add(audit)
    audit.add_done_callback(_REVIEW_TASKS.discard)
    task = asyncio.create_task(container.executor.review_pr(repo_name, pr_id, pr_url))
    _REVIEW_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _REVIEW_TASKS.discard(t)
        exc = None if t.cancelled() else t.exception()
        if exc:
            _log.warning("background review failed", pr=pr_id, error=_fmt_exc(exc))

    task.add_done_callback(_done)


_REVIEWING_MSG = (
    "🔍 Đang review PR !{pr} trong `{repo}` — Claude phân tích diff so với codebase, "
    "findings sẽ được đăng thẳng lên PR trong ít phút."
)


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

_UNSET = object()  # "no cached email" — distinct from a cached None (lookup failed)


class _DeferredContext:
    """Stands in for the ``TurnContext`` after the turn has already been answered.

    WHY: the Teams messaging endpoint only returns HTTP 200 once the turn handler
    finishes (``app.py`` → ``start_agent_process`` → ``adapter.process`` awaits it), and
    the channel gives up on the request after ~15 seconds. Any Claude call made inline
    (the agentic turn budgets 120s, the tool-less ones 45s) therefore strands the user:
    no reply ever arrives, while the run keeps going invisibly. So the live turn acks in
    under a second and hands the slow work a context that looks the same to every reply
    function but delivers through the proactive API instead.

    Quacks like the two things those functions touch: ``send_activity`` (text OR an
    Adaptive Card attachment) and ``.activity``. ``resolved_email`` is filled in during
    the LIVE turn because the identity lookup needs a real ``TurnContext`` connector,
    which no longer exists out here — see ``_teams_email``."""

    def __init__(self, app, adapter, conversation, activity, email: str | None) -> None:
        self._app = app
        self._adapter = adapter
        self._conversation = conversation
        self.activity = activity
        self.resolved_email = email

    async def send_activity(self, message) -> None:
        from microsoft_agents.hosting.core import MessageFactory

        activity = MessageFactory.text(message) if isinstance(message, str) else message
        await self._app.proactive.send_activity(
            self._adapter, self._conversation, activity
        )


@dataclass
class _Deferral:
    """Ability to reply into THIS turn's conversation from a background task.

    ``sem`` caps how many of those background replies may hold a Claude process at once
    (``teams_agent_max_concurrent``); ``None`` = uncapped. It is acquired around the WORK
    only, never around the ack — a queued message must still get immediate feedback."""

    app: Any
    adapter: Any
    sem: asyncio.Semaphore | None = None

    def context_for(self, context, email: str | None) -> _DeferredContext:
        from microsoft_agents.hosting.core.app.proactive import Conversation

        return _DeferredContext(
            self.app, self.adapter, Conversation.from_turn_context(context),
            context.activity, email,
        )


def _typing_activity():
    from microsoft_agents.activity import Activity, ActivityTypes

    return Activity(type=ActivityTypes.typing)


async def _run_deferred(defer, context, ack: str, work) -> bool:
    """Ack on the live turn, then run ``work(deferred_context)`` detached.

    Returns False when deferral isn't available (unit tests calling these helpers
    directly, or the SDK absent) so the caller can just run ``work(context)`` inline —
    which is the pre-existing behaviour, kept so nothing depends on the fast path."""
    if defer is None:
        return False
    with contextlib.suppress(Exception):  # cosmetic, and the cheapest signal — send first
        await context.send_activity(_typing_activity())
    # Resolve identity while the real TurnContext is still alive (one quick connector
    # call); the background work can't do it afterwards.
    email = await _teams_email(context)
    deferred = defer.context_for(context, email)
    sem = defer.sem
    if ack:
        # Say so when the reply will have to queue behind other runs, rather than letting
        # it look like the bot went quiet again.
        queued = sem is not None and sem.locked()
        await context.send_activity(ack + (_QUEUED_SUFFIX if queued else ""))

    async def _guarded() -> None:
        try:
            if sem is None:
                await work(deferred)
            else:
                async with sem:  # cap concurrent Claude processes, not concurrent turns
                    await work(deferred)
        except asyncio.CancelledError:
            raise  # shutdown — cancel_background_work() is draining us
        except Exception as exc:  # noqa: BLE001 — a detached task must not die silently
            _log.error("deferred Teams work failed", error=_fmt_exc(exc))
            with contextlib.suppress(Exception):
                await deferred.send_activity(
                    "⚠️ Có lỗi khi xử lý — thử lại giúp mình nhé."
                )

    task = asyncio.create_task(_guarded())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return True


_THINKING_ACK = "⏳ Để mình tra cứu rồi trả lời ngay nhé…"
_CREATING_ACK = "⏳ Đang tạo ticket…"
_QUEUED_SUFFIX = " (đang có việc khác chạy trước nên hơi lâu một chút nhé)"

# Chat commands, each with its usage — the ONE place they're described. Used to build
# /help and, on a syntax slip, to answer with that command's usage instantly instead of
# dropping the message into the (slow) agent path.
_COMMANDS: dict[str, tuple[str, str]] = {
    "/items": ("/items", "work item của bạn (khớp theo email Teams ↔ ADO assignee)"),
    "/prs": ("/prs", "PR bạn là author hoặc reviewer, kèm tình trạng vote"),
    "/review": ("/review <repo> <pr-id>", "bot review lại PR đó ngay (chạy skill review đầy đủ)"),
    "/pr": ("/pr <repo> <pr-id>", "xem chi tiết BẤT KỲ PR nào (không chỉ của bạn)"),
    "/item": ("/item <id>", "xem chi tiết BẤT KỲ work item nào"),
    "/team": ("/team", "tổng quan PR của cả team, cũ nhất trước"),
    "/queue": ("/queue", "việc autopilot đang chờ người xử lý (needs human)"),
    "/resume": ("/resume <id>", "tiếp tục 1 việc đang chờ (có xác nhận)"),
    "/log": ("/log <mô tả>", "tạo nhanh 1 Requirement trong ADO (có xác nhận trước khi tạo)"),
    "/status": ("/status", "tình trạng hoạt động"),
    "/help": ("/help", "bảng lệnh này"),
}


def _help_text(config: Settings) -> str:
    """The /help table. Built at call time so the PR-comment command list comes from
    ``config`` (see ``Settings.comment_command_hint_markdown``) instead of a hardcoded
    copy that silently drifted from what this instance actually accepts."""
    lines = [f"- `{usage}` — {desc}" for usage, desc in _COMMANDS.values()]
    pr_hint = config.comment_command_hint_markdown
    text = (
        "🤖 **AI Autopilot**\n\n"
        + "\n".join(lines)
        + "\n- 🔗 Dán **link PR** + \"review\" → mình review ngay (dán link không kèm gì "
        "→ xem chi tiết)\n\n"
        "💬 Cũng có thể gõ tự nhiên: hỏi để tra cứu (*\"PR nào của tôi đang bị block?\"*) "
        "hoặc **tạo ticket** (*\"tạo ticket: đăng nhập lỗi khi SSO timeout\"* — bot hỏi xác "
        "nhận trước khi tạo). KHÔNG sửa/vote/merge được qua chat."
    )
    if pr_hint:
        # Same word, two scopes — worth spelling out: /review HERE runs the full review
        # skill, whereas /review as a PR reply is one of the advisory commands below.
        text += (
            "\n\n**Trên chính PR trong Azure DevOps** (reply vào PR, không phải ở đây):\n"
            + pr_hint
        )
    return text

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

    cap = config.teams_agent_max_concurrent
    deferral = _Deferral(
        app=app, adapter=adapter,
        sem=asyncio.Semaphore(cap) if cap > 0 else None,  # 0/negative = no cap
    )

    @app.activity(ActivityTypes.message)
    async def on_message(context: TurnContext, _state) -> None:
        try:
            # Remember every conversation we actually talk in, not just the one where the
            # bot was added (on_members_added below) — otherwise a DM the user started
            # themselves never receives the proactive digest.
            with contextlib.suppress(Exception):
                await app.proactive.store_conversation(context)
            await _handle_turn(
                context, config, container, reviewer_tracker, defer=deferral
            )
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
    # Read with the keys EXACTLY as stored. ``all_keys`` returns the SDK's own storage
    # keys, which are already prefixed ("proactive/conversations/<id>"); passing one to
    # ``proactive.get_conversation`` — which prefixes the id it's given — looked up
    # "proactive/conversations/proactive/conversations/<id>" and always missed, so every
    # digest quietly went to nobody while logging a healthy "sent". Going through storage
    # directly also avoids depending on that private prefix.
    from microsoft_agents.hosting.core.app.proactive import Conversation

    keys = await storage.all_keys()
    conversations = await storage.read(keys, target_cls=Conversation)
    sent, failed = 0, 0
    for key, conversation in conversations.items():
        if conversation is None:
            continue
        try:
            await app.proactive.send_activity(adapter, conversation, message_factory.text(text))
            sent += 1
        except Exception as exc:  # noqa: BLE001 — one bad conversation must not stop the rest
            failed += 1
            _log.warning(
                "digest send failed for one conversation", key=key, error=_fmt_exc(exc)
            )
    _log.info("Teams digest sent", sent=sent, failed=failed, total=len(keys))


async def _handle_turn(
    context, config: Settings, container: Container,
    reviewer_tracker: ReviewerTrackerService, *, defer: _Deferral | None = None,
) -> None:
    activity = context.activity
    if _already_handled(getattr(activity, "id", None)):
        _log.info("skipping redelivered Teams activity", activity_id=activity.id)
        return
    payload: dict[str, Any] | None = getattr(activity, "value", None)
    if payload:
        await _handle_action(context, container, reviewer_tracker, payload, defer=defer)
        return
    # A quoted reply keeps what was quoted OUT of activity.text's own words — pull it out
    # separately so "review" stays the instruction while the quote supplies the subject.
    quoted = _quoted_text(activity)
    await _handle_command(
        context, config, container, reviewer_tracker,
        _strip_quote(activity.text or ""), defer=defer, quoted=quoted,
    )


async def _handle_action(
    context, container: Container, reviewer_tracker: ReviewerTrackerService, payload: dict,
    *, defer: _Deferral | None = None,
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
        # Creating the item is quick, but the persona then composes the reply with a
        # Claude call — deferred so tapping Confirm feels instant instead of hanging out
        # the whole turn (and with it the channel's HTTP request).
        title = str(payload.get("title") or "")

        async def _work(ctx) -> None:
            await _create_logged_ticket(ctx, container, title)

        if not await _run_deferred(defer, context, _CREATING_ACK, _work):
            await _work(context)
        return
    if action == "log_cancel":
        await context.send_activity("Đã hủy — không tạo ticket.")
        return
    if action == "queue_resume":
        try:
            iid = int(payload.get("id"))
        except (TypeError, ValueError):
            await context.send_activity("Không nhận diện được item để resume.")
            return
        started = await _resume_held_item(container, iid)
        await context.send_activity(
            f"▶ Đã tiếp tục **#{iid}** — mình nhận việc lại rồi ạ."
            if started else f"⚠️ Không resume được #{iid} (không tìm thấy hoặc dry-run)."
        )
        return
    if action == "queue_cancel":
        await context.send_activity("Đã hủy — vẫn giữ ở queue.")
        return
    await context.send_activity("Không nhận diện được hành động trên card này.")


async def _held_items(container: Container) -> list:
    """Work items the autopilot escalated (needs human), newest first."""
    held = [s for s in await container.state_repo.all() if s.state == PipelineState.NEEDS_HUMAN]
    held.sort(key=lambda s: s.updated_at or _MIN_DT, reverse=True)
    return held


async def _reply_queue(context, container: Container) -> None:
    """List held items so a human can resume them (reply `/resume <id>`)."""
    held = await _held_items(container)
    if not held:
        await context.send_activity("✅ Không có việc nào đang chờ xử lý (queue trống).")
        return
    lines = [
        f"- **#{s.work_item_id}** {s.title or ''} — {s.detail or 'chờ người xử lý'}"
        for s in held[:20]
    ]
    await context.send_activity(
        f"🙋 **Việc đang chờ ({len(held)})** — reply `/resume <id>` để tiếp tục:\n"
        + "\n".join(lines)
    )


async def _resume_held_item(container: Container, item_id: int) -> int:
    """Clear the hold tag and hand the item back to the poller. Returns items started."""
    hold = container.config.escalation_tag
    if hold:
        with contextlib.suppress(Exception):
            await container.ado.remove_tag(item_id, hold)
    started = await planning_analyzer.start_items(container, [item_id])
    await container.state_repo.set(item_id, PipelineState.QUEUED)
    await container.audit_repo.record(
        actor="teams", source="teams", action="item.resumed", target=f"#{item_id}",
    )
    return started


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
    cfg = container.config
    org = cfg.ado_organization.rstrip("/")
    project = cfg.ado_project
    url = f"{org}/{project}/_workitems/edit/{wid}"
    trigger = (cfg.effective_trigger_tags or ["<trigger-tag>"])[0]
    await container.audit_repo.record(
        actor=email, source="teams", action="ticket.created",
        target=f"#{wid}", detail=title[:200],
    )
    facts = (
        f"- Đã tạo Requirement [#{wid}]({url})\n"
        f"- Tiêu đề: {title}\n"
        f"- Project: {project} · Trạng thái: mới (Backlog) · Tag: teams-logged\n"
        f"- Người yêu cầu: {email}\n"
        f"- Ticket đang ở Backlog nên autopilot CHƯA tự xử lý; để bắt đầu thì gắn tag "
        f"trigger `{trigger}` (hoặc chuyển sang trạng thái làm việc).\n"
        f"- Có thể hỏi lại người dùng xem cần bổ sung mô tả / tiêu chí nghiệm thu không."
    )
    voiced = await _compose_message(
        cfg,
        "Báo cho người dùng biết ticket họ nhờ đã được tạo, kèm bước tiếp theo và chủ "
        "động hỏi có cần bổ sung gì không.",
        facts,
    )
    await context.send_activity(
        voiced or f"✅ Đã tạo **Requirement [#{wid}]({url})** — {title} (Backlog). "
        f"Gắn tag `{trigger}` khi muốn mình bắt đầu."
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
                "type": "TextBlock", "weight": "Bolder", "wrap": True, "size": "Medium",
                "text": "📝 Xác nhận tạo Requirement mới?",
            },
            {"type": "TextBlock", "wrap": True, "text": title, "spacing": "Small"},
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Loại:", "value": "Requirement"},
                    {"title": "Trạng thái:", "value": "mới (Backlog) — chờ triage"},
                ],
            },
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


async def _send_resume_confirm_card(context, item_id: int, title: str) -> None:
    """Approve/resume confirmation card for a held item — same gated round-trip as /log."""
    from microsoft_agents.hosting.core import CardFactory, MessageFactory

    card = {
        "type": "AdaptiveCard", "version": "1.4",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {"type": "TextBlock", "weight": "Bolder", "wrap": True, "size": "Medium",
             "text": f"▶ Resume #{item_id}?"},
            {"type": "TextBlock", "wrap": True, "text": title or f"Work item #{item_id}",
             "spacing": "Small"},
            {"type": "TextBlock", "wrap": True, "isSubtle": True,
             "text": "Gỡ trạng thái giữ và giao lại cho autopilot xử lý tiếp."},
        ],
        "actions": [
            {"type": "Action.Submit", "title": "▶ Resume",
             "data": {"action": "queue_resume", "id": item_id}},
            {"type": "Action.Submit", "title": "Giữ nguyên", "data": {"action": "queue_cancel"}},
        ],
    }
    await context.send_activity(MessageFactory.attachment(CardFactory.adaptive_card(card)))


_REVIEW_RE = re.compile(r"^/review\s+(\S+)\s+(\d+)\s*$", re.IGNORECASE)
_LOG_RE = re.compile(r"^/log\s+(.+)$", re.IGNORECASE | re.DOTALL)
_RESUME_RE = re.compile(r"^/resume\s+(\d+)\s*$", re.IGNORECASE)
# A pasted Azure DevOps PR URL, e.g.
# https://dev.azure.com/org/Project/_git/Micro-Frontend/pullrequest/2470
_PR_URL_RE = re.compile(
    r"dev\.azure\.com/[^/\s]+/[^/\s]+/_git/([^/\s?#]+)/pullrequest/(\d+)", re.IGNORECASE
)
# Words that turn a pasted PR link into a "review it" request (vs just showing detail).
_REVIEW_INTENT = ("review", "duyệt", "rà soát", "soát", "kiểm tra", "check", "xem lại")
_PR_LOOKUP_RE = re.compile(r"^/pr\s+(\S+)\s+(\d+)\s*$", re.IGNORECASE)
_ITEM_LOOKUP_RE = re.compile(r"^/item\s+(\d+)\s*$", re.IGNORECASE)
_SLASH_WORD_RE = re.compile(r"^(/\w+)")
# A PR named by NUMBER rather than a full link — "Pull request 2488", "PR 2488", "!2488".
# Teams unfurls a pasted ADO link into a preview card titled "Pull request <n>: <title>",
# so when that card is what got quoted, the number is all that survives: no URL for
# _PR_URL_RE to match, and no repo name. The repo is then resolved by scanning repos for
# that id (ReviewerTrackerService.find_pr_by_id).
_PR_NUMBER_RE = re.compile(
    r"(?:pull\s*request|\bpr\b|!)\s*#?\s*(\d{2,7})\b", re.IGNORECASE
)
_BLOCKQUOTE_RE = re.compile(r"<blockquote[^>]*>(.*?)</blockquote>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _quoted_text(activity) -> str:
    """The content of the message the user replied to, or ``""``.

    Teams delivers a quoted reply with the original inside a ``<blockquote>`` in ``text``
    and/or as a ``messageReference`` attachment — neither of which survives into the bare
    ``activity.text`` the bot used to read. That mattered: replying "AI Autopilot review"
    while quoting a PR notification put the PR *only* in the quote, so the bot had to ask
    which PR was meant even though the user had plainly pointed at one."""
    import html as _html

    parts: list[str] = []
    for m in _BLOCKQUOTE_RE.finditer(activity.text or ""):
        parts.append(_html.unescape(_TAG_RE.sub(" ", m.group(1))))
    for att in (getattr(activity, "attachments", None) or []):
        content = getattr(att, "content", None)
        if isinstance(content, str):
            parts.append(_html.unescape(_TAG_RE.sub(" ", content)))
        elif isinstance(content, dict):
            # messageReference and card attachments both keep their human-readable bits
            # under a handful of well-known keys.
            for key in ("messagePreview", "text", "title", "subtitle", "body"):
                value = content.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(_html.unescape(_TAG_RE.sub(" ", value)))
    return "\n".join(p.strip() for p in parts if p.strip()).strip()


def _strip_quote(text: str) -> str:
    """The user's OWN words, with any quoted block removed — so "review" stays the
    instruction and the quote is passed separately as context."""
    return _TAG_RE.sub(" ", _BLOCKQUOTE_RE.sub(" ", text or "")).strip()


def _find_pr_reference(*texts: str) -> tuple[str | None, int, str] | None:
    """First PR referenced across ``texts``, as ``(repo_name | None, pr_id, url)``.

    A full link gives the repo directly; a bare number ("Pull request 2488") does not, and
    the caller resolves it. Searched in argument order so the user's own message wins over
    anything they quoted."""
    for text in texts:
        if not text:
            continue
        m = _PR_URL_RE.search(text)
        if m:
            return unquote(m.group(1)), int(m.group(2)), f"https://{m.group(0)}"
    for text in texts:
        if not text:
            continue
        m = _PR_NUMBER_RE.search(text)
        if m:
            return None, int(m.group(1)), ""
    return None


def _slash_help(text: str) -> str | None:
    """Reply for a message that LOOKS like a command but matched nothing, or ``None`` if
    it isn't slash-prefixed at all.

    Without this, ``/review 2470`` (right command, missing the repo) fell through to the
    natural-language path and spent a whole Claude run answering something else — the
    command appeared "broken". A known command gets its own usage line; an unknown one
    gets the closest match by prefix."""
    m = _SLASH_WORD_RE.match(text.strip())
    if not m:
        return None
    word = m.group(1).lower()
    known = _COMMANDS.get(word)
    if known:
        usage, desc = known
        return f"⚠️ Cú pháp: `{usage}` — {desc}.\nGõ `/help` để xem tất cả lệnh."
    near = [c for c in _COMMANDS if c.startswith(word[:3])] or None
    suggestion = f" Ý bạn là {', '.join(f'`{c}`' for c in near)}?" if near else ""
    return f"⚠️ Không có lệnh `{word}`.{suggestion} Gõ `/help` để xem lệnh có sẵn."


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
    *, defer: _Deferral | None = None, quoted: str = "",
) -> None:
    low = text.lower()
    # Dispatch the no-argument commands on the EXACT leading word, not a prefix:
    # ``startswith("/prs")`` also swallowed ``/prss``, so a typo silently ran a different
    # command and looked like the bot ignoring what was asked. An unrecognised word falls
    # through to _slash_help below, which answers with usage.
    word_match = _SLASH_WORD_RE.match(text.strip())
    word = word_match.group(1).lower() if word_match else ""
    if low in ("", "help") or word == "/help":
        await context.send_activity(_help_text(config))
        return
    if word == "/status":
        await context.send_activity(
            "📊 Đang hoạt động. Xem chi tiết trên dashboard `/dashboard/reviews`."
        )
        return
    if word == "/items":
        await _reply_items(context, container)
        return
    if word == "/prs":
        await _reply_prs(context, reviewer_tracker)
        return
    if word == "/team":
        prs = await reviewer_tracker.team_overview()
        bullets = _format_team_overview(prs)
        await context.send_activity(
            f"👥 **Team overview — PR active, cũ nhất trước** ({len(prs)})\n{bullets}"
        )
        return
    m = _RESUME_RE.match(text.strip())
    if m:
        iid = int(m.group(1))
        item = await container.ado.get_work_item(iid)
        await _send_resume_confirm_card(context, iid, item.title if item else "")
        return
    if word == "/queue":
        await _reply_queue(context, container)
        return
    m = _REVIEW_RE.match(text.strip())
    if m:
        repo_name, pr_id = m.group(1), int(m.group(2))
        _spawn_review(container, repo_name, pr_id)
        await context.send_activity(_REVIEWING_MSG.format(pr=pr_id, repo=repo_name))
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
    # Looks like a command but matched nothing above → answer with its usage NOW. Must
    # come before the Claude paths: a typo'd command is a typo, not a question, and
    # sending it to the agent costs a whole run to produce an off-target reply.
    usage = _slash_help(text)
    if usage is not None:
        await context.send_activity(usage)
        return

    # "review" in plain words, with the PR named by link OR by number — including a number
    # that only appears in a QUOTED message. This is the "AI Autopilot review" reply to a
    # PR notification: the user has plainly pointed at a PR, so asking them which one back
    # is the wrong answer. Deterministic and instant (no Claude), and identical in agentic
    # and classifier mode. Questions are excluded — "PR 2488 review chưa?" is a lookup.
    ref = _find_pr_reference(text, quoted)
    if ref and any(k in low for k in _REVIEW_INTENT) and not _QUESTION_RE.search(text):
        repo_name, pr_id, url = ref
        if repo_name is None:
            # Only a number survived (a quoted "Pull request 2488" preview carries no repo
            # and no URL) — find which repo it lives in.
            found = await reviewer_tracker.find_pr_by_id(pr_id)
            repo_name = (found or {}).get("repo") or None
        if repo_name is None:
            await context.send_activity(
                f"Mình không tìm thấy PR !{pr_id} đang active trong repo nào cả — "
                f"bạn gửi link PR hoặc `/review <repo> {pr_id}` giúp mình nhé."
            )
            return
        _spawn_review(container, repo_name, pr_id, url)
        await context.send_activity(_REVIEWING_MSG.format(pr=pr_id, repo=repo_name))
        return

    # Agentic mode: hand everything non-slash to a real Claude agent turn — it parses PR
    # links, looks up data with tools, and requests gated actions itself (no regex/keyword
    # routing). It is then the ONLY path: chaining the classifier + phrasing calls behind
    # it meant one message could spawn three sequential Claude processes (~210s worst
    # case), so a failure ends in a static hint instead.
    if config.teams_agentic_enabled:
        async def _agentic_work(ctx) -> None:
            if not await _agentic_turn(
                ctx, config, container, reviewer_tracker, text, quoted=quoted
            ):
                await ctx.send_activity(_FREEFORM_FALLBACK)

        if not await _run_deferred(defer, context, _THINKING_ACK, _agentic_work):
            await _agentic_work(context)
        return

    m = _PR_URL_RE.search(text)
    if m:
        # Classifier mode fast-path: a pasted PR link, no /review syntax needed.
        repo_name, pr_id = unquote(m.group(1)), int(m.group(2))
        if any(k in low for k in _REVIEW_INTENT):
            _spawn_review(container, repo_name, pr_id, f"https://{m.group(0)}")
            await context.send_activity(_REVIEWING_MSG.format(pr=pr_id, repo=repo_name))
            return
        repo_id = await _resolve_repo_id(container, repo_name)
        if repo_id is None:
            await context.send_activity(f"Không tìm thấy repo `{repo_name}`.")
            return
        detail = await reviewer_tracker.pr_detail(repo_id, pr_id)
        await context.send_activity(
            _format_pr_detail(detail) if detail
            else f"PR !{pr_id} không tìm thấy hoặc không còn active trong `{repo_name}`."
        )
        return
    await _handle_free_text(
        context, config, container, reviewer_tracker, text, defer=defer
    )


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
    then report "identity chưa xác định" rather than silently showing everyone's data.

    On a DEFERRED turn the answer was already resolved during the live turn and cached on
    the context: the lookup below needs a real ``TurnContext`` connector, which is gone by
    then, so without this the background work would lose the caller's identity and silently
    drop every personal (items / PRs) section."""
    cached = getattr(context, "resolved_email", _UNSET)
    if cached is not _UNSET:
        return cached
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
#   1. A deterministic pre-filter refuses anything that reads like an INSTRUCTION to
#      mutate BEFORE any Claude call — cheap, and doesn't depend on the model behaving.
#   2. Even past that filter, the classifier's OUTPUT SCHEMA only has four intents —
#      items / prs / status / help — and _dispatch_intent below has no branch that
#      calls anything but the existing read-only reply functions. There is no code
#      path from free text to /ai, cast_pull_request_vote, or any write — the model's
#      only power is picking among reply functions that were already safe to call.
#
# Layer 1 matches on WORD BOUNDARIES and only fires on an INSTRUCTION, not a question.
# As plain substrings these hints misfired badly: "fix" also hit "prefix" and "đã fix",
# "delete"/"approve"/"push" hit their own past tenses — so ordinary read-only questions
# ("PR nào cần fix?", "ai đã push lên branch này?") were refused with a lecture about
# Azure DevOps instead of being answered. Meanwhile " merge " needed literal surrounding
# spaces, so it missed "merge hộ PR 2470" at the start of a message.
_MUTATION_WORDS = (
    "sửa", "chỉnh", "fix", "commit", "push", "merge", "approve", "reject",
    "revert", "delete", "xoá", "xóa", "vote",
)
_MUTATION_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(w) for w in _MUTATION_WORDS) + r")(?!\w)",
    re.IGNORECASE,
)
# Asking ABOUT a change is a lookup, not a request to make one: "PR nào đang chờ merge?"
# must be answered while "merge hộ PR 2470" is refused. A mutation phrased as a question
# ("sửa chỗ nào sai?") therefore reaches the classifier — which is fine, because layer 2
# has no mutating intent to reach: this filter is defence in depth, not the wall.
_QUESTION_RE = re.compile(
    r"\?|(?<!\w)(nào|ai|sao|đâu|bao nhiêu|thế nào|chưa|gì|liệt kê|list)(?!\w)",
    re.IGNORECASE,
)


def _is_mutation_request(text: str) -> bool:
    """True for an instruction to change something (refuse it), False for a question
    about one (answer it)."""
    return bool(_MUTATION_RE.search(text)) and not _QUESTION_RE.search(text)

_REDIRECT_TO_ADO = (
    "Việc sửa code / vote / merge chỉ thực hiện được khi reply trực tiếp trên PR "
    "trong Azure DevOps — không hỗ trợ qua chat Teams. Gõ `/help` để xem lệnh đọc "
    "hỗ trợ ở đây."
)

_INTENT_PROMPT = """Bạn là bộ phân loại Ý ĐỊNH cho một bot Teams chủ yếu CHỈ ĐỌC. \
Ngoại lệ GHI DUY NHẤT được phép là TẠO một ticket/Requirement mới trong ADO (và luôn \
có bước xác nhận trước khi tạo). Bot KHÔNG được sửa code, vote, merge hay thay đổi gì khác.

Phân loại tin nhắn của người dùng vào ĐÚNG MỘT trong các intent sau, và trả về \
CHÍNH XÁC một dòng JSON, không kèm giải thích, không markdown:
{{"intent": "items|prs|pr_lookup|item_lookup|team_overview|status|help|create_ticket|queue|resume|unknown", \
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
- "create_ticket": người dùng muốn TẠO / GHI / LOG một ticket, task, bug hay yêu cầu MỚI \
(vd "tạo ticket ...", "log giúp bug ...", "mở 1 task cho ...", "ghi nhận việc ..."). \
filter = TIÊU ĐỀ ngắn gọn (một câu) tóm tắt ticket, rút từ lời người dùng.
- "queue": người dùng hỏi việc đang CHỜ NGƯỜI XỬ LÝ / bị giữ / cần duyệt (vd "có việc nào \
đang chờ không", "queue", "còn gì cần tôi xử lý"). filter = null.
- "resume": người dùng muốn TIẾP TỤC / CHẠY LẠI / MỞ LẠI một việc đang chờ theo số (vd \
"tiếp tục #6753", "resume 6753", "chạy lại việc 6753"). filter = số work item đó dạng chuỗi.
- "unknown": MỌI yêu cầu khác — đặc biệt là bất kỳ yêu cầu SỬA CODE/VOTE/MERGE/\
APPROVE/REJECT/COMMIT/PUSH nào — luôn phân vào "unknown", không tự bịa intent mới.

Tin nhắn: {text}"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_ALLOWED_INTENTS = {
    "items", "prs", "pr_lookup", "item_lookup", "team_overview", "status", "help",
    "create_ticket", "queue", "resume", "unknown",
}


def _fmt_exc(exc: Exception) -> str:
    """``str(asyncio.TimeoutError())`` / ``str(asyncio.CancelledError())`` (and
    several other stdlib exceptions) is "" — always include the type name, and any
    of the SDK's own diagnostic attributes (exit_code/stderr/line), or a bare
    cancellation is silently indistinguishable from a timeout or anything else."""
    parts = [type(exc).__name__]
    text = str(exc)
    if text:
        parts.append(text)
    for attr in ("exit_code", "stderr", "line"):
        value = getattr(exc, attr, None)
        if value:
            parts.append(f"{attr}={value!r}")
    return " | ".join(parts)


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
            effort=config.claude_effort_chat,
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
            effort=config.claude_effort_chat,
        )
        return (run.text or "").strip() or bullets
    except Exception as exc:  # noqa: BLE001 — phrasing failure must not crash the turn
        _log.warning("reply phrasing failed — falling back to plain list", error=_fmt_exc(exc))
        return bullets


# Fallback shown only when the intelligent answer can't be produced (Claude down /
# timeout) — never crash the turn, always leave the user something actionable.
_FREEFORM_FALLBACK = (
    "Mình chưa trả lời được câu này ngay lúc này. Bạn thử hỏi cụ thể hơn (vd "
    "*\"PR nào của tôi đang bị block?\"*, *\"work item nào còn active?\"*) hoặc gõ "
    "`/help` để xem các lệnh."
)

def _persona_preamble(config: Settings) -> str:
    """Voice/identity block prepended to every message the persona composes."""
    name = config.bot_persona_name or "trợ lý"
    return (
        f'Bạn là "{name}" — {config.bot_persona_voice}\n'
        "Chỉ dùng đúng thông tin được cung cấp, TUYỆT ĐỐI không bịa. Bạn CHỈ ĐỌC dữ liệu "
        "(không sửa code / vote / merge); nếu người dùng muốn vậy, chỉ họ thao tác trên "
        "Azure DevOps."
    )


async def _compose_message(config: Settings, task: str, facts: str) -> str:
    """Have the persona (Claude, tool-less) write ONE short voiced message from FACTS.

    Returns "" on any failure so the caller can fall back to a plain line — the bot
    must never go silent just because the stylistic compose step failed."""
    from ai_autopilot.execution.claude_client import run_claude

    prompt = (
        _persona_preamble(config)
        + f"\n\nTình huống: {task}\n\nDữ liệu thật:\n{facts}\n\n"
        "Viết ĐÚNG MỘT tin nhắn Teams ngắn gọn, tự nhiên bằng tiếng Việt theo đúng giọng "
        "trên (giữ nguyên số hiệu / link dạng markdown nếu có trong dữ liệu)."
    )
    try:
        run = await run_claude(
            prompt, tempfile.gettempdir(), timeout_seconds=45,
            model=config.claude_model or None, max_turns=1, allowed_tools=[],
            effort=config.claude_effort_chat,
        )
        return (run.text or "").strip()
    except Exception as exc:  # noqa: BLE001 — styling failure must not lose the reply
        _log.warning("persona compose failed", error=_fmt_exc(exc))
        return ""


_FREEFORM_PROMPT = """{persona}

Người dùng hỏi: "{question}"

Dưới đây là DỮ LIỆU THẬT đã tra cứu sẵn cho người dùng này (chỉ dùng đúng những gì có ở \
đây — TUYỆT ĐỐI không bịa thêm work item / PR / con số nào không xuất hiện bên dưới):

{snapshot}

Các lệnh có sẵn để bạn gợi ý khi hợp lý:
{help}

Hãy trả lời NGẮN GỌN, TỰ NHIÊN bằng tiếng Việt, bám sát dữ liệu trên (giữ markdown nhẹ như \
in đậm số PR/work item). Nếu dữ liệu không đủ để trả lời, nói thẳng là chưa có thông tin và \
gợi ý lệnh phù hợp — đừng suy diễn. Nếu người dùng muốn sửa/vote/merge, nhắc họ thao tác \
trực tiếp trên PR trong Azure DevOps. KHÔNG đề xuất bạn sẽ tự thực hiện bất kỳ thay đổi nào."""


async def _answer_freeform(
    context, config: Settings, container: Container,
    reviewer_tracker: ReviewerTrackerService, question: str,
) -> str:
    """Answer an off-intent free-text question with a Claude call that reasons over a
    READ-ONLY snapshot Python fetched up front. Tool-less (``allowed_tools=[]``) so it
    can never mutate; it only phrases/reasons over data already looked up. Any failure
    falls back to a helpful hint so the turn never crashes."""
    from ai_autopilot.execution.claude_client import run_claude

    sections: list[str] = []
    # Each lookup is best-effort and independent: one failing source must not sink
    # the whole answer. Personal sections are skipped when the caller's email is
    # unknown (DM identity not resolvable), leaving the team overview.
    try:
        email, items = await _items_data(context, container)
        if email is not None:
            sections.append("Work item của bạn:\n" + _format_items(items)[1])
    except Exception as exc:  # noqa: BLE001
        _log.warning("freeform: items lookup failed", error=_fmt_exc(exc))
    try:
        email, prs = await _prs_data(context, reviewer_tracker)
        if email is not None:
            sections.append("PR của bạn:\n" + _format_prs(prs)[1])
    except Exception as exc:  # noqa: BLE001
        _log.warning("freeform: prs lookup failed", error=_fmt_exc(exc))
    try:
        sections.append(
            "Tổng quan PR của team (cũ nhất trước):\n"
            + _format_team_overview(await reviewer_tracker.team_overview())
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("freeform: team overview failed", error=_fmt_exc(exc))

    snapshot = "\n\n".join(sections) if sections else "(chưa tra cứu được dữ liệu nào)"

    try:
        run = await run_claude(
            _FREEFORM_PROMPT.format(
                persona=_persona_preamble(config),
                question=question[:300], snapshot=snapshot, help=_help_text(config),
            ),
            tempfile.gettempdir(),  # tool-less → cwd irrelevant, just must exist
            timeout_seconds=45,     # see _classify_intent — same headroom reasoning
            model=config.claude_model or None,
            max_turns=1,
            allowed_tools=[],       # no tool use — cannot mutate anything
            effort=config.claude_effort_chat,
        )
        return (run.text or "").strip() or _FREEFORM_FALLBACK
    except Exception as exc:  # noqa: BLE001 — answer failure must not crash the turn
        _log.warning("freeform answer failed — falling back to hint", error=_fmt_exc(exc))
        return _FREEFORM_FALLBACK


# ── Agentic free-text (opt-in): a REAL Claude agent turn with tools ──────────
#
# Instead of a fixed intent classifier + keyword guards, the message is handed to
# Claude with actual (read-only) tools: it looks up live ADO data itself, decides
# what the user means, and answers in the persona voice. SAFETY moves from keyword
# guessing to a hard TOOL ALLOWLIST — write tools (vote, merge, update, create,
# delete…) are simply not available to the agent, so no phrasing can reach them.
# Consequential actions (review / create ticket / resume) are requested via a final
# ACTION line, which Python routes through the SAME gated paths as the slash
# commands (background review; confirm cards for writes).
_AGENT_ADO_READ_TOOLS = (
    "repo_get_pull_request_by_id", "repo_get_pull_request_changes",
    "repo_list_pull_requests_by_repo_or_project", "repo_list_pull_request_threads",
    "repo_list_repos_by_project", "repo_get_repo_by_name_or_id",
    "wit_get_work_item", "wit_get_work_items_batch_by_ids", "wit_my_work_items",
    "wit_list_work_item_comments", "search_code", "search_workitem",
)
_AGENT_ACTION_RE = re.compile(r"^ACTION:\s*(\{.*\})\s*$", re.MULTILINE)
# Session memory is stored in the shared per-branch table under this pseudo-repo, so no
# schema change is needed; the "branch" is the Teams conversation.
_SESSION_REPO_KEY = "teams"


def _conversation_key(activity) -> str:
    """Stable key for this conversation's Claude session.

    Teams' ``conversation.id`` already encodes the THREAD inside a channel
    (``19:…@thread.tacv2;messageid=…``), so keying on it gives per-thread memory for free
    and keeps separate threads from bleeding into one another. Hashed when it would not fit
    the ``String(200)`` column — SQLite silently accepts an overlong value but SQL Server
    and Postgres reject it, which would surface as a write error mid-reply."""
    conv = getattr(activity, "conversation", None)
    cid = str(getattr(conv, "id", "") or "")
    if not cid:
        return ""
    if len(cid) <= 200:
        return cid
    import hashlib

    return "sha256:" + hashlib.sha256(cid.encode("utf-8")).hexdigest()


async def _load_session(config: Settings, container: Container, activity) -> tuple[str, str | None]:
    """``(conversation_key, session_id_to_resume)`` — best-effort, never raises."""
    if not config.teams_agent_session_memory:
        return "", None
    key = _conversation_key(activity)
    repo = getattr(container, "claude_session_repo", None)
    if not key or repo is None:
        return key, None
    try:
        return key, await repo.get(
            _SESSION_REPO_KEY, key, config.claude_session_ttl_hours
        )
    except Exception as exc:  # noqa: BLE001 — memory is a nicety, never block the reply
        _log.warning("teams session lookup failed", error=_fmt_exc(exc))
        return key, None


async def _save_session(container: Container, key: str, session_id: str | None) -> None:
    """Remember this turn's session so the next message in the thread continues it."""
    repo = getattr(container, "claude_session_repo", None)
    if not (key and session_id and repo is not None):
        return
    with contextlib.suppress(Exception):
        await repo.save(_SESSION_REPO_KEY, key, session_id)


def _agent_allowed_tools(mcp_servers: dict | None) -> list[str]:
    """The agentic turn's hard tool allowlist: read-only builtins + read-only ADO
    MCP tools on every configured server. Nothing here can mutate ADO or code."""
    tools = ["Read", "Grep", "Glob"]
    for server in (mcp_servers or {}):
        tools += [f"mcp__{server}__{t}" for t in _AGENT_ADO_READ_TOOLS]
    return tools


_AGENTIC_PROMPT = """{persona}

Bạn đang trả lời một tin nhắn Teams. Người gửi: {email}.
Tin nhắn: "{text}"
{quoted}{continuing}
Bạn CÓ các tool CHỈ ĐỌC (Azure DevOps MCP: PR, work item, search; đọc file trong
workspace). Hãy TỰ tra cứu dữ liệu thật cần thiết rồi trả lời NGẮN GỌN, tự nhiên
bằng tiếng Việt theo đúng giọng trên. Không bịa — chỉ nói điều tra cứu được.

Bạn KHÔNG có tool ghi. Nếu người dùng muốn một HÀNH ĐỘNG, kết thúc câu trả lời
bằng ĐÚNG MỘT dòng (Python sẽ thực thi qua luồng có kiểm soát):
ACTION: {{"action": "review_pr", "repo": "<tên repo>", "pr_id": <số>, "pr_url": "<url nếu có>"}}
ACTION: {{"action": "create_ticket", "title": "<tiêu đề ngắn>"}}
ACTION: {{"action": "resume", "id": <số work item>}}
- review_pr: người dùng muốn review/đánh giá code một PR (kèm lời xác nhận bạn sẽ review).
- create_ticket: muốn tạo ticket/task/bug mới (sẽ có card xác nhận trước khi tạo).
- resume: muốn tiếp tục một việc autopilot đang giữ (sẽ có card xác nhận).
Yêu cầu sửa code / vote / merge / xoá: KHÔNG có action — giải thích rằng việc đó
thao tác trực tiếp trên PR trong Azure DevOps. Không có hành động nào → không có dòng ACTION."""


async def _agentic_turn(
    context, config: Settings, container: Container,
    reviewer_tracker: ReviewerTrackerService, text: str, *, quoted: str = "",
) -> bool:
    """One genuine agent turn over the message. Returns True when it handled the
    reply; False on any failure so the caller falls back to the classifier path."""
    from ai_autopilot.execution.claude_client import run_claude
    from ai_autopilot.execution.claude_executor import _load_mcp_servers

    # Runs in the MAIN workspace on purpose — no git worktree, unlike task execution and
    # review_pr (which get one via _acquire_agent_scratch). Chat is overwhelmingly Q&A, so
    # isolation would buy nothing and cost 200-500ms plus disk on every message, on the one
    # path where latency is felt directly. Safety here is the read-only tool allowlist below
    # (no Write/Edit/Bash, no ADO write tools), not the filesystem.
    #
    # Consequence worth knowing: answers reflect the checkout AS IT IS on the machine
    # running autopilot, uncommitted changes included — not a clean base branch.
    workspace = config.workspace_directory
    mcp = _load_mcp_servers(workspace) if workspace else None
    email = await _teams_email(context) or "(không rõ)"
    # Continue this thread's session so the reply knows what was already said. run_claude
    # falls back to a fresh session by itself if the stored id can't be resumed.
    conv_key, resume = await _load_session(config, container, context.activity)
    try:
        run = await run_claude(
            _AGENTIC_PROMPT.format(
                persona=_persona_preamble(config), email=email, text=text[:600],
                # The user replied TO something; that quote is usually where the subject
                # (a PR, a work item) actually is.
                quoted=(
                    f'\nHọ đang reply/quote tin nhắn này — chủ thể câu hỏi thường nằm ở '
                    f'đây:\n"""{quoted[:800]}"""\n' if quoted else ""
                ),
                # Resuming a session: without saying so, the agent re-introduces itself and
                # re-asks for context it already has earlier in this same thread.
                continuing=(
                    "\nĐây là tin nhắn TIẾP THEO trong cùng một cuộc hội thoại — bạn đã "
                    "nói chuyện với họ ở các tin trước. Dùng lại ngữ cảnh đó (PR/work item "
                    "đang bàn, việc đã tra cứu); KHÔNG chào lại từ đầu, KHÔNG hỏi lại điều "
                    "họ đã nói rồi.\n" if resume else ""
                ),
            ),
            workspace or tempfile.gettempdir(),
            timeout_seconds=120,
            model=config.claude_model or None,
            max_turns=12,                                  # room for tool lookups
            allowed_tools=_agent_allowed_tools(mcp),       # read-only, hard limit
            mcp_servers=mcp,
            resume=resume,
            effort=config.claude_effort_agentic,
        )
    except Exception as exc:  # noqa: BLE001 — agent failure must not lose the turn
        _log.warning("agentic turn failed — falling back to classifier", error=_fmt_exc(exc))
        return False

    # Saved even when the reply is empty below: the session exists and its context is worth
    # continuing, whatever this particular turn produced.
    await _save_session(container, conv_key, run.session_id)

    reply = (run.text or "").strip()
    if not reply:
        return False

    action = None
    m = _AGENT_ACTION_RE.search(reply)
    if m:
        reply = reply[: m.start()].strip()
        with contextlib.suppress(ValueError):
            action = json.loads(m.group(1))
    if reply:
        await context.send_activity(reply)

    if isinstance(action, dict):
        kind = str(action.get("action") or "")
        if kind == "review_pr" and action.get("repo") and _as_int(action.get("pr_id")):
            pr_id = _as_int(action.get("pr_id"))
            _spawn_review(container, str(action["repo"]), pr_id, str(action.get("pr_url") or ""))
            if not reply:  # the agent's own confirmation usually covers this
                await context.send_activity(
                    _REVIEWING_MSG.format(pr=pr_id, repo=action["repo"])
                )
        elif kind == "create_ticket" and str(action.get("title") or "").strip():
            await _send_log_confirm_card(context, str(action["title"]).strip()[:250])
        elif kind == "resume" and _as_int(action.get("id")) is not None:
            iid = _as_int(action.get("id"))
            item = await container.ado.get_work_item(iid)
            await _send_resume_confirm_card(context, iid, item.title if item else "")
    return True


async def _handle_free_text(
    context, config: Settings, container: Container,
    reviewer_tracker: ReviewerTrackerService, text: str,
    *, defer: _Deferral | None = None,
) -> None:
    """Classifier path (agentic mode off). The two guards below are instant, so they
    answer on the live turn; everything past them needs Claude and is deferred."""
    if _is_mutation_request(text):
        await context.send_activity(_REDIRECT_TO_ADO)
        return
    if not config.teams_agent_nlu_enabled:
        await context.send_activity(
            "Lệnh này chưa hỗ trợ — gõ `/help` để xem lệnh có sẵn."
        )
        return

    async def _work(ctx) -> None:
        await _classify_and_reply(ctx, config, container, reviewer_tracker, text)

    if not await _run_deferred(defer, context, _THINKING_ACK, _work):
        await _work(context)


async def _classify_and_reply(
    context, config: Settings, container: Container,
    reviewer_tracker: ReviewerTrackerService, text: str,
) -> None:
    """Classify the message, then answer it — every branch here costs at least one
    Claude call, so callers run it off the turn (see ``_handle_free_text``)."""
    result = await _classify_intent(config, text)
    intent, flt = result["intent"], result.get("filter")
    if intent == "create_ticket":
        # The ONE write intent — natural language "tạo ticket ..." reuses the exact
        # same confirm-then-create card as /log, so a ticket is never created without
        # an explicit Confirm click.
        title = (flt if isinstance(flt, str) else "").strip() or text.strip()
        await _send_log_confirm_card(context, title[:250])
        return
    if intent == "queue":
        await _reply_queue(context, container)
        return
    if intent == "resume":
        iid = _as_int(flt)
        if iid is None:
            await context.send_activity("Bạn muốn tiếp tục việc nào? Cho mình số work item nhé.")
            return
        item = await container.ado.get_work_item(iid)
        await _send_resume_confirm_card(context, iid, item.title if item else "")
        return
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
        await context.send_activity(_help_text(config))
    else:
        # Off-intent free text: instead of a canned "didn't understand" reply, let
        # Claude reason over a read-only snapshot and answer naturally (tool-less —
        # still cannot mutate). Mutation-style requests were already redirected above.
        await context.send_activity(
            await _answer_freeform(context, config, container, reviewer_tracker, text)
        )
