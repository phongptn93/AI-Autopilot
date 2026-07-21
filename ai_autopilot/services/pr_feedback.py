"""Pure decision logic for the PR babysitter (no I/O — easy to unit-test).

Given ADO PR threads, decide which review comments are *actionable* (unresolved,
human-authored) and map a PR's source branch back to its work item id.
"""

from __future__ import annotations

from typing import Any

from ai_autopilot.config import is_bot_signed, match_command

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
            # Skip the bot's OWN comments (acks / status updates) — told apart by the
            # signature, not the author, since the bot posts under the operator's identity.
            if content and not is_bot_signed(content):
                out.append(content)
    return out


def command_threads(
    threads: list[dict[str, Any]], commands: list[str]
) -> list[dict[str, Any]]:
    """PR threads whose latest human comment is a ``/command`` addressed to the autopilot.

    Returns one entry per actionable thread — ``{thread_id, comment_id, instruction,
    author_email, author_name}`` — so the babysitter can handle each command individually.

    "Handled" is judged PER COMMENT, not by thread status: a command counts as done once a
    bot-signed reply follows it in the thread. That's the durable mark (survives restarts),
    and it's what lets a user keep the conversation going — replying ``/ai fix it`` under
    the bot's review findings re-activates the thread even though the bot resolved it after
    the earlier ``/review``. A RESOLVED thread the bot never spoke in is different: the
    human closed it themselves, so its command is treated as dismissed, not pending."""
    out: list[dict[str, Any]] = []
    for thread in threads:
        resolved = (thread.get("status") or "").lower() in _RESOLVED_STATUSES
        tid = thread.get("id")
        if tid is None:
            continue
        latest, instruction = None, None
        bot_seen = False          # any bot-signed comment so far
        bot_replied_after = False  # bot-signed reply AFTER the newest command → handled
        follows_bot = False        # newest command came after a bot reply (a follow-up)
        for comment in thread.get("comments") or []:
            if (comment.get("commentType") or "text") == "system":
                continue
            if is_bot_signed(comment.get("content") or ""):
                bot_seen = True
                if latest is not None:
                    bot_replied_after = True
                continue
            got = match_command(comment.get("content"), commands)
            if got is not None:
                latest, instruction = comment, got  # keep the NEWEST command in the thread
                bot_replied_after = False           # a newer command supersedes old replies
                follows_bot = bot_seen
        if latest is None or bot_replied_after:
            continue  # no command, or the bot already answered the newest one
        if resolved and not follows_bot:
            continue  # human-resolved before the bot ever engaged → dismissed
        author = latest.get("author") or {}
        out.append({
            "thread_id": tid,
            "comment_id": latest.get("id"),
            "instruction": instruction,
            "author_email": author.get("uniqueName"),
            "author_name": author.get("displayName"),
        })
    return out


def newest_actionable_thread_id(threads: list[dict[str, Any]]) -> int | None:
    """Thread id of the most recent unresolved, human-authored (unsigned) comment — the
    thread the babysitter should reply INTO so the conversation stays together. Returns
    None when there's nothing to reply to (fall back to a fresh thread)."""
    best_id: int | None = None
    best_key: str | None = None
    for thread in threads:
        if (thread.get("status") or "").lower() in _RESOLVED_STATUSES:
            continue
        tid = thread.get("id")
        if tid is None:
            continue
        for comment in thread.get("comments") or []:
            if (comment.get("commentType") or "text") == "system":
                continue
            content = (comment.get("content") or "").strip()
            if not content or is_bot_signed(content):
                continue
            key = str(comment.get("publishedDate") or "")
            if best_key is None or key >= best_key:
                best_key, best_id = key, tid
    return best_id
