"""The activity feed key comes from the URL — it must not reach the filesystem raw."""

from __future__ import annotations

from ai_autopilot.dashboard import _feed_key


def test_accepts_the_two_shapes_we_mint():
    assert _feed_key("8946") == "8946"        # a work item
    assert _feed_key("pr-3882") == "pr-3882"  # a PR-level run


def test_rejects_anything_that_could_walk_the_path():
    for raw in ("../../config", "pr-3882/../../x", "..", "a" * 5, "pr-", "3882.activity.log"):
        assert _feed_key(raw) == "", raw
