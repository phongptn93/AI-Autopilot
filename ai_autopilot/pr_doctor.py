"""``ai-autopilot pr-doctor <pr-url>`` — why a comment on THIS pull request did not
reach the autopilot.

A mention that goes unanswered gives you nothing to work with: the bot simply never
speaks. Seven independent gates have to pass before a comment becomes work, they live
in three different services, and most of them fail by returning early — so the log
is silent about six of the seven. This walks the same gates in the same order, on the
real PR, and stops at the first one that would have dropped the comment.

It is deliberately read-only and offline-safe: no comment is posted, no state is
changed, and every ADO call is a GET. Run it where the autopilot runs, so it reads
that machine's config and credentials.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass

from ai_autopilot.config import (
    BotIdentity,
    Settings,
    describe_users,
    find_bot_mention,
    load_settings,
    match_command,
)
from ai_autopilot.services.pr_feedback import is_bot_branch, parse_work_item_id

OK, BAD, WARN = "ok", "bad", "warn"

_PR_URL = re.compile(
    r"/_git/(?P<repo>[^/\s?#]+)/pullrequest/(?P<pr>\d+)", re.IGNORECASE
)


@dataclass
class Check:
    level: str
    title: str
    detail: str = ""
    fix: str = ""


def parse_target(url: str) -> tuple[str, int] | None:
    """``(repo name, pr id)`` from a PR URL, or None if it is not one."""
    m = _PR_URL.search(url or "")
    return (m.group("repo"), int(m.group("pr"))) if m else None


def check_switches(cfg: Settings, owned: bool) -> list[Check]:
    """The two loops that can answer a comment, and whether either is running.

    Which one applies depends on who opened the PR: the babysitter only ever handles
    branches the autopilot itself created, so on a hand-made PR the reviewer tracker
    is the only path — and it is off by default.
    """
    out: list[Check] = []
    if owned:
        out.append(
            Check(OK, "PR feedback loop is on (this PR's branch is autopilot-shaped)")
            if cfg.feedback_loop_enabled
            else Check(
                BAD, "PR feedback loop is off",
                "feedback_loop_enabled=false, so nothing scans this PR for commands.",
                "Settings → 🔁 PR review & feedback → Enable PR feedback loop.",
            )
        )
    else:
        out.append(
            Check(OK, "PR reviewer tracking is on (the path for hand-made PRs)")
            if cfg.pr_reviewer_tracking_enabled
            else Check(
                BAD, "PR reviewer tracking is off",
                "This PR was not opened by the autopilot, so the babysitter ignores it "
                "and the reviewer tracker is the only loop that would answer a comment "
                "here — pr_reviewer_tracking_enabled=false.",
                "Settings → PR reviewer tracking → Track PR reviewers.",
            )
        )
    if not cfg.comment_mention_enabled:
        out.append(Check(
            BAD, "@mentions are not treated as commands",
            "comment_mention_enabled=false — only a literal /command is read.",
            "Settings → 🔁 PR review & feedback → Answer an @mention on a PR.",
        ))
    if cfg.dry_run:
        out.append(Check(
            WARN, "dry_run is on",
            "Commands are recognised and logged, but nothing is written back.",
            "Turn dry_run off to let the bot act.",
        ))
    return out


def check_branch(source_ref: str, cfg: Settings) -> tuple[bool, list[Check]]:
    """Which loop owns this PR — decided by the branch PREFIX alone.

    The work-item id in the branch name is not part of ownership: it is resolved from
    ADO's PR link when a command needs it, so a branch we created but named without an
    id is still ours.
    """
    branch = (source_ref or "").removeprefix("refs/heads/")
    if is_bot_branch(source_ref, tuple(cfg.bot_branch_prefixes)):
        item_id = parse_work_item_id(source_ref)
        where = f"item #{item_id} from the name" if item_id else "work item from the PR link"
        return True, [Check(OK, f"Branch was created by the autopilot ({branch} → {where})")]
    why = "prefix is not one of " + ", ".join(cfg.bot_branch_prefixes)
    return False, [Check(
        WARN, f"Branch '{branch}' was not created by the autopilot",
        f"({why}) The two loops divide the work by that test, so commands here are the "
        "reviewer tracker's job. It revises and pushes on hand-made branches just like "
        "the babysitter does on its own — what it waits for is not a branch name but "
        "your consent: THE BOT MUST BE A REVIEWER ON THE PR.",
        "Nothing to fix on the branch. Turn on PR reviewer tracking and add the bot as "
        "a reviewer on this PR.",
    )]


def check_scope(target_ref: str, repo: str, cfg: Settings) -> list[Check]:
    """Target-branch and repository scoping, both of which silently exclude a PR."""
    out: list[Check] = []
    target = (target_ref or "").removeprefix("refs/heads/")
    if cfg.target_in_scope(target_ref):
        out.append(Check(OK, f"Target branch in scope ({target or '?'})"))
    else:
        out.append(Check(
            BAD, f"Target branch '{target}' is out of scope",
            "pr_reviewer_target_branches = " + ", ".join(cfg.reviewer_target_branches),
            "Add this branch to the target list, or clear the list to cover every branch.",
        ))
    allowed = [r for r in (cfg.allowed_repos or []) if r.strip()]
    if allowed and repo not in allowed:
        out.append(Check(
            BAD, f"Repository '{repo}' is not in allowed_repos",
            "allowed_repos = " + ", ".join(allowed),
            "Add the repo, or clear allowed_repos to cover them all.",
        ))
    return out


def check_reviewer_seat(
    pr: dict, bot: BotIdentity | None, owned: bool, cfg: Settings | None = None
) -> list[Check]:
    """On a hand-made PR, being a reviewer is what licenses the bot to act.

    The reviewer tracker answers /commands only on PRs it was ADDED to — which is the
    real consent signal, and the one gate a reader could never infer from the config.
    Nothing in the PR or the log hints at it: an uninvited bot is simply silent.
    """
    if owned:
        return []
    reviewers = [r for r in (pr.get("reviewers") or []) if not r.get("isContainer")]
    if cfg is not None and cfg.pr_commands_on_any_pr:
        return [Check(
            OK, "Reviewer seat not required (pr_commands_on_any_pr is on)",
            "Being named in a comment by someone on the command roster is accepted as "
            "the consent instead.",
        )]
    names = [r.get("displayName") or r.get("uniqueName") or "?" for r in reviewers]
    if bot is not None and bot.identity_id:
        seated = any(str(r.get("id") or "").lower() == bot.identity_id.lower()
                     for r in reviewers)
    else:  # no GUID to compare — fall back to the display name, like the bot itself does
        want = (bot.display_name if bot else "").strip().lower()
        seated = bool(want) and any(want in n.lower() for n in names)
    if seated:
        return [Check(OK, "The bot is a reviewer on this PR")]
    return [Check(
        BAD, "The bot is NOT a reviewer on this PR",
        "On a PR the autopilot did not open, the reviewer tracker only answers commands "
        "where it was added as a reviewer — that invitation IS the permission. "
        + (f"Current reviewers: {', '.join(names)}." if names else "No reviewers yet."),
        "Add the bot account as a reviewer on the PR — or turn on "
        "pr_commands_on_any_pr to let a comment from the command roster be the consent "
        "instead — then comment again.",
    )]


def check_comments(threads: list[dict], cfg: Settings, bot: BotIdentity | None) -> list[Check]:
    """Did anything in this PR actually address the bot, and may that person command it?

    Two different silences look identical from outside: "nobody said anything the bot
    recognises" and "someone did, but they are not allowed to". Both are named.
    """
    addressed: list[tuple[str, str, str]] = []  # (author, how, text)
    for thread in threads:
        for comment in thread.get("comments") or []:
            if (comment.get("commentType") or "text") == "system":
                continue
            content = comment.get("content") or ""
            author = (comment.get("author") or {}).get("displayName") or "?"
            if match_command(content, cfg.comment_commands):
                addressed.append((author, "/command", content[:80]))
            elif bot is not None and find_bot_mention(content, bot) is not None:
                addressed.append((author, "@mention", content[:80]))

    if not addressed:
        return [Check(
            BAD, "No comment on this PR addresses the bot",
            "Nothing matched a configured /command, and no @mention resolved to this "
            "machine's identity. An ADO mention carries the GUID of the account it "
            "points at — mentioning a DIFFERENT account with a similar name does not "
            "reach this bot.",
            "Check the identity below, then @mention exactly that account, or start "
            "the comment with one of: " + ", ".join(cfg.comment_commands),
        )]

    out = [Check(OK, f"{len(addressed)} comment(s) address the bot")]
    claimed = cfg.command_allowlist
    for author, how, text in addressed:
        allowed = not claimed or any(
            author.strip().lower() == c.strip().lower()
            or c.strip().lower() in author.strip().lower()
            for c in claimed
        )
        out.append(Check(
            OK if allowed else BAD,
            f"{how} by {author}: {text}",
            "" if allowed else f"This machine only obeys: {describe_users(claimed)}",
            "" if allowed else "Add them under Tags & Trigger → others allowed to "
                               "command, or turn on 'let ANYONE command'.",
        ))
    return out


def identity_note(bot: BotIdentity | None, cfg: Settings) -> Check:
    """Who the bot thinks it is — the single most common reason a mention misses."""
    if bot is None:
        return Check(BAD, "@mention matching is disabled (comment_mention_enabled=false)")
    if bot.identity_id:
        return Check(
            OK, f"Bot identity: {bot.display_name or '(no name)'}",
            f"id={bot.identity_id} — mentions are matched by GUID, so a rename is safe.",
        )
    return Check(
        WARN, "Bot identity could not be resolved from the credentials",
        "Mentions fall back to matching the display name "
        f"'{bot.display_name or cfg.pr_bot_identity or '(none set)'}'.",
        "Set pr_bot_identity to the exact display name of the account behind the PAT.",
    )


async def diagnose(url: str, cfg: Settings) -> list[Check]:
    """Walk every gate for one PR, stopping at the first that cannot be evaluated."""
    import httpx

    from ai_autopilot.ado.auth import AdoAuthService
    from ai_autopilot.ado.client import AdoClient

    target = parse_target(url)
    if target is None:
        return [Check(BAD, "That does not look like a pull request URL",
                      "Expected …/_git/<repo>/pullrequest/<id>")]
    repo, pr_id = target
    http = httpx.AsyncClient(timeout=30)
    client = AdoClient(http, AdoAuthService(cfg), cfg)
    try:
        pr = await client.get_pull_request(repo, pr_id)
        if not pr:
            return [Check(BAD, f"PR !{pr_id} not found in '{repo}'",
                          "Wrong project/organisation, or the PAT cannot see this repo.",
                          "Check ado_organization / code_project and the PAT's scope.")]
        out = [Check(OK, f"PR !{pr_id} in {repo}: {(pr.get('title') or '')[:70]}")]
        owned, branch_checks = check_branch(pr.get("sourceRefName", ""), cfg)
        out += branch_checks
        out += check_switches(cfg, owned)
        out += check_scope(pr.get("targetRefName", ""), repo, cfg)

        bot = None
        if cfg.comment_mention_enabled:
            detected = await client.get_connection_data() or {}
            bot = BotIdentity(
                identity_id=detected.get("id") or "",
                display_name=detected.get("display_name") or cfg.pr_bot_identity,
                claimed=cfg.command_user,
            )
        out.append(identity_note(bot, cfg))
        out += check_reviewer_seat(pr, bot, owned, cfg)

        # Threads are addressed by repo id, and the URL only carries the name.
        repos = await client.get_repositories()
        repo_id = next(
            (r.get("id") for r in repos if (r.get("name") or "").lower() == repo.lower()),
            repo,
        )
        threads = await client.get_pull_request_threads(repo_id, pr_id)
        out += check_comments(threads, cfg, bot)
        return out
    finally:
        await http.aclose()


def render(checks: list[Check]) -> str:
    icon = {OK: "✅", WARN: "⚠️ ", BAD: "⛔"}
    lines: list[str] = []
    for c in checks:
        lines.append(f"{icon.get(c.level, '·')} {c.title}")
        if c.detail:
            lines.append(f"     {c.detail}")
        if c.fix:
            lines.append(f"     → {c.fix}")
    blockers = [c for c in checks if c.level == BAD]
    lines.append("")
    lines.append(
        f"{len(blockers)} blocker(s) — fix the first one and comment again."
        if blockers
        else "No blocker found: a comment addressing the bot on this PR should be picked "
             "up within one scan interval."
    )
    return "\n".join(lines)


def run(url: str = "") -> int:
    if not url:
        print("usage: ai-autopilot pr-doctor <pull request url>", file=sys.stderr)
        return 2
    cfg = load_settings()
    checks = asyncio.run(diagnose(url, cfg))
    print(render(checks))
    return 1 if any(c.level == BAD for c in checks) else 0
