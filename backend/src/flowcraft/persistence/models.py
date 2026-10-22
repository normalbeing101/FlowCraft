"""SQLAlchemy models. Workspace-scoped for isolation from day one."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"        # human rejected an approval gate
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class StepStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"          # branch not taken
    BLOCKED_GUARDED = "blocked_guarded"  # retry refused by idempotency guard
    AWAITING_APPROVAL = "awaiting_approval"


class WorkflowRecord(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("workspace_id", "workflow_id", "version", name="uq_wf_version"),
    )

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    workflow_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict] = mapped_column(JSON)  # canonical parsed workflow dict
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunRecord(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_run_idem"),
        Index("ix_runs_wf_status", "workflow_id", "status"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    workflow_id: Mapped[str] = mapped_column(String(64), index=True)
    workflow_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.PENDING.value)
    inputs: Mapped[dict | None] = mapped_column(JSON)     # redacted copy
    outputs: Mapped[dict | None] = mapped_column(JSON)    # redacted final context
    error: Mapped[dict | None] = mapped_column(JSON)      # redacted failure info
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_step: Mapped[str | None] = mapped_column(String(64))
    approval_payload: Mapped[dict | None] = mapped_column(JSON)

    steps: Mapped[list["StepRecord"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="StepRecord.pk")


class StepRecord(Base):
    __tablename__ = "steps"

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"),
                                        index=True)
    step_id: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=StepStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    attempt_log: Mapped[list | None] = mapped_column(JSON)  # per-attempt errors (redacted)
    input: Mapped[dict | None] = mapped_column(JSON)        # redacted resolved input
    output: Mapped[dict | None] = mapped_column(JSON)       # redacted validated output
    error: Mapped[dict | None] = mapped_column(JSON)
    failure_class: Mapped[str | None] = mapped_column(String(32))
    provider_meta: Mapped[dict | None] = mapped_column(JSON)  # model/usage metadata
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)

    run: Mapped[RunRecord] = relationship(back_populates="steps")


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"),
                                        index=True)
    step_id: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(128))
    approved: Mapped[bool | None] = mapped_column()
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
