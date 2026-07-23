"""ORM entities (ported from ``ExecutionRecord`` / ``AutopilotDbContext``)."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, Index, Integer, String, Text
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


class PrCommandState(Base):
    """PR babysitter memory per work item: how much of the revision budget /ai
    commands have spent — persisted so a restart neither resets the cap (runaway
    churn) nor blocks items that had headroom left."""

    __tablename__ = "pr_command_states"

    work_item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revisions: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class HandledPrComment(Base):
    """PR comment ids the babysitter already dispatched — the restart-proof twin of
    its in-memory set, closing the gap where a command was dispatched but the
    bot-signed reply (the other durable mark) never got posted."""

    __tablename__ = "handled_pr_comments"

    pr_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comment_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)


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
    started_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0)
