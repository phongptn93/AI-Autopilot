"""Prometheus metrics (ported from ``AutopilotMetrics``)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

TASKS_TOTAL = Counter(
    "autopilot_tasks_total",
    "Total tasks processed",
    ["status", "category", "skill"],
)

TASK_DURATION = Histogram(
    "autopilot_task_duration_seconds",
    "Task execution duration in seconds",
    ["category"],
    buckets=[10 * 2**i for i in range(10)],  # 10s, 20s, ... ~5120s
)

POLL_ITEMS_FOUND = Gauge(
    "autopilot_poll_items_found",
    "Number of items found in last poll",
)

ACTIVE_EXECUTIONS = Gauge(
    "autopilot_active_executions",
    "Number of currently running executions",
)

RETRY_TOTAL = Counter("autopilot_retry_total", "Total retry attempts")
COST_TOKENS_TOTAL = Counter("autopilot_cost_tokens_total", "Total tokens consumed")


def record_task(status: str, category: str, skill: str) -> None:
    TASKS_TOTAL.labels(status, category, skill).inc()


def record_duration(category: str, seconds: float) -> None:
    TASK_DURATION.labels(category).observe(seconds)


def set_poll_items_found(count: int) -> None:
    POLL_ITEMS_FOUND.set(count)


def record_cost(tokens: int) -> None:
    if tokens:
        COST_TOKENS_TOTAL.inc(tokens)
