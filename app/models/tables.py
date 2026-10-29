"""SQLAlchemy 2.x ORM tables (PostgreSQL). Mapping only; no behaviour."""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Conversation(Base):
    """Logical Conversation Session: minimal metadata only (no message content)."""
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    context_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    __table_args__ = (CheckConstraint("status IN ('ACTIVE','CLOSED','EXPIRED')", name="ck_conversations_status"),)


class Message(Base):
    """Message content, separated from session metadata. No FK points *to* this table."""
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("role IN ('USER','SYSTEM')", name="ck_messages_role"),)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    requirements: Mapped[str] = mapped_column(Text, nullable=False)
    requirements_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    limits: Mapped[dict] = mapped_column(JSONB, nullable=False)
    test_policy: Mapped[dict] = mapped_column(JSONB, nullable=False)
    test_policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_path: Mapped[str | None] = mapped_column(Text)
    plan_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    workflow_iteration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replan_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cli_invocations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    run_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    decisions_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    resume_step_key: Mapped[str | None] = mapped_column(String(32))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()


class TaskRun(Base):
    __tablename__ = "task_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    runner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finish_reason: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING','FINISHED','ABANDONED')", name="ck_task_runs_status"),
        # Final defence: at most one RUNNING run per task.
        Index("uq_task_runs_one_running", "task_id", unique=True, postgresql_where=text("status = 'RUNNING'")),
    )


class TaskStep(Base):
    __tablename__ = "task_steps"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    workflow_iteration: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str] = mapped_column(String(32), nullable=False)
    step_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str | None] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    output: Mapped[dict | None] = mapped_column(JSONB)
    snapshot_before: Mapped[dict | None] = mapped_column(JSONB)
    snapshot_after: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("task_id", "plan_revision", "workflow_iteration", "step_key", "step_attempt",
                         name="uq_task_steps_identity"),
    )


class Execution(Base):
    """One CLI invocation. STARTING is committed before the process is spawned."""
    __tablename__ = "executions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_steps.id"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    request_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    command_fingerprint: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    runner_id: Mapped[str | None] = mapped_column(String(128))
    pid: Mapped[int | None] = mapped_column(Integer)
    process_create_time: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stdout_tail: Mapped[str | None] = mapped_column(Text)
    stderr_tail: Mapped[str | None] = mapped_column(Text)
    discarded_result: Mapped[dict | None] = mapped_column(JSONB)
    detail: Mapped[dict | None] = mapped_column(JSONB)


class StateTransition(Base):
    """Append-only (a trigger in the migration rejects UPDATE/DELETE)."""
    __tablename__ = "state_transitions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="TASK_STATE", nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(128))
    event: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    step_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_steps.id"))
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    target: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    basis: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checkpoint_step_key: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    command_fingerprint: Mapped[str | None] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    __table_args__ = (
        CheckConstraint("type IN ('DESIGN','RECOVERY','PERMISSION')", name="ck_approvals_type"),
        CheckConstraint("status IN ('PENDING','DECIDED')", name="ck_approvals_status"),
    )


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    selected_option: Mapped[str | None] = mapped_column(String(64))
    rationale: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    basis: Mapped[dict] = mapped_column(JSONB, nullable=False)
    decided_at: Mapped[datetime] = _now()
    __table_args__ = (
        UniqueConstraint("approval_id", name="uq_approval_decisions_approval"),
        CheckConstraint("decision IN ('APPROVED','REJECTED')", name="ck_approval_decisions_decision"),
    )


class DesignDecision(Base):
    __tablename__ = "design_decisions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"), nullable=False, unique=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _now()


class ExecutionRequest(Base):
    __tablename__ = "execution_requests"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    runner_id: Mapped[str | None] = mapped_column(String(128))
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("approvals.id"), unique=True)
    note: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("kind IN ('START','RESUME')", name="ck_execution_requests_kind"),
        CheckConstraint("status IN ('PENDING','CLAIMED','DONE','NEEDS_REVIEW')", name="ck_execution_requests_status"),
    )


class AgentSession(Base):
    """Current native resume id per (task, role). native_session_id is internal metadata: never exposed."""
    __tablename__ = "agent_sessions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    native_session_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    __table_args__ = (
        UniqueConstraint("task_id", "agent_role", name="uq_agent_sessions_task_role"),
        CheckConstraint(
            "agent_role IN ('PLANNER','DESIGN_REVIEWER','DEVELOPER','TESTER','CODE_REVIEWER')",
            name="ck_agent_sessions_role"),
        CheckConstraint("provider IN ('CODEX','CLAUDE')", name="ck_agent_sessions_provider"),
        CheckConstraint("status IN ('ACTIVE','INVALID','CLOSED')", name="ck_agent_sessions_status"),
    )
