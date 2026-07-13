"""Per-work-item retry tracking with exponential backoff (ported from ``RetryPolicy``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ai_autopilot.logging_config import get_logger


@dataclass
class RetryState:
    retry_count: int = 0
    last_attempt: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error: str | None = None


class RetryPolicy:
    def __init__(self, max_retries: int, backoff_seconds: int) -> None:
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._states: dict[int, RetryState] = {}
        self._log = get_logger("execution.retry_policy")

    def should_retry(self, work_item_id: int) -> bool:
        state = self._states.get(work_item_id)
        return state is None or state.retry_count < self._max_retries

    def is_backoff_active(self, work_item_id: int) -> bool:
        state = self._states.get(work_item_id)
        if state is None:
            return False
        delay = self._backoff_seconds * (2 ** (state.retry_count - 1))
        elapsed = (datetime.now(UTC) - state.last_attempt).total_seconds()
        return elapsed < delay

    def record_failure(self, work_item_id: int, error: str) -> None:
        state = self._states.get(work_item_id)
        if state is None:
            state = RetryState(retry_count=1, last_error=error)
            self._states[work_item_id] = state
        else:
            state.retry_count += 1
            state.last_attempt = datetime.now(UTC)
            state.last_error = error
        self._log.warning(
            "work item failed",
            id=work_item_id,
            attempt=state.retry_count,
            max=self._max_retries,
            error=error,
        )

    def record_success(self, work_item_id: int) -> None:
        self._states.pop(work_item_id, None)

    def is_exhausted(self, work_item_id: int) -> bool:
        state = self._states.get(work_item_id)
        return state is not None and state.retry_count >= self._max_retries

    def get_state(self, work_item_id: int) -> RetryState | None:
        return self._states.get(work_item_id)
