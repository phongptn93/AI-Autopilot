"""Pure decision logic for the PR babysitter (no I/O — easy to unit-test).

Given ADO PR threads, decide which review comments are *actionable* (unresolved,
human-authored) and map a PR's source branch back to its work item id.
"""

from __future__ import annotations

from typing import Any

# Thread statuses that mean "no action needed".
_RESOLVED_STATUSES = {"closed", "fixed", "wontfix", "resolved", "bydesign"}


def parse_work_item_id(source_ref: str) -> int | None:
    """Extract the work item id from a branch like ``feature/be/123-slug``.

    ``source_ref`` may be a full ref (``refs/heads/feature/be/123-foo``) or a
    bare branch name. The id is the leading numeric token of the last segment.
    """
    if not source_ref:
        return None
    name = source_ref.rsplit("/", 1)[-1]  # "123-foo"
    head = name.split("-", 1)[0]
    return int(head) if head.isdigit() else None


def is_bot_branch(source_ref: str, prefixes: tuple[str, ...]) -> bool:
    """True if the branch was created by autopilot (matches a known prefix)."""
    branch = source_ref.removeprefix("refs/heads/")
    return any(branch.startswith(p) for p in prefixes)


def actionable_comments(threads: list[dict[str, Any]], bot_name: str = "") -> list[str]:
    """Return unresolved, human-authored review comments from PR threads."""
    out: list[str] = []
    for thread in threads:
        status = (thread.get("status") or "").lower()
        if status in _RESOLVED_STATUSES:
            continue
        for comment in thread.get("comments") or []:
            if (comment.get("commentType") or "text") == "system":
                continue
            author = ((comment.get("author") or {}).get("displayName")) or ""
            if bot_name and author.lower() == bot_name.lower():
                continue
            content = (comment.get("content") or "").strip()
            if content:
                out.append(content)
    return out
