"""Token cost tracking + daily budget alerts (ported from ``CostTracker``)."""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, date, datetime

from ai_autopilot.config import Settings
from ai_autopilot.data.repository import ExecutionRepository
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import WorkItemInfo
from ai_autopilot.notifications.base import (
    NotificationChannel,
    NotificationMessage,
    NotificationType,
)

_TOKEN_RE = re.compile(r"(\d[\d,]+)\s*tokens?", re.IGNORECASE)


class CostTracker:
    def __init__(
        self,
        repo: ExecutionRepository,
        config: Settings,
        channels: list[NotificationChannel],
    ) -> None:
        self._repo = repo
        self._config = config
        self._channels = channels
        self._log = get_logger("tracking.cost")
        self._daily_tokens = 0
        self._daily_reset: date = datetime.now(UTC).date()
        self._alert_sent = False

    @staticmethod
    def parse_token_count(claude_output: str) -> int:
        """Fallback parser for free-text token counts (kept for compatibility)."""
        total = 0
        for match in _TOKEN_RE.finditer(claude_output):
            total += int(match.group(1).replace(",", ""))
        return total

    async def track(self, record_id: int, tokens: int) -> None:
        if tokens <= 0:
            return

        await self._repo.update_cost(record_id, tokens)

        today = datetime.now(UTC).date()
        if today != self._daily_reset:
            self._daily_tokens = 0
            self._daily_reset = today
            self._alert_sent = False

        self._daily_tokens += tokens
        self._log.debug("cost tracked", tokens=tokens, daily=self._daily_tokens)

        budget = self._config.daily_budget_tokens
        if (
            self._config.cost_alert_enabled
            and budget > 0
            and self._daily_tokens > budget
            and not self._alert_sent
        ):
            self._alert_sent = True
            self._log.warning(
                "daily token budget exceeded", used=self._daily_tokens, budget=budget
            )
            await self._broadcast_budget_alert(budget)

    async def _broadcast_budget_alert(self, budget: int) -> None:
        message = NotificationMessage(
            work_item=WorkItemInfo(id=0, title="Budget Alert"),
            type=NotificationType.ERROR,
            error=(
                f"Daily token budget exceeded: {self._daily_tokens:,} / {budget:,} tokens"
            ),
        )
        for channel in self._channels:
            if not channel.is_enabled:
                continue
            with contextlib.suppress(Exception):
                await channel.send(message)
