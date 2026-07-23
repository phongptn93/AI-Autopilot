"""PR reviewer tracker.

Watches the reviewer list of EVERY active PR in the configured repos (not just
autopilot-created ones) and reacts to changes:

- **Bot added as reviewer** → run an AI code review (read-only), post structured
  findings on the PR, and cast a vote. Re-arms when new commits land on the PR.
- **Human reviewers** → track who was added and their votes (dashboard), and post
  ONE polite reminder on the PR when a reviewer sits vote-less past the deadline.

Detection is a snapshot diff: the live ADO reviewer list vs ``pr_reviewer_states``
rows — restart-proof, so the bot never re-reviews a commit it already voted on and
never double-reminds.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime
from urllib.parse import quote

from ai_autopilot import metrics
from ai_autopilot.config import match_command, matches_user
from ai_autopilot.container import Container
from ai_autopilot.logging_config import get_logger
from ai_autopilot.models import TaskCategory, WorkItemInfo
from ai_autopilot.notifications.base import NotificationMessage, NotificationType
from ai_autopilot.services.pr_feedback import (
    command_threads,
    is_bot_branch,
    parse_work_item_id,
)

# ADO reviewer vote scale.
VOTE_APPROVED = 10
VOTE_APPROVED_SUGGESTIONS = 5
VOTE_NONE = 0
VOTE_WAITING = -5
VOTE_REJECTED = -10

VOTE_LABELS = {
    VOTE_APPROVED: "✅ Approved",
    VOTE_APPROVED_SUGGESTIONS: "✅ Approved with suggestions",
    VOTE_NONE: "⏳ No vote yet",
    VOTE_WAITING: "⏸️ Waiting for author",
    VOTE_REJECTED: "❌ Rejected",
}

# Vote value → Prometheus metric label.
_VOTE_METRIC = {
    VOTE_APPROVED: "approved",
    VOTE_APPROVED_SUGGESTIONS: "suggestions",
    VOTE_WAITING: "wait",
}

# The auto-review prompt asks the agent to end with this marker; we map it to a vote.
_VERDICT_RE = re.compile(r"VERDICT:\s*(approve|suggestions|wait)", re.IGNORECASE)
_VERDICT_VOTES = {
    "approve": VOTE_APPROVED,
    "suggestions": VOTE_APPROVED_SUGGESTIONS,
    "wait": VOTE_WAITING,
}

_REVIEW_INSTRUCTION = (
    "You were just added as a REVIEWER on this pull request. Perform a professional, "
    "structured code review and post it as a PR comment with exactly these sections:\n"
    "1. **📋 Summary** — what the PR does, in 2-3 sentences.\n"
    "2. **🔍 Findings** — grouped by severity (🔴 Critical / 🟠 Major / 🟡 Minor / "
    "💡 Suggestion), each with file:line and a concrete fix. Write 'None' for empty "
    "groups.\n"
    "3. **✅ Checklist** — correctness, security, performance, tests, naming/style: "
    "one line each with a pass/fail/n-a verdict.\n"
    "4. **🎯 Verdict** — your overall recommendation.\n"
    "Be specific and courteous; review the DIFF only, don't restyle the codebase. "
    "Then end your FINAL response (not the comment) with one line:\n"
    "VERDICT: approve | suggestions | wait\n"
    "(approve = mergeable as-is; suggestions = mergeable, minor points; wait = must "
    "fix critical/major findings first)."
)


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes — they were stored as UTC, restamp them."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ADO ISO-8601 timestamp (e.g. ``creationDate``) into a UTC-aware
    datetime. None on missing/unparseable input — callers then skip the item rather
    than guess a date."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class ReviewerTrackerService:
    def __init__(self, c: Container) -> None:
        self._c = c
        self._config = c.config
        self._log = get_logger("services.reviewer_tracker")
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        # Bounded like the babysitter — an auto-review is a full agent run.
        self._sem = asyncio.Semaphore(c.config.max_concurrent)
        # PRs with an auto-review in flight (don't double-launch across scans).
        self._reviewing: set[int] = set()
        # Resolved bot identity {"id", "display_name", "unique_name"} — cached.
        self._bot: dict | None = None
        # Serialise /ai revises per (repo, branch) so parallel commands on one branch
        # can't corrupt the checkout (different branches still run in parallel).
        self._branch_locks: dict[tuple[str, str], asyncio.Lock] = {}

    @property
    def _repo(self):
        return getattr(self._c, "pr_reviewer_repo", None)

    @property
    def _cmd_repo(self):
        """Shared restart-proof handled-comment / revision store (with the babysitter)."""
        return getattr(self._c, "pr_command_repo", None)

    def start(self) -> None:
        if not self._config.pr_reviewer_tracking_enabled:
            self._log.info("PR reviewer tracking disabled")
            return
        self._task = asyncio.create_task(self._run(), name="reviewer-tracker")

    async def stop(self) -> None:
        for task in [self._task, *self._tasks]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def trigger_review_now(self, repo_id: str, pr_id: int) -> str:
        """On-demand re-review, called from the Teams bot's "🔍 Review lại ngay" button.

        Requires the bot to already be a reviewer on the PR (it votes as ITSELF — with
        only app-only credentials there's no per-human identity to vote as, so this never
        pretends to be the clicking user). Returns a short status string for the reply."""
        bot = await self._bot_identity()
        if not bot.get("id"):
            return "Không xác định được danh tính bot — kiểm tra cấu hình PAT."
        repos = {r.get("id"): r.get("name") or "" for r in await self._c.ado.get_repositories()}
        repo_name = repos.get(repo_id, "")
        target_pr = None
        for pr in await self._c.ado.get_active_pull_requests(repo_id):
            if pr.get("pullRequestId") == pr_id:
                target_pr = pr
                break
        if target_pr is None:
            return f"PR !{pr_id} không còn active."
        reviewers = target_pr.get("reviewers") or []
        if not any(str(r.get("id")) == bot["id"] for r in reviewers):
            return f"Bot chưa phải reviewer của PR !{pr_id} — add reviewer trước."
        if pr_id in self._reviewing:
            return f"PR !{pr_id} đang được review, chờ chút nhé."
        commit = (target_pr.get("lastMergeSourceCommit") or {}).get("commitId") or ""
        self._reviewing.add(pr_id)
        self._spawn(self._auto_review(repo_id, repo_name, target_pr, bot["id"], commit))
        return f"🔍 Đã kích hoạt review lại PR !{pr_id} — kết quả sẽ đăng trên PR trong giây lát."

    async def prs_for_person(self, identity: str) -> list[dict]:
        """Active PRs where ``identity`` (email/uniqueName substring, case-insensitive)
        is the author or a reviewer — across every configured repo. Used by the Teams
        bot's ``/prs`` command; a live ADO scan (not cached) since it's a rare, on-demand
        query, not a hot path like the tracker's own poll."""
        needle = (identity or "").strip().lower()
        if not needle:
            return []
        out: list[dict] = []
        for repo in await self._c.ado.get_repositories():
            repo_id = repo.get("id")
            if not repo_id:
                continue
            repo_name = repo.get("name") or ""
            for pr in await self._c.ado.get_active_pull_requests(repo_id):
                if not self._config.target_in_scope(pr.get("targetRefName", "")):
                    continue
                author = pr.get("createdBy") or {}
                is_author = needle in (author.get("uniqueName") or "").lower()
                reviewers = pr.get("reviewers") or []
                mine = next(
                    (
                        r for r in reviewers
                        if needle in (r.get("uniqueName") or "").lower()
                        or needle in (r.get("displayName") or "").lower()
                    ),
                    None,
                )
                if not (is_author or mine):
                    continue
                out.append({
                    "id": pr.get("pullRequestId"),
                    "title": pr.get("title") or "",
                    "repo": repo_name,
                    "role": "author" if is_author else "reviewer",
                    "vote": int(mine.get("vote") or 0) if mine else None,
                    "is_draft": bool(pr.get("isDraft")),
                })
        return out

    async def prs_ready_to_merge(self) -> list[dict]:
        """Active, non-draft PRs with at least one approval and NOTHING blocking or
        still pending — i.e. genuinely ready, not just "has an approval". Used by the
        Teams digest."""
        out: list[dict] = []
        for repo in await self._c.ado.get_repositories():
            repo_id = repo.get("id")
            if not repo_id:
                continue
            repo_name = repo.get("name") or ""
            for pr in await self._c.ado.get_active_pull_requests(repo_id):
                if pr.get("isDraft") or not self._config.target_in_scope(
                    pr.get("targetRefName", "")
                ):
                    continue
                reviewers = [
                    r for r in (pr.get("reviewers") or [])
                    if r.get("id") and not r.get("isContainer")
                ]
                votes = [int(r.get("vote") or 0) for r in reviewers]
                if not votes:
                    continue
                approved = sum(1 for v in votes if v >= 5)
                blocked = sum(1 for v in votes if v < 0)
                pending = sum(1 for v in votes if v == 0)
                if approved >= 1 and blocked == 0 and pending == 0:
                    out.append({
                        "id": pr.get("pullRequestId"),
                        "title": pr.get("title") or "",
                        "repo": repo_name,
                        "author": (pr.get("createdBy") or {}).get("displayName") or "",
                    })
        return out

    async def new_prs_since(self, cutoff: datetime) -> list[dict]:
        """Active PRs created since ``cutoff`` — for the Teams digest's 24h activity."""
        out: list[dict] = []
        for repo in await self._c.ado.get_repositories():
            repo_id = repo.get("id")
            if not repo_id:
                continue
            repo_name = repo.get("name") or ""
            for pr in await self._c.ado.get_active_pull_requests(repo_id):
                if not self._config.target_in_scope(pr.get("targetRefName", "")):
                    continue
                created = _parse_dt(pr.get("creationDate"))
                if created is not None and created >= cutoff:
                    out.append({
                        "id": pr.get("pullRequestId"),
                        "title": pr.get("title") or "",
                        "repo": repo_name,
                        "author": (pr.get("createdBy") or {}).get("displayName") or "",
                    })
        return out

    async def merged_prs_since(self, cutoff: datetime) -> list[dict]:
        """Completed PRs merged since ``cutoff``. ADO's PR payload has no
        ``closedDate`` field (verified) — ``lastMergeCommit.committer.date`` is used
        as the merge timestamp instead, which IS present on a completed PR."""
        out: list[dict] = []
        for repo in await self._c.ado.get_repositories():
            repo_id = repo.get("id")
            if not repo_id:
                continue
            repo_name = repo.get("name") or ""
            for pr in await self._c.ado.get_completed_pull_requests(repo_id):
                if not self._config.target_in_scope(pr.get("targetRefName", "")):
                    continue
                merge_commit = pr.get("lastMergeCommit") or {}
                merged_at = _parse_dt((merge_commit.get("committer") or {}).get("date"))
                if merged_at is not None and merged_at >= cutoff:
                    out.append({
                        "id": pr.get("pullRequestId"),
                        "title": pr.get("title") or "",
                        "repo": repo_name,
                        "author": (pr.get("createdBy") or {}).get("displayName") or "",
                    })
        return out

    async def tickets_logged_since(self, cutoff: datetime) -> list:
        """Work items tagged ``teams-logged`` (created via the Teams ``/log`` command)
        changed since ``cutoff`` — for the Teams digest's 24h activity."""
        items = await self._c.ado.get_all_active_work_items(top=300)
        return [
            it for it in items
            if "teams-logged" in [t.lower() for t in it.tags]
            and it.changed_date is not None and _as_utc(it.changed_date) >= cutoff
        ]

    async def team_overview(self, limit: int = 10) -> list[dict]:
        """Every active PR across configured repos, OLDEST first — "what's stuck"
        for a team lead. Used by the Teams bot's ``/team`` command and NLU intent."""
        out: list[dict] = []
        for repo in await self._c.ado.get_repositories():
            repo_id = repo.get("id")
            if not repo_id:
                continue
            repo_name = repo.get("name") or ""
            for pr in await self._c.ado.get_active_pull_requests(repo_id):
                if not self._config.target_in_scope(pr.get("targetRefName", "")):
                    continue
                age_days = None
                created = pr.get("creationDate")
                if created:
                    with contextlib.suppress(ValueError):
                        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                        age_days = (datetime.now(UTC) - dt).days
                reviewers = [
                    r for r in (pr.get("reviewers") or [])
                    if r.get("id") and not r.get("isContainer")
                ]
                votes = [int(r.get("vote") or 0) for r in reviewers]
                out.append({
                    "id": pr.get("pullRequestId"),
                    "title": pr.get("title") or "",
                    "repo": repo_name,
                    "author": (pr.get("createdBy") or {}).get("displayName") or "",
                    "age_days": age_days,
                    "is_draft": bool(pr.get("isDraft")),
                    "approved": sum(1 for v in votes if v >= 5),
                    "pending": sum(1 for v in votes if v == 0),
                    "blocked": sum(1 for v in votes if v < 0),
                })
        out.sort(key=lambda p: p["age_days"] if p["age_days"] is not None else -1, reverse=True)
        return out[:limit]

    async def find_pr_by_id(self, pr_id: int) -> dict | None:
        """Same as ``pr_detail`` but scans every configured repo for ``pr_id`` — for
        free-text lookups ("PR 2261 tình trạng sao rồi") that don't name a repo."""
        for repo in await self._c.ado.get_repositories():
            repo_id = repo.get("id")
            if not repo_id:
                continue
            detail = await self.pr_detail(repo_id, pr_id)
            if detail is not None:
                return detail
        return None

    async def pr_detail(self, repo_id: str, pr_id: int) -> dict | None:
        """Full detail of ONE active PR — any PR, not filtered to a person. Used by
        the Teams bot's ``/pr <repo> <pr-id>`` lookup. None if not found/not active."""
        target = None
        for pr in await self._c.ado.get_active_pull_requests(repo_id):
            if pr.get("pullRequestId") == pr_id:
                target = pr
                break
        if target is None:
            return None
        reviewers = [
            {
                "name": r.get("displayName") or r.get("uniqueName") or "?",
                "vote": int(r.get("vote") or 0),
            }
            for r in (target.get("reviewers") or [])
            if r.get("id") and not r.get("isContainer")
        ]
        return {
            "id": pr_id,
            "title": target.get("title") or "",
            "author": (target.get("createdBy") or {}).get("displayName") or "",
            "source": (target.get("sourceRefName") or "").removeprefix("refs/heads/"),
            "target": (target.get("targetRefName") or "").removeprefix("refs/heads/"),
            "is_draft": bool(target.get("isDraft")),
            "reviewers": reviewers,
        }

    async def _run(self) -> None:
        self._log.info("reviewer tracker started — watching PR reviewer lists")
        while True:
            try:
                await asyncio.sleep(self._config.pr_reviewer_poll_interval_seconds or 30)
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log.error("reviewer tracker cycle failed", error=str(exc))

    # ── Bot identity ─────────────────────────────────────────────────────────

    async def _bot_identity(self) -> dict:
        """Who "the bot" is on ADO — the identity behind our credentials (auto-detected
        once via connectionData), optionally overridden by ``pr_bot_identity``."""
        if self._bot is None:
            detected = None
            with contextlib.suppress(Exception):
                detected = await self._c.ado.get_connection_data()
            self._bot = detected or {"id": "", "display_name": "", "unique_name": ""}
            if detected:
                self._log.info(
                    "bot identity resolved",
                    id=detected["id"], name=detected["display_name"],
                    unique=detected["unique_name"],
                )
            else:
                self._log.warning(
                    "could not resolve bot identity from connectionData — relying on "
                    "pr_bot_identity config", override=self._config.pr_bot_identity,
                )
        return self._bot

    def _is_bot(self, reviewer: dict, bot: dict) -> bool:
        rid = str(reviewer.get("id") or "")
        if bot.get("id") and rid == bot["id"]:
            return True
        override = (self._config.pr_bot_identity or "").strip().lower()
        if override:
            names = {
                str(reviewer.get("uniqueName") or "").lower(),
                str(reviewer.get("displayName") or "").lower(),
            }
            return override in names
        return False

    # ── Scan loop ────────────────────────────────────────────────────────────

    async def _scan(self) -> None:
        c = self._c
        bot = await self._bot_identity()
        active_ids: set[int] = set()
        for repo in await c.ado.get_repositories():
            repo_id = repo.get("id")
            if not repo_id:
                continue
            repo_name = repo.get("name") or ""
            for pr in await c.ado.get_active_pull_requests(repo_id):
                pid = pr.get("pullRequestId")
                if pid is not None:
                    active_ids.add(pid)
                try:
                    await self._track_pr(repo_id, repo_name, pr, bot)
                except Exception as exc:  # noqa: BLE001 — one bad PR must not stop the scan
                    self._log.error(
                        "reviewer tracking failed for PR",
                        pr=pr.get("pullRequestId"), error=str(exc),
                    )
        await self._forget_closed_prs(active_ids)

    async def _forget_closed_prs(self, active_ids: set[int]) -> None:
        """Drop tracked rows for PRs no longer active (completed / abandoned) so the
        store doesn't grow unbounded. Best-effort — never blocks the scan."""
        if self._repo is None:
            return
        try:
            tracked_ids = {snap.pr_id for snap in await self._repo.all_reviewers()}
            for pr_id in tracked_ids - active_ids:
                await self._repo.delete_pr(pr_id)
                self._log.info("forgot closed PR", pr=pr_id)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("closed-PR cleanup failed", error=str(exc))

    async def _track_pr(self, repo_id: str, repo_name: str, pr: dict, bot: dict) -> None:
        pr_id = pr.get("pullRequestId")
        if pr_id is None or self._repo is None:
            return
        # Target-branch filter: skip PRs merging into a branch outside the configured
        # scope (empty scope = every target).
        if not self._config.target_in_scope(pr.get("targetRefName", "")):
            return
        # Individuals only — group reviewers (isContainer) can't vote themselves.
        reviewers = [
            r for r in (pr.get("reviewers") or [])
            if r.get("id") and not r.get("isContainer")
        ]
        known = await self._repo.reviewers_for_pr(pr_id)
        current_commit = (pr.get("lastMergeSourceCommit") or {}).get("commitId") or ""

        pending_reminders: list[dict] = []
        for r in reviewers:
            rid = str(r["id"])
            vote = int(r.get("vote") or 0)
            is_bot = self._is_bot(r, bot)
            prev = known.get(rid)
            if prev is None:
                self._log.info(
                    "reviewer added", pr=pr_id, repo=repo_name,
                    reviewer=r.get("displayName"), bot=is_bot,
                )
            elif vote != prev.vote:
                self._log.info(
                    "reviewer vote changed", pr=pr_id, reviewer=r.get("displayName"),
                    vote=VOTE_LABELS.get(vote, vote),
                )
            snap = await self._repo.upsert(
                pr_id, rid, repo_id=repo_id,
                display_name=r.get("displayName") or "",
                unique_name=r.get("uniqueName") or "",
                is_bot=is_bot, vote=vote,
            )
            if is_bot:
                if self._should_auto_review(pr_id, snap.reviewed_commit, current_commit):
                    if self._config.dry_run:
                        # Dry-run: never touch ADO. Mark this commit reviewed so we log
                        # the intent once, not every scan.
                        self._log.info("[DRY-RUN] would auto-review + vote", pr=pr_id)
                        metrics.record_auto_review("skipped")
                        await self._repo.set_reviewed_commit(
                            pr_id, rid, current_commit or "@none"
                        )
                    else:
                        self._reviewing.add(pr_id)
                        self._spawn(
                            self._auto_review(repo_id, repo_name, pr, rid, current_commit)
                        )
            elif self._reminder_due(snap):
                pending_reminders.append(r)

        if pending_reminders:
            await self._send_reminder(repo_id, repo_name, pr, pending_reminders)
        # Reviewers removed from the PR by a human → forget them.
        await self._repo.remove_absent(pr_id, {str(r["id"]) for r in reviewers})

        # Interactive commands: when the bot is a reviewer here, honour /review and /ai
        # replies just like the babysitter — but ONLY on non-bot branches (the babysitter
        # already owns bot-created PRs, so this fills the "someone else's PR" gap without
        # double-handling; the shared handled-comment store guards the overlap anyway).
        bot_is_reviewer = any(self._is_bot(r, bot) for r in reviewers)
        if bot_is_reviewer and not is_bot_branch(
            pr.get("sourceRefName", ""), tuple(self._config.bot_branch_prefixes)
        ):
            await self._handle_commands(repo_id, repo_name, pr)

    def _should_auto_review(self, pr_id: int, reviewed_commit: str, commit: str) -> bool:
        if not self._config.pr_auto_review_on_added:
            return False
        if pr_id in self._reviewing:
            return False
        # Already reviewed this iteration (or ever, when the PR has no commit id yet).
        return reviewed_commit != (commit or "@none")

    def _reminder_due(self, snap) -> bool:
        hours = self._config.pr_reviewer_reminder_hours
        if not hours or snap.vote != VOTE_NONE or snap.reminded_at is not None:
            return False
        added = _as_utc(snap.added_at)
        if added is None:
            return False
        return (datetime.now(UTC) - added).total_seconds() >= hours * 3600

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _branch_lock(self, repo_id: str, branch: str) -> asyncio.Lock:
        key = (repo_id, branch)
        lock = self._branch_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._branch_locks[key] = lock
        return lock

    def _verb(self, instruction: str) -> str:
        """Leading /command in a comment (for metrics labels), or 'other'."""
        low = (instruction or "").lstrip().lower()
        for cmd in self._config.comment_commands:
            if cmd.startswith("/") and low.startswith(cmd.lower()):
                return cmd
        return "other"

    # ── Interactive commands (/review, /ai) on PRs the bot reviews ───────────

    async def _handle_commands(self, repo_id: str, repo_name: str, pr: dict) -> None:
        """Pick new ``/command`` threads on a PR the bot reviews and dispatch each.

        Mirrors the babysitter for non-bot PRs: only the configured user's commands run,
        each is marked handled (shared store) before dispatch so it never double-runs, and
        the revision cap bounds ``/ai`` churn. ``/review`` is advisory (no code change)."""
        c, cfg = self._c, self._config
        pr_id = pr.get("pullRequestId")
        cmd_repo = self._cmd_repo
        if pr_id is None or cmd_repo is None:
            return
        threads = await c.ado.get_pull_request_threads(repo_id, pr_id)
        commands = command_threads(threads, cfg.comment_commands)
        if not commands:
            return
        claimed = cfg.assignee_trigger_user or cfg.auto_transition_assignee
        handled = await cmd_repo.handled_comments(pr_id)
        branch = pr.get("sourceRefName", "").removeprefix("refs/heads/")
        item = await self._pr_work_item(pr)
        for cmd in commands:
            cid = cmd["comment_id"]
            if cid in handled:
                continue
            if cfg.dry_run:
                # Dry-run: acknowledge in the log, mark handled so we don't re-log, no ADO write.
                self._log.info("[DRY-RUN] would handle PR command", pr=pr_id, cmd=cmd["instruction"][:60])
                await cmd_repo.mark_handled(pr_id, cid)
                continue
            if not matches_user(cmd["author_email"], cmd["author_name"], claimed):
                await cmd_repo.mark_handled(pr_id, cid)
                self._spawn(self._reply(repo_id, pr_id, cmd["thread_id"],
                    f"<div>🔒 Tôi chỉ nhận lệnh từ <b>{claimed}</b> trên máy này — nhờ đúng "
                    "người reply để tôi tiếp nhận.</div>"))
                continue
            await cmd_repo.mark_handled(pr_id, cid)
            advisory = match_command(cmd["instruction"], cfg.advisory_commands) is not None
            metrics.record_pr_command(self._verb(cmd["instruction"]))
            if not advisory:
                spent = await cmd_repo.revision_count(item.id)
                if spent >= cfg.max_revisions:
                    self._spawn(self._reply(repo_id, pr_id, cmd["thread_id"],
                        f"<div>⏸️ Đã đạt giới hạn {cfg.max_revisions} lần sửa tự động cho "
                        "PR này. Tôi tạm dừng để tránh sửa lan man — nới "
                        "<code>max_revisions</code> nếu cần tiếp.</div>"))
                    continue
                await cmd_repo.set_revision_count(item.id, spent + 1)
            self._spawn(
                self._run_command(repo_id, repo_name, pr_id, branch, item, cmd, advisory)
            )

    async def _run_command(
        self, repo_id: str, repo_name: str, pr_id: int, branch: str,
        item: WorkItemInfo, cmd: dict, advisory: bool,
    ) -> None:
        c = self._c
        tid = cmd["thread_id"]
        lock = self._branch_lock(repo_id, branch)
        try:
            ack = (
                "<div><b>🔍 Đang xem lại</b> — tôi phân tích và đăng nhận xét ngay tại "
                "đây. Không chỉnh code.</div>"
                if advisory else
                "<div><b>🔧 Đang xử lý</b> — tôi tự chỉnh trên <code>"
                f"{branch}</code>, commit &amp; push rồi báo lại tại đây.</div>"
            )
            await c.ado.reply_to_pull_request_thread(repo_id, pr_id, tid, ack)
            await c.ado.set_pull_request_thread_status(repo_id, pr_id, tid, "pending")
            guard = contextlib.nullcontext() if advisory else lock
            async with guard, self._sem:
                result = await c.feedback.handle_feedback(
                    item, branch, cmd["instruction"], revision=0,
                    repo=repo_name, review_only=advisory,
                )
            hint = (
                "<br/><sub>💬 Reply để tôi làm tiếp: <code>/ai</code> sửa code · "
                "<code>/spec</code> cập nhật spec · <code>/test</code> viết test · "
                "<code>/review</code> · <code>/qc</code> · <code>/security</code> · "
                "<code>/impact</code> · <code>/summary</code>.</sub>"
            )
            if result.success:
                msg = (
                    f"<div><b>🔍 Đã xem xong</b> — nhận xét chi tiết ở trên.{hint}</div>"
                    if advisory
                    else f"<div><b>✅ Đã xử lý xong</b> — branch đã được cập nhật.{hint}</div>"
                )
                await c.ado.reply_to_pull_request_thread(repo_id, pr_id, tid, msg)
                await c.ado.set_pull_request_thread_status(repo_id, pr_id, tid, "fixed")
            else:
                verb = "xem" if advisory else "xử lý"
                await c.ado.reply_to_pull_request_thread(
                    repo_id, pr_id, tid,
                    f"<div><b>⚠️ Chưa {verb} được:</b> {result.error}{hint}</div>",
                )
                await c.ado.set_pull_request_thread_status(repo_id, pr_id, tid, "active")
        except Exception as exc:  # noqa: BLE001 — a background task must not die silently
            self._log.error("PR command failed", pr=pr_id, error=str(exc))

    async def _reply(self, repo_id: str, pr_id: int, thread_id: int, text: str) -> None:
        with contextlib.suppress(Exception):
            await self._c.ado.reply_to_pull_request_thread(repo_id, pr_id, thread_id, text)

    # ── Auto-review (bot added as reviewer) ──────────────────────────────────

    async def _auto_review(
        self, repo_id: str, repo_name: str, pr: dict, reviewer_id: str, commit: str
    ) -> None:
        """Run one structured AI review and vote as ``reviewer_id`` (the bot's entry in
        the PR's reviewer list). The reviewed-commit mark is written even on failure so
        a broken run can't re-launch every scan — a new push (or re-adding the bot
        after removing it) re-arms."""
        c = self._c
        pr_id = pr.get("pullRequestId")
        branch = (pr.get("sourceRefName") or "").removeprefix("refs/heads/")
        try:
            item = await self._pr_work_item(pr)
            self._log.info(
                "auto-review starting", pr=pr_id, repo=repo_name, branch=branch,
                commit=commit[:8] if commit else "",
            )
            await c.ado.add_pull_request_comment(
                repo_id, pr_id,
                "<div><b>🔍 Đã nhận vai trò reviewer</b> — tôi đang tự phân tích thay đổi "
                "và sẽ đăng nhận xét chi tiết kèm vote ngay tại đây. Không cần thao tác "
                "thêm.</div>",
            )
            instruction = _REVIEW_INSTRUCTION
            if self._config.use_specialized_agents:
                instruction += (
                    "\n\nIf an `agent-pr-reviewer` subagent is available, delegate the "
                    "review to it via the Task tool (it is purpose-built for PR review); "
                    "still end YOUR final response with the VERDICT line."
                )
            async with self._sem:
                result = await c.feedback.handle_feedback(
                    item, branch, instruction, revision=0,
                    repo=repo_name, review_only=True,
                )
            vote: int | None = None
            if result.success:
                vote = self._parse_verdict(result.output)
                if vote is None:
                    self._log.warning(
                        "auto-review produced no VERDICT marker — posting review "
                        "without a vote", pr=pr_id,
                    )
                voted = False
                if vote is not None:
                    voted = await c.ado.cast_pull_request_vote(
                        repo_id, pr_id, reviewer_id, vote
                    )
                    if voted:
                        metrics.record_vote(_VOTE_METRIC.get(vote, "other"))
                label = VOTE_LABELS.get(vote, "—") if vote is not None else "—"
                note = (
                    f"<div><b>✅ Review xong</b> — tôi đã tự vote: <b>{label}</b>."
                    "<br/><sub>💬 Reply để tôi làm tiếp: <code>/ai</code> sửa theo nhận xét "
                    "· <code>/spec</code> · <code>/test</code> · <code>/review</code> · "
                    "<code>/qc</code> · <code>/security</code> · <code>/impact</code>. "
                    "Push commit mới → tôi tự review lại.</sub></div>"
                )
                if vote is not None and not voted:
                    vote = None  # vote didn't land — don't record it
                    note = (
                        f"<div><b>✅ Review xong</b> — đề xuất: <b>{label}</b>. Tôi chưa "
                        "cast được vote (thiếu quyền trên repo này) — nhờ kiểm tra quyền "
                        "của tài khoản bot.</div>"
                    )
                await c.ado.add_pull_request_comment(repo_id, pr_id, note)
                metrics.record_auto_review("done")
                self._log.info("auto-review done", pr=pr_id, vote=label)
            else:
                await c.ado.add_pull_request_comment(
                    repo_id, pr_id,
                    f"<div><b>⚠️ Chưa review được:</b> {result.error}<br/>"
                    "<sub>Tôi sẽ tự thử lại khi có commit mới, hoặc reply "
                    "<code>/review</code> để tôi chạy lại ngay.</sub></div>",
                )
                metrics.record_auto_review("failed")
                self._log.warning("auto-review run failed", pr=pr_id, error=result.error)
            # Durable mark — success OR failure: this iteration was attempted, no
            # re-launch until a new commit lands.
            if self._repo is not None:
                await self._repo.upsert(
                    pr_id, reviewer_id, repo_id=repo_id, is_bot=True, vote=vote or 0,
                )
                await self._repo.set_reviewed_commit(pr_id, reviewer_id, commit or "@none")
        except Exception as exc:  # noqa: BLE001 — a background task must not die silently
            self._log.error("auto-review failed", pr=pr_id, error=str(exc))
        finally:
            self._reviewing.discard(pr_id)

    async def _pr_work_item(self, pr: dict) -> WorkItemInfo:
        """The PR's work item when the branch encodes one; else a synthetic stand-in
        (any dev's PR is reviewable — a work item is not required)."""
        branch = (pr.get("sourceRefName") or "").removeprefix("refs/heads/")
        wid = parse_work_item_id(f"refs/heads/{branch}")
        if wid is not None:
            item = None
            with contextlib.suppress(Exception):
                item = await self._c.ado.get_work_item(wid)
            if item is not None:
                return item
        return WorkItemInfo(
            id=pr.get("pullRequestId") or 0,
            title=pr.get("title") or "",
            work_item_type="PullRequest",
            category=TaskCategory.UNKNOWN,
        )

    @staticmethod
    def _parse_verdict(output: str | None) -> int | None:
        m = _VERDICT_RE.search(output or "")
        if m is None:
            return None
        return _VERDICT_VOTES.get(m.group(1).lower())

    # ── Reviewer reminders ───────────────────────────────────────────────────

    async def _send_reminder(
        self, repo_id: str, repo_name: str, pr: dict, reviewers: list[dict]
    ) -> None:
        """One polite reminder per PR for every overdue reviewer — posted as a PR comment
        (ADO emails the participants) AND broadcast to the configured channels (Teams /
        Email / Zalo). Each reviewer is marked reminded once. No-op writes under dry-run."""
        pr_id = pr.get("pullRequestId")
        hours = self._config.pr_reviewer_reminder_hours
        names = ", ".join(
            f"<b>{r.get('displayName') or r.get('uniqueName')}</b>" for r in reviewers
        )
        if self._config.dry_run:
            self._log.info("[DRY-RUN] would remind reviewers", pr=pr_id, count=len(reviewers))
            if self._repo is not None:
                for r in reviewers:
                    await self._repo.mark_reminded(pr_id, str(r["id"]))
            return
        note = (
            f"<div>👋 <b>Nhắc review</b> — {names} được thêm làm reviewer đã hơn {hours}h "
            "nhưng chưa vote. Nhờ anh/chị review để PR không bị nghẽn. Cần tóm tắt lại "
            "thay đổi, reply <code>/review</code> để tôi tổng hợp giúp.</div>"
        )
        ok = await self._c.ado.add_pull_request_comment(repo_id, pr_id, note)
        if ok and self._repo is not None:
            for r in reviewers:
                await self._repo.mark_reminded(pr_id, str(r["id"]))
        if ok:
            metrics.record_reminder(len(reviewers))
            await self._broadcast_reminder(repo_name, pr, reviewers, hours)
        self._log.info(
            "reviewer reminder sent", pr=pr_id, repo=repo_name,
            reviewers=[r.get("displayName") for r in reviewers], ok=ok,
        )

    async def _broadcast_reminder(
        self, repo_name: str, pr: dict, reviewers: list[dict], hours: int
    ) -> None:
        """Push the reminder to the configured notification channels (Teams/Email/Zalo)
        with an "Open PR" button. Best-effort — a channel failure never affects the flow."""
        channels = getattr(self._c, "channels", None)
        if not channels:
            return
        pr_id = pr.get("pullRequestId") or 0
        who = ", ".join(r.get("displayName") or r.get("uniqueName") or "?" for r in reviewers)
        item = WorkItemInfo(
            id=pr_id,
            title=(
                f"“{pr.get('title') or 'PR'}” chờ review — {who} chưa vote sau {hours}h"
            ),
            work_item_type="PullRequest",
            category=TaskCategory.UNKNOWN,
        )
        actions: list[tuple[str, str]] = []
        url = self._pr_web_url(repo_name, pr_id)
        if url:
            actions.append(("🔗 Mở PR", url))
        msg = NotificationMessage(
            work_item=item, type=NotificationType.REMINDER, actions=actions
        )
        for ch in channels:
            if not getattr(ch, "is_enabled", False):
                continue
            with contextlib.suppress(Exception):
                await ch.send(msg)

    def _pr_web_url(self, repo_name: str, pr_id: int) -> str:
        """Browser URL of a PR (for notification action buttons)."""
        cfg = self._config
        org = cfg.ado_organization.rstrip("/")
        project = quote(cfg.code_project or cfg.ado_project, safe="")
        if not (org and repo_name and pr_id):
            return ""
        return f"{org}/{project}/_git/{quote(repo_name, safe='')}/pullrequest/{pr_id}"
