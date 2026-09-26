"""ORM entities (ported from ``ExecutionRecord`` / ``AutopilotDbContext``)."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ExecutionStatus(enum.Enum):
    PENDING = "Pending"
    RUNNING = "Running"
    SUCCESS = "Success"
    FAILED = "Failed"
    RETRYING = "Retrying"


class PipelineState(enum.Enum):
    """Per-work-item pipeline stage. Values match the board column names."""

    QUEUED = "Queued"
    IN_PROGRESS = "In progress"
    IN_REVIEW = "In review"
    NEEDS_HUMAN = "Needs human"
    DONE = "Done"
    FAILED = "Failed"


class WorkItemState(Base):
    """Authoritative pipeline state per work item (survives restarts → resume)."""

    __tablename__ = "work_item_states"

    work_item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    state: Mapped[PipelineState] = mapped_column(
        Enum(PipelineState, native_enum=False, length=20), default=PipelineState.QUEUED
    )
    detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class WorkItemStateHistory(Base):
    """One row per observed ADO **state transition** — the raw material for lead time,
    cycle time and the cumulative-flow chart on the Delivery page.

    Why a table rather than deriving it from ADO on demand: ``System.ChangedDate`` is
    bumped by ANY edit (a comment, a tag, an assignment), so "how long has this been in
    review?" computed from it is not just imprecise — it is systematically optimistic,
    which hides exactly the stuck items a PM is looking for. ADO's revisions API has the
    truth but costs one request per work item. Recording transitions as we already see
    them costs nothing extra and is exact from the moment it is switched on.

    Consequence worth stating plainly: **there is no history before the first run of the
    recorder.** Flow and lead-time figures fill in over the following days rather than
    appearing complete on day one.

    ``category`` is the ADO *state category* (Proposed / InProgress / Resolved /
    Completed / Removed) captured alongside the state name. Storing it here rather than
    resolving it at read time means a chart built months later still groups by what the
    process template said AT THE TIME, and does not silently rewrite history when
    someone renames a state or the API is unreachable.
    """

    __tablename__ = "work_item_state_history"
    __table_args__ = (
        # The two access patterns: "this item's timeline" and "everything in a window".
        Index("ix_wi_history_item_at", "work_item_id", "entered_at"),
        Index("ix_wi_history_at", "entered_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_item_id: Mapped[int] = mapped_column(Integer)
    project: Mapped[str] = mapped_column(String(200), default="")
    state: Mapped[str] = mapped_column(String(100), default="")
    category: Mapped[str] = mapped_column(String(40), default="")
    assigned_to: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    entered_at: Mapped[datetime] = mapped_column(DateTime)


class SdlcLoopState(Base):
    """Per-item progress of the closed-loop SDLC engine — resumable across restarts.

    Separate from ``work_item_states`` (which stays the coarse board state) so a
    crash mid-loop resumes at the exact ``(stage_index, iterations)``. A NEW table
    (not extra columns) because ``create_all`` won't ALTER an existing one.
    """

    __tablename__ = "sdlc_loop_states"

    work_item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile: Mapped[str] = mapped_column(String(64), default="")   # resolved profile name
    stage_index: Mapped[int] = mapped_column(Integer, default=0)   # cursor into the stage list
    iterations: Mapped[int] = mapped_column(Integer, default=0)    # SHARED revise counter
    branch: Mapped[str] = mapped_column(String(200), default="")   # item's feature branch
    signals_json: Mapped[str] = mapped_column(Text, default="")    # serialized StageSignals
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class PlannedRun(Base):
    """A batch of work items the Planning workbench scheduled to Start at ``run_at``.

    Persisted so a scheduled run survives restarts. The poller sweeps due rows each
    cycle and applies Start (trigger tag + state) to their items."""

    __tablename__ = "planned_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_ids: Mapped[str] = mapped_column(Text, default="")   # JSON array of work-item ids
    run_at: Mapped[datetime] = mapped_column(DateTime)         # local wall-clock time to fire
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|done|cancelled
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)


class AiConflict(Base):
    """A hidden code-conflict the Planning workbench's Analyze confirmed via an AI
    judge (two items likely touch the same files). Persisted so the poller can feed
    it back into scheduling as a Related soft-conflict — the autopilot then avoids
    running the pair concurrently even though the BA never linked them.

    Key is the ordered pair ``(a_id < b_id)`` so the same conflict upserts once."""

    __tablename__ = "ai_conflicts"

    a_id: Mapped[int] = mapped_column(Integer, primary_key=True)   # always < b_id
    b_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    score: Mapped[int] = mapped_column(Integer, default=0)          # 0–100 likelihood
    modules: Mapped[str] = mapped_column(Text, default="")          # JSON array
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)


class SchedulerDecision(Base):
    """One dependency-scheduler decision worth keeping (a cycle that deferred work),
    persisted so the Planning dashboard can show the recent trend across restarts.

    Bounded: the repository prunes to ``scheduler_history_limit`` newest rows."""

    __tablename__ = "scheduler_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime)                 # decision time (UTC)
    candidates: Mapped[int] = mapped_column(Integer, default=0)
    ready_ids: Mapped[str] = mapped_column(Text, default="")       # JSON array of ids
    deferred_json: Mapped[str] = mapped_column(Text, default="")   # JSON [{id,title,reason}]


class MergedPr(Base):
    """PR ids the state-sync already transitioned on merge — persisted so a restart
    doesn't re-apply ``on_merge_state`` to items that have since moved on (the cause
    of items bouncing back from a later state to the merge state)."""

    __tablename__ = "merged_prs"

    pr_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(100), default="")  # state applied / seen
    created_at: Mapped[datetime] = mapped_column(DateTime)


class SyncMarker(Base):
    """A single named watermark the state-sync must not forget across a restart.

    ``last_deploy_build`` is the one that matters: the deploy stage advances items only
    on a build NEWER than the last it saw, and it baselines itself on first sight so a
    fresh install does not transition a month of old builds. Held only in memory, that
    baseline was re-taken on every restart — so a deploy that succeeded while the
    autopilot was down (or that triggered the restart) was swallowed, and the items it
    shipped sat in their merge state until some later deploy happened to pass by."""

    __tablename__ = "sync_markers"

    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime)


class HeldNotification(Base):
    """A notice raised outside the notification window, waiting for it to open.

    Persisted rather than kept in memory: the whole point is that it survives the night,
    and a restart at 03:00 must not be what makes the morning summary lie.
    """

    __tablename__ = "held_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), default="")     # started / completed / …
    work_item_id: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(500), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime)


class AlertState(Base):
    """What has already been said about one problem, so it is not said again daily.

    The row is the alert's LIFE, not an event log: one row per (kind, work item), updated
    in place. That is the whole point — a table of occurrences would answer "how often did
    we mention it", and the question the digest needs answered is "have we mentioned it,
    and has it got worse since".

    ``last_age_hours`` is what makes escalation possible without a second table: an item
    reported at 26 hours and now sitting at 100 is materially different news, while the
    same item at 27 hours is the same news. ``acked_at`` and ``snoozed_until`` are the two
    ways a human says "I know" — the first permanently (until the alert clears and
    returns), the second until a date.
    """

    __tablename__ = "alert_states"
    __table_args__ = (
        UniqueConstraint("kind", "work_item_id", name="uq_alert_kind_item"),
        Index("ix_alert_states_snoozed_until", "snoozed_until"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), default="")       # delivery.KIND_*
    work_item_id: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(500), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Age (hours) at the moment we last reported it — the baseline escalation is measured
    # against. Float because an alert can legitimately fire under an hour old.
    last_age_hours: Mapped[float] = mapped_column(Float, default=0.0)
    notify_count: Mapped[int] = mapped_column(Integer, default=0)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acked_by: Mapped[str] = mapped_column(String(200), default="")
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Cleared when the underlying condition goes away, so the SAME problem recurring next
    # month is new news rather than something we think we already reported.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SpecDrift(Base):
    """One place a run's code stopped agreeing with the work item that described it.

    Persisted rather than left in the ADO comment because the comment answers "was this
    reported"; this answers the questions the BA actually works from — what is still
    outstanding, on which items, of what kind — and holds the tick-off (``resolved_at``)
    that says the specification has been brought back in line.
    """

    __tablename__ = "spec_drifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_item_id: Mapped[int] = mapped_column(Integer, index=True)
    project: Mapped[str] = mapped_column(String(200), default="")
    title: Mapped[str] = mapped_column(String(500), default="")
    pr_url: Mapped[str] = mapped_column(String(500), default="")
    kind: Mapped[str] = mapped_column(String(40), default="assumption")
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    where: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str] = mapped_column(String(200), default="")


class PrCommandState(Base):
    """PR babysitter memory per work item: how much of the revision budget /ai
    commands have spent — persisted so a restart neither resets the cap (runaway
    churn) nor blocks items that had headroom left."""

    __tablename__ = "pr_command_states"

    work_item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revisions: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class HandledPrComment(Base):
    """PR comments the babysitter already dispatched — the restart-proof twin of its
    in-memory set, closing the gap where a command was dispatched but the bot-signed
    reply (the other durable mark) never got posted.

    Keyed by THREAD as well as comment, because an ADO comment id is an ordinal within
    its thread, not a PR-wide id: every thread starts again at 1. Keying on
    ``(pr_id, comment_id)`` therefore made the first comment of every new thread collide
    with the first comment of the oldest one — a PR that had once handled comments
    1/4/7/9 silently swallowed the next four threads whose command landed at those
    ordinals. Silently: no reply, no log, indistinguishable from the bot being down.

    A new table rather than a third key column on the old one — ``create_all`` adds
    missing tables but never alters existing ones, so a renamed table is the migration.
    Losing the old rows is harmless and in fact desirable: a command the bot really did
    answer is still marked by its bot-signed reply (see ``command_threads``), so only the
    wrongly-swallowed ones come back."""

    __tablename__ = "handled_pr_thread_comments"

    pr_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class PrReviewBudget(Base):
    """How much review effort one PR has already consumed.

    Separate from ``PrCommandState`` (which counts CODE revisions per work item) because
    these are per-PR and bound *reviews*, which change nothing and so were previously
    unbounded: ``advisory_runs`` is scoped to ``commit_id`` and resets when the branch
    moves (re-reviewing the same commit yields the same findings), while ``auto_reviews``
    counts the PR's whole life.

    A new table rather than columns on an existing one: ``create_all`` adds missing tables
    to an existing database but will not ALTER one, so this upgrades in place.
    """

    __tablename__ = "pr_review_budgets"

    pr_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    commit_id: Mapped[str] = mapped_column(String(64), default="")
    advisory_runs: Mapped[int] = mapped_column(Integer, default=0)
    auto_reviews: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class PrReviewerState(Base):
    """One reviewer on one active PR, as last seen by the reviewer tracker.

    The tracker diffs the live ADO reviewer list against these rows each poll to
    detect *added* reviewers (→ auto-review when it's the bot), vote changes, and
    stale reviewers due a reminder. Persisted so a restart neither re-reviews a PR
    the bot already voted on nor re-sends reminders."""

    __tablename__ = "pr_reviewer_states"

    pr_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reviewer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo_id: Mapped[str] = mapped_column(String(64), default="")
    display_name: Mapped[str] = mapped_column(String(200), default="")
    unique_name: Mapped[str] = mapped_column(String(200), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    vote: Mapped[int] = mapped_column(Integer, default=0)  # ADO scale: -10..10
    added_at: Mapped[datetime] = mapped_column(DateTime)   # when the tracker first saw them
    last_vote_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Bot only: the PR source commit the last auto-review covered — a new commit
    # re-arms the auto-review (fresh iteration → fresh look).
    reviewed_commit: Mapped[str] = mapped_column(String(64), default="")
    # When that auto-review attempt (success or failure) completed — distinct from
    # updated_at, which also moves on every routine reviewer-list poll (every ~30s)
    # and so can't answer "how many auto-reviews happened in the last 24h".
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ClaudeSession(Base):
    """The Claude Agent SDK ``session_id`` last used for a branch, so a follow-up
    revise can RESUME that conversation instead of starting cold — the agent keeps
    the files it read and decisions it made across ``/ai`` rounds. Keyed by
    ``(repo, branch)``; refreshed each run and honoured only within a TTL."""

    __tablename__ = "claude_sessions"

    repo: Mapped[str] = mapped_column(String(200), primary_key=True)
    branch: Mapped[str] = mapped_column(String(200), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class TeamsConversation(Base):
    """A Teams conversation (channel/chat) the bot has been added to, serialized as
    the Agents SDK's own ``Conversation`` JSON — persisted so the bot can proactively
    message it later (e.g. the daily digest) even after a restart. ``MemoryStorage``
    (the SDK's default) would lose every entry on restart, defeating the point of a
    recurring digest."""

    __tablename__ = "teams_conversations"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class AuditEvent(Base):
    """One consequential action, for the audit trail: who did what to which target,
    from which surface. Append-only — nothing in the app updates or deletes rows."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_at", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(200), default="")   # email / "dashboard" / "system"
    source: Mapped[str] = mapped_column(String(50), default="")   # teams | dashboard | poller
    action: Mapped[str] = mapped_column(String(100), default="")  # e.g. "item.resumed"
    target: Mapped[str] = mapped_column(String(300), default="")  # item id / PR / config keys
    detail: Mapped[str] = mapped_column(String(2000), default="")


class FleetWorker(Base):
    """One worker machine, as last seen by the central VM.

    The row is the machine's CURRENT state, updated in place on every heartbeat — not a
    log of beats. The question the fleet page answers is "what is this machine doing and
    is its configuration current", and a table of beats would answer "how often did it
    call home" while making the useful query a group-by.

    ``running`` is JSON rather than a child table for the same reason: it is a snapshot
    that is replaced wholesale every beat and never queried across machines. The durable
    record of what ran lives on the worker's own ``executions`` table.
    """

    __tablename__ = "fleet_workers"
    __table_args__ = (Index("ix_fleet_workers_last_seen", "last_seen"),)

    # The machine's own name (hostname by default) — natural key, so a machine that
    # restarts or moves IP keeps its history rather than appearing as a second host.
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(200), default="")
    version: Mapped[str] = mapped_column(String(40), default="")
    profile: Mapped[str] = mapped_column(String(80), default="")      # the role it runs
    tags: Mapped[str] = mapped_column(Text, default="[]")             # JSON: its own tags
    # Hash of the central document the worker last applied, next to the hash the central
    # served at that moment: equal = in sync, different = it has not caught up yet.
    config_hash: Mapped[str] = mapped_column(String(64), default="")
    central_hash: Mapped[str] = mapped_column(String(64), default="")
    config_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    running: Mapped[str] = mapped_column(Text, default="[]")          # JSON snapshot
    done_today: Mapped[int] = mapped_column(Integer, default=0)
    failed_today: Mapped[int] = mapped_column(Integer, default=0)


