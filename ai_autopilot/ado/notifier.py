"""Update ADO work items and broadcast results to notification channels.

Ported from ``AdoNotifier``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ai_autopilot.ado.client import AdoClient
from ai_autopilot.config import Settings
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import ExecutionResult, WorkItemInfo
from ai_autopilot.notifications.base import (
    NotificationChannel,
    NotificationMessage,
    NotificationType,
)
from ai_autopilot.scheduling import QuietHours, render_held_summary


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class AdoNotifier:
    def __init__(
        self, ado: AdoClient, config: Settings, channels: list[NotificationChannel],
        hold_repo: object | None = None,
    ) -> None:
        self._ado = ado
        self._config = config
        self._channels = channels
        self._log = get_logger("ado.notifier")
        self._quiet = QuietHours(config)
        self._hold_repo = hold_repo
        # Set at startup so the FIRST in-hours broadcast checks the queue: a restart
        # loses the in-memory flag, and notices held before it must still get out.
        self._maybe_held = True

    async def notify_started(
        self, item: WorkItemInfo, skill: str, *, post_comment: bool = True
    ) -> None:
        """Announce that work has begun: an ADO comment plus the notification channels.

        ``post_comment=False`` is for callers that already wrote their own richer comment on
        the item (the interactive path names the Remote-Control session). They still need the
        broadcast — that path used to skip this method entirely, so Teams got a "completed"
        card with no "started" card before it.
        """
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        comment = (
            "<div><b>▶️ Đã nhận việc — bắt đầu xử lý tự động</b><br/><ul>"
            f"<li><b>Skill:</b> <code>{skill}</code></li>"
            f"<li><b>Category:</b> {item.category}</li>"
            f"<li><b>Started:</b> {now} UTC</li>"
            "</ul></div>"
        )
        if self._config.dry_run:
            self._log.info("[DRY-RUN] would comment: started", id=item.id)
            return
        if post_comment:
            await self._ado.add_comment(item.id, comment)
        # NOTE: ADO ``System.State`` transitions are driven by the poller's pipeline
        # stages (see AdoPollerService._apply_ado_state), so they apply uniformly
        # across interactive / assisted / unattended — not just here.
        await self._broadcast(
            NotificationMessage(work_item=item, type=NotificationType.STARTED, skill=skill)
        )

    async def notify_completed(self, item: WorkItemInfo, result: ExecutionResult) -> None:
        if result.success:
            files_html = ""
            if result.files_changed:
                shown = "".join(
                    f"<li><code>{f}</code></li>" for f in result.files_changed[:20]
                )
                more = (
                    f"<li>...and {len(result.files_changed) - 20} more</li>"
                    if len(result.files_changed) > 20
                    else ""
                )
                files_html = f"<li><b>Files changed:</b><ul>{shown}{more}</ul></li>"
            prs = result.pr_urls or ([result.pr_url] if result.pr_url else [])
            if prs:
                label = "PR" if len(prs) == 1 else f"PRs ({len(prs)})"
                links = "".join(f'<li><a href="{u}">{u}</a></li>' for u in prs)
                pr_html = f"<li><b>{label}:</b><ul>{links}</ul></li>"
            else:
                pr_html = ""
            comment = (
                "<div><b>✅ Hoàn tất</b><br/><ul>"
                f"<li><b>Skill:</b> <code>{result.skill_used}</code></li>"
                f"<li><b>Duration:</b> {_mmss(result.duration_seconds)}</li>"
                f"<li><b>Branch:</b> <code>{result.branch_name}</code></li>"
                f"{files_html}{pr_html}</ul></div>"
            )
        else:
            comment = (
                "<div><b>❌ Chưa hoàn tất</b><br/><ul>"
                f"<li><b>Skill:</b> <code>{result.skill_used}</code></li>"
                f"<li><b>Duration:</b> {_mmss(result.duration_seconds)}</li>"
                f"<li><b>Error:</b> {result.error}</li>"
                "</ul></div>"
            )

        if self._config.dry_run:
            status = "Completed" if result.success else "Failed"
            self._log.info("[DRY-RUN] would comment", id=item.id, status=status)
            return

        await self._ado.add_comment(item.id, comment)
        # ADO tags + System.State are owned by the poller's outcome policy
        # (AdoPollerService._apply_outcome), not here — one source of truth.

        await self._broadcast(
            NotificationMessage(
                work_item=item, type=NotificationType.COMPLETED, result=result
            )
        )

    async def notify_error(self, item: WorkItemInfo, error: str) -> None:
        comment = f"<div><b>⚠️ Gặp lỗi khi xử lý</b><br/><p>{error}</p></div>"
        if self._config.dry_run:
            self._log.info("[DRY-RUN] would comment: error", id=item.id, error=error)
            return
        await self._ado.add_comment(item.id, comment)
        await self._broadcast(
            NotificationMessage(work_item=item, type=NotificationType.ERROR, error=error)
        )

    async def broadcast_digest(self, heading: str, text: str) -> None:
        """Send a free-form notice (no work item) to every enabled channel.

        Routed through the same ``_broadcast`` as everything else so a digest inherits
        the per-channel error isolation rather than growing a second, subtly different
        fan-out loop."""
        await self._broadcast(NotificationMessage(
            work_item=WorkItemInfo(id=0), type=NotificationType.INFO,
            heading=heading, text=text,
        ))

    async def notify(self, message: NotificationMessage) -> None:
        """Send a pre-built notice through the alert policy and the quiet window.

        For callers that assemble their own message (scheduled loops, the reviewer
        nudge, the budget alarm) and used to iterate ``channels`` themselves — which
        meant every alert setting silently did not apply to them.
        """
        await self._broadcast(message)

    async def _broadcast(self, message: NotificationMessage) -> None:
        """Send to every enabled channel — unless we are outside notification hours.

        This is the ONE place every notice passes through, which is why the quiet window
        is enforced here rather than at each caller: a rule applied at seven call sites
        is a rule that will be missed at the eighth.

        ADO comments are deliberately NOT gated (they are written by the callers above,
        before this): a comment is the record on the work item, not an interruption, and
        a record with holes in it overnight would be worse than a late ping.

        Order matters. The alert POLICY (is this kind of notice wanted at all?) is
        applied before the quiet window, so a notice the team switched off is dropped
        outright rather than queued and then delivered in the morning summary — being
        held is for things you still want, just not now.
        """
        if not self._config.wants_alert(message.event, int(message.severity)):
            self._log.debug(
                "notification suppressed by alert policy",
                event=message.event, severity=message.severity.name,
            )
            return
        if await self._hold_if_quiet(message):
            return
        await self._flush_held()
        await self._send_now(message)

    async def _send_now(self, message: NotificationMessage) -> None:
        for channel in self._channels:
            if not channel.is_enabled:
                continue
            try:
                await channel.send(message)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("notification failed", channel=channel.name, error=str(exc))

    async def _hold_if_quiet(self, message: NotificationMessage) -> bool:
        """Queue the notice if it is outside hours. Returns True when it was held."""
        if self._hold_repo is None or not self._quiet.is_quiet():
            return False
        try:
            dropped = await self._hold_repo.hold(
                kind=message.type.value, title=message.title, body=message.summary,
                work_item_id=getattr(message.work_item, "id", 0) or 0,
                cap=self._config.notify_quiet_max_held,
            )
        except Exception as exc:  # noqa: BLE001 — a queue failure must not lose the notice
            self._log.warning("could not hold notification — sending", error=str(exc))
            return False
        self._maybe_held = True
        self._log.info(
            "notification held — outside notify hours",
            title=message.title, dropped=dropped,
        )
        return True

    async def flush_quiet(self) -> int:
        """Deliver anything held, if the window is open now. Returns how many were held.

        Called on the poller's tick as well as before every live broadcast: without a
        tick, a quiet night with nothing happening in the morning would keep yesterday's
        notices queued until the next event, which may be hours away.
        """
        return await self._flush_held()

    async def _flush_held(self) -> int:
        if self._hold_repo is None or not self._maybe_held or self._quiet.is_quiet():
            return 0
        try:
            held = await self._hold_repo.drain()
        except Exception as exc:  # noqa: BLE001
            self._log.warning("could not read held notifications", error=str(exc))
            return 0
        self._maybe_held = False
        if not held:
            return 0
        heading, body = render_held_summary(held)
        self._log.info("delivering held notifications", count=len(held))
        await self._send_now(NotificationMessage(
            work_item=WorkItemInfo(id=0), type=NotificationType.INFO,
            heading=heading, text=body,
        ))
        return len(held)
