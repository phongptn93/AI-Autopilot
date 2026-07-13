"""Retry policy tests (ported from RetryPolicy)."""

from __future__ import annotations

from ai_autopilot.execution import RetryPolicy


def test_new_item_should_retry():
    policy = RetryPolicy(max_retries=3, backoff_seconds=1)
    assert policy.should_retry(1) is True
    assert policy.is_exhausted(1) is False


def test_exhausts_after_max_retries():
    policy = RetryPolicy(max_retries=2, backoff_seconds=1)
    policy.record_failure(1, "boom")
    assert policy.is_exhausted(1) is False
    policy.record_failure(1, "boom again")
    assert policy.is_exhausted(1) is True
    assert policy.should_retry(1) is False


def test_success_clears_state():
    policy = RetryPolicy(max_retries=2, backoff_seconds=1)
    policy.record_failure(1, "boom")
    policy.record_success(1)
    assert policy.get_state(1) is None
    assert policy.is_exhausted(1) is False


def test_backoff_active_immediately_after_failure():
    policy = RetryPolicy(max_retries=3, backoff_seconds=60)
    policy.record_failure(1, "boom")
    assert policy.is_backoff_active(1) is True