class FleetKnowledge(Base):
    """One piece of knowledge the fleet has pooled, as held by the CENTRAL.

    Workers learn alone. Three machines running the same codebase used to have to make
    the same mistake three times before all three knew about it, and the central — the
    one screen anybody looks at — could not see any of it.

    Rows are keyed by the NORMALISED text, so the same lesson arriving from three
    machines is one row that knows it came from three. That count is the whole point:
    one machine tripping over something is an anecdote, three machines tripping over it
    is a rule, and the difference is what decides whether it gets handed back out.

    Nothing is redistributed until it is ``approved``. A worker that learned something
    wrong would otherwise poison every other machine on the next beat, and a bad lesson
    is re-taught on every run — the blast radius of auto-merge here is the whole fleet.

    A new table rather than columns elsewhere: ``create_all`` adds missing tables to an
    existing database but will not ALTER one, so this upgrades in place.
    """

    __tablename__ = "fleet_knowledge"
    __table_args__ = (Index("ix_fleet_knowledge_status", "status"),)

    #: Normalised text (see ``lessons.normalize``) — the natural key, so the same
    #: lesson from two machines merges instead of appearing twice.
    key: Mapped[str] = mapped_column(String(500), primary_key=True)
    #: The text as it will be handed out, in the wording of whoever said it best.
    text: Mapped[str] = mapped_column(Text, default="")
    #: Which repo it belongs to, or the shared bucket for workspace-wide knowledge.
    repo: Mapped[str] = mapped_column(String(200), default="")
    #: ``draft`` (waiting for a human), ``approved`` (served to the fleet), ``rejected``
    #: (kept, so the same wrong lesson is not re-queued on every beat by every machine).
    status: Mapped[str] = mapped_column(String(20), default="draft")
    #: JSON list of machine names that reported it — length is the "how many machines
    #: independently hit this" signal that auto-promotion reads.
    origins: Mapped[str] = mapped_column(Text, default="[]")
    #: Total occurrences summed across machines (each machine counts its own repeats).
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    #: ``authored`` when a human typed it on some machine, else ``learned``. A rule a
    #: person wrote does not need three machines to agree before it is worth sharing.
    source: Mapped[str] = mapped_column(String(20), default="learned")
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)


