"""Work-item queue for the ADO Service Hook (ported from ``WebhookController``).

The webhook endpoint (defined in ``app.py``) simply enqueues work item IDs here;
the background poller drains the queue on its next cycle so all execution goes
through a single code path.
"""

from __future__ import annotations

from collections import deque


class WebhookQueue:
    """FIFO of work item IDs awaiting pickup (single-process asyncio app)."""

    def __init__(self) -> None:
        self._queue: deque[int] = deque()

    def enqueue(self, work_item_id: int) -> None:
        self._queue.append(work_item_id)

    def drain(self) -> list[int]:
        items: list[int] = []
        while self._queue:
            items.append(self._queue.popleft())
        return items

    def __len__(self) -> int:
        return len(self._queue)
