"""ORM entities (ported from ``ExecutionRecord`` / ``AutopilotDbContext``)."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, Index, Integer, String, Text
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
    skill_used: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus, native_enum=False, length=20)
    )
    branch_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    files_changed: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    output: Mapped[str | None] = mapped_column(String(5000), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    cost_tokens: Mapped[int] = mapped_column(Integer, default=0)
