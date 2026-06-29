"""Tests for the PR babysitter's pure decision logic."""

from __future__ import annotations

import pytest

from ai_autopilot.services.pr_feedback import (
    actionable_comments,
    is_bot_branch,
    parse_work_item_id,
)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("refs/heads/feature/be/123-add-login", 123),
        ("refs/heads/fix/42-crash", 42),
        ("feature/fe/7-page", 7),
        ("refs/heads/autopilot/loop/deps-20260629", None),
        ("refs/heads/develop", None),
    ],
)
def test_parse_work_item_id(ref, expected):
    assert parse_work_item_id(ref) == expected


def test_is_bot_branch():
    pfx = ("feature/", "fix/", "autopilot/")
    assert is_bot_branch("refs/heads/feature/be/1-x", pfx) is True
    assert is_bot_branch("refs/heads/fix/2-y", pfx) is True
    assert is_bot_branch("refs/heads/main", pfx) is False


def test_actionable_comments_filters_resolved_and_system():
    def comment(text, author="Human", ctype="text"):
        return {"commentType": ctype, "author": {"displayName": author}, "content": text}

    threads = [
        {"status": "active", "comments": [comment("Please rename")]},
        {"status": "fixed", "comments": [comment("old issue")]},
        {"status": "active", "comments": [comment("PR updated", ctype="system")]},
    ]
    assert actionable_comments(threads) == ["Please rename"]


def test_actionable_comments_excludes_bot_author():
    def comment(text, author):
        return {"commentType": "text", "author": {"displayName": author}, "content": text}

    threads = [
        {
            "status": "active",
            "comments": [comment("done", "Autopilot"), comment("fix this", "Carol")],
        }
    ]
    assert actionable_comments(threads, bot_name="Autopilot") == ["fix this"]