class QualityKind:
    """``QualityEvent.kind`` values. Plain strings, not an ``Enum`` column: this is an
    append-only analytics log that will outlive today's vocabulary, and a new kind must
    never invalidate rows already written."""

    EXECUTION_RETRY = "execution_retry"   # value = attempt number
    PR_REVISION = "pr_revision"           # value = /ai revise round on the PR
    SDLC_ITERATION = "sdlc_iteration"     # value = SDLC revise round; stage = stage name
    REVIEW_VOTE = "review_vote"           # value = ADO vote -10..10; actor = reviewer
    REVIEW_FINDING = "review_finding"     # value = finding count; detail = the findings
    TEST_FAILED = "test_failed"           # detail = test summary
    REOPENED = "reopened"                 # a human dragged the item back to a trigger state

    #: Kinds that mean "this item had to be redone" — the rework tally.
    REWORK = (EXECUTION_RETRY, PR_REVISION, SDLC_ITERATION, REOPENED)


class QualityEvent(Base):
    """One durable data point about how much rework a work item needed.

    Append-only and never reset. Every counter this draws from is a *budget* built to
    stop runaway loops, not to measure: ``pr_command_states.revisions`` is zeroed when
    the PR closes, ``sdlc_loop_states`` rows are deleted on success, ``PrReviewerState``
    keeps only the CURRENT vote, and ``RetryPolicy`` lives in a dict that a restart
    empties. Each is cleared at precisely the moment the number finally means something
    — so the answer to "how many times did #123 get sent back" was unrecoverable.
    Events are written at those same moments, before the clearing, and kept forever.

    A new table rather than columns on ``executions``: ``create_all`` adds missing
    tables to an existing database but will not ALTER one, so this upgrades in place.
    """

    __tablename__ = "quality_events"
    __table_args__ = (
        Index("ix_quality_events_at", "at"),
        Index("ix_quality_events_work_item_id", "work_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime)
    work_item_id: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(64), default="")     # see QualityKind
    stage: Mapped[str] = mapped_column(String(64), default="")    # SDLC stage, when known
    value: Mapped[int] = mapped_column(Integer, default=0)        # meaning depends on kind
    actor: Mapped[str] = mapped_column(String(200), default="")   # reviewer / "autopilot"
    pr_id: Mapped[int] = mapped_column(Integer, default=0)        # 0 when not PR-scoped
    detail: Mapped[str] = mapped_column(String(2000), default="")


class ExecutionRecord(Base):
    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_work_item_id", "work_item_id"),
        Index("ix_executions_started_at", "started_at"),
        Index("ix_executions_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_item_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500), default="")
    # The item's ADO project, so History/Overview/Analytics can be scoped to a
    # workspace. Blank on rows written before this column existed — those are shown
    # only in the unscoped view rather than attributed to an arbitrary workspace.
    project: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(50), default="")
    # Which trigger tag matched this item (for dashboard filtering). NULL on rows
    # created before this column existed → only shown under the "All" filter.
    trigger_tag: Mapped[str | None] = mapped_column(String(100), nullable=True)
    skill_used: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus, native_enum=False, length=20)
    )
    branch_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # primary PR
    pr_urls: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array of every PR
    files_changed: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    output: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    # The QC verdict, as numbers. It existed only inside a rendered ADO comment, so
    # "which items have a failing test case right now" was a question nothing could
    # answer — not the dashboard, not a report, not an alert. A finding you cannot
    # query is a finding nobody acts on.
    tests_total: Mapped[int] = mapped_column(Integer, default=0)
    tests_failed: Mapped[int] = mapped_column(Integer, default=0)
    tests_blocked: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Cost detail. All nullable: rows written before these columns existed genuinely do
    # not know, and a 0 would be read as "this run was free" — the one wrong answer a
    # cost table must never give. The UI renders None as an em dash, not as a zero.
    model_used: Mapped[str | None] = mapped_column(String(120), nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_creation_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Lessons the learning loop injected into this run's brief. NULL on rows written
    # before the column existed → rendered as "no badge", never as a zero claim.
    lessons_injected: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    # WHICH ROLE this run is: the SDLC profile the item resolved to (dev, qc, full…).
    # ``skill_used`` cannot answer it — for the default execution mode it is
    # "interactive:<session id>", which names the console and not the work — so the one
    # question an operator watching a live run asks first, "is this reviewing or is it
    # building the whole thing", had no answer anywhere on the page. Stored rather than
    # re-derived because by then the item has moved to its WORKING state, and resolving
    # the role from that gives a different answer than the run was actually given.
    # NULL on rows written before this column, and on runs with no relay wiring at all.
    profile: Mapped[str | None] = mapped_column(String(100), nullable=True)


class LoopReport(Base):
    """One run of a scheduled REPORT loop — an audit, not a build.

    Kept apart from ``executions`` on purpose. An execution row answers "did this run
    succeed, and what did it change"; the answer is a status, a branch and a PR, and its
    ``output`` is a ``String(5000)`` summary because that is all a diff needs saying
    about it. A report has no diff: the text IS the deliverable, it is routinely longer
    than that column, and it is read months later by someone asking what we knew back
    then. Findings also need counting by severity, which a prose column cannot do.
    """

    __tablename__ = "loop_reports"
    __table_args__ = (
        Index("ix_loop_reports_loop_name", "loop_name"),
        Index("ix_loop_reports_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    loop_name: Mapped[str] = mapped_column(String(200), default="")
    project: Mapped[str] = mapped_column(String(200), default="")
    repo: Mapped[str] = mapped_column(String(500), default="")
    # "success" | "failed" — a failed audit is still a row: "the nightly review did not
    # run" is exactly the kind of silence an operator needs to see on the page.
    status: Mapped[str] = mapped_column(String(20), default="success")
    summary: Mapped[str] = mapped_column(Text, default="")
    # The agent's answer, whole and untruncated. Text, not String(n) — see the docstring.
    body_md: Mapped[str] = mapped_column(Text, default="")
    findings_json: Mapped[str] = mapped_column(Text, default="[]")
    agents: Mapped[str] = mapped_column(String(500), default="")   # comma-separated
    # Counts are stored rather than derived so the LIST page can render severity chips
    # without parsing every report's JSON — the list is the page that is loaded most.
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    high_count: Mapped[int] = mapped_column(Integer, default=0)
    medium_count: Mapped[int] = mapped_column(Integer, default=0)
    low_count: Mapped[int] = mapped_column(Integer, default=0)
    info_count: Mapped[int] = mapped_column(Integer, default=0)
    html_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PrConflict(Base):
    """A pull request whose merge into its target is blocked by conflicts.

    One row per PR, kept after it is resolved: "how long did PRs sit unmergeable, and
    who fixed them" is a question about history, and the row is also what makes every
    side effect happen ONCE — the PR comment, the notification, and the resolution
    attempt against a given target commit.
    """

    __tablename__ = "pr_conflicts"
    __table_args__ = (
        UniqueConstraint("repo_id", "pr_id", name="uq_pr_conflicts_repo_pr"),
        Index("ix_pr_conflicts_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[str] = mapped_column(String(100), default="")
    repo_name: Mapped[str] = mapped_column(String(200), default="")
    pr_id: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(400), default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    url: Mapped[str] = mapped_column(String(600), default="")
    source_branch: Mapped[str] = mapped_column(String(300), default="")
    target_branch: Mapped[str] = mapped_column(String(300), default="")
    work_item_id: Mapped[int] = mapped_column(Integer, default=0)
    owned: Mapped[bool] = mapped_column(Boolean, default=False)   # bot branch prefix
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    # open | resolving | escalated | resolved | closed — see ai_autopilot.pr_conflicts
    status: Mapped[str] = mapped_column(String(20), default="open")
    files_json: Mapped[str] = mapped_column(Text, default="[]")
    first_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str] = mapped_column(String(40), default="")  # agent|clean|external
    # The target commit ADO last test-merged against, and the one the last attempt ran
    # on: equal means "already tried these exact inputs".
    target_commit: Mapped[str] = mapped_column(String(64), default="")
    attempt_target: Mapped[str] = mapped_column(String(64), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    checks_json: Mapped[str] = mapped_column(Text, default="{}")
    merge_commit: Mapped[str] = mapped_column(String(64), default="")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    # The /resolve comment already answered, as "thread:comment" — so a restart never
    # runs the same request twice.
    handled_command: Mapped[str] = mapped_column(String(60), default="")
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0)


class SecurityScan(Base):
    """One run of the security scanner — from the CLI, a scan loop, the pre-PR gate or
    the Security page. The trend chart is drawn from these rows, which is why counts are
    stored: "how many open highs did we have on the 1st" must not need every finding of
    every scan reparsed."""

    __tablename__ = "security_scans"
    __table_args__ = (
        Index("ix_security_scans_repo", "repo"),
        Index("ix_security_scans_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String(500), default="")
    project: Mapped[str] = mapped_column(String(200), default="")
    trigger: Mapped[str] = mapped_column(String(30), default="cli")  # cli|loop|pr-gate|dashboard
    loop_name: Mapped[str] = mapped_column(String(200), default="")
    branch: Mapped[str] = mapped_column(String(300), default="")
    scope: Mapped[str] = mapped_column(String(20), default="full")   # full | diff
    ai_mode: Mapped[str] = mapped_column(String(10), default="off")
    tools_json: Mapped[str] = mapped_column(Text, default="{}")      # {name: status label}
    status: Mapped[str] = mapped_column(String(20), default="success")
    # Totals of what this scan REPORTED (after suppression), by severity …
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    high_count: Mapped[int] = mapped_column(Integer, default=0)
    medium_count: Mapped[int] = mapped_column(Integer, default=0)
    low_count: Mapped[int] = mapped_column(Integer, default=0)
    info_count: Mapped[int] = mapped_column(Integer, default=0)
    # … and how it compared with the baseline.
    new_count: Mapped[int] = mapped_column(Integer, default=0)
    fixed_count: Mapped[int] = mapped_column(Integer, default=0)
    suppressed_count: Mapped[int] = mapped_column(Integer, default=0)
    gate_passed: Mapped[bool] = mapped_column(Boolean, default=True)
    fail_on: Mapped[str] = mapped_column(String(10), default="high")
    report_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # loop_reports.id
    # Which fingerprints this scan found new / fixed — so the scan's own page can list
    # them without re-deriving from timestamps (which lie across overlapping scans).
    new_json: Mapped[str] = mapped_column(Text, default="[]")
    fixed_json: Mapped[str] = mapped_column(Text, default="[]")
    filtered_count: Mapped[int] = mapped_column(Integer, default=0)   # dropped by rule/path ignore
    html_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SecurityFinding(Base):
    """A security finding with a LIFE — first seen, last seen, fixed, suppressed, filed.

    One row per (repo, fingerprint). A report loop stores what one run said; this stores
    what is true about the code now, updated by every scan: still there → ``last_seen``
    moves, gone from a full scan → ``fixed``, back again → reopened. That is what lets
    the page answer "what is new since yesterday" and the gate fail only on that.
    """

    __tablename__ = "security_findings"
    __table_args__ = (
        UniqueConstraint("repo", "fingerprint", name="uq_security_findings_repo_fp"),
        Index("ix_security_findings_status", "status"),
        Index("ix_security_findings_severity", "severity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String(500), default="")
    project: Mapped[str] = mapped_column(String(200), default="")
    fingerprint: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(30), default="")
    rule_id: Mapped[str] = mapped_column(String(200), default="")
    severity: Mapped[str] = mapped_column(String(10), default="info")
    cwe: Mapped[str] = mapped_column(String(20), default="")
    owasp: Mapped[str] = mapped_column(String(20), default="")
    confidence: Mapped[str] = mapped_column(String(10), default="")
    file: Mapped[str] = mapped_column(String(1000), default="")
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    snippet: Mapped[str] = mapped_column(String(600), default="")
    agent: Mapped[str] = mapped_column(String(200), default="")
    # open | fixed | suppressed | false_positive
    status: Mapped[str] = mapped_column(String(20), default="open")
    suppress_reason: Mapped[str] = mapped_column(String(1000), default="")
    suppress_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    suppressed_by: Mapped[str] = mapped_column(String(200), default="")
    ado_bug_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 3: PoC verification outcome. None = not attempted.
    verified: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    poc_md: Mapped[str] = mapped_column(Text, default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime)
    last_seen: Mapped[datetime] = mapped_column(DateTime)
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_scan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    times_seen: Mapped[int] = mapped_column(Integer, default=1)
