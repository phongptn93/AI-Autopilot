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


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class AdoNotifier:
    def __init__(
        self, ado: AdoClient, config: Settings, channels: list[NotificationChannel]
    ) -> None:
        self._ado = ado
        self._config = config
        self._channels = channels
        self._log = get_logger("ado.notifier")

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

    async def _broadcast(self, message: NotificationMessage) -> None:
        for channel in self._channels:
            if not channel.is_enabled:
                continue
            try:
                await channel.send(message)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("notification failed", channel=channel.name, error=str(exc))
