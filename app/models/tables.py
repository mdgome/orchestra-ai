"""SQLAlchemy 2.x ORM tables (PostgreSQL). Mapping only; no behaviour."""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, CheckConstraint, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, LargeBinary, String,
    Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.repositories.tracked_sql import ANOMALY_SQL, U_SQL
from app.validation import policies as P


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


class Project(Base):
    """A registered workspace that groups tasks. Owns the write lock and the workspace baselines."""
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_key: Mapped[str] = mapped_column(Text, nullable=False)     # realpath + normcase, unique
    repository_url: Mapped[str | None] = mapped_column(Text)            # credentials stripped
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", server_default="ACTIVE", nullable=False)
    test_policy: Mapped[dict | None] = mapped_column(JSONB)
    write_owner_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", name="fk_projects_write_owner_task_id", use_alter=True))
    write_epoch: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    baseline_fingerprint: Mapped[str | None] = mapped_column(Text)
    write_baseline_fingerprint: Mapped[str | None] = mapped_column(Text)
    workspace_state: Mapped[str] = mapped_column(String(24), default="CLEAN", server_default="CLEAN", nullable=False)
    profile_version: Mapped[int | None] = mapped_column(Integer)         # the ACTIVE profile version (see project_profiles)
    row_version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    __table_args__ = (
        CheckConstraint("profile_version IS NULL OR profile_version > 0", name="ck_projects_profile_version"),
        # the active pointer can only name a version of THIS project (MATCH SIMPLE: NULL = no profile yet)
        ForeignKeyConstraint(["id", "profile_version"], ["project_profiles.project_id", "project_profiles.version"],
                             name="fk_projects_active_profile", use_alter=True),
        CheckConstraint("status IN ('ACTIVE','ARCHIVED')", name="ck_projects_status"),
        CheckConstraint("workspace_state IN ('CLEAN','DIRTY_UNRESOLVED')", name="ck_projects_workspace_state"),
        CheckConstraint("(write_owner_task_id IS NULL) = (write_baseline_fingerprint IS NULL)",
                        name="ck_projects_write_owner_baseline"),
        UniqueConstraint("workspace_key", name="uq_projects_workspace_key"),
    )


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", name="fk_tasks_project_id", ondelete="RESTRICT"), index=True)
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
    # The project profile as it was when the task was created: NULL (all three) = a task without a project (or one that
    # predates profiles), which keeps the original built-in rules.
    profile_version: Mapped[int | None] = mapped_column(Integer)
    profile_hash: Mapped[str | None] = mapped_column(String(64))
    # none_as_null: Python None must be SQL NULL (not the JSON value null), or the all-or-none CHECK would reject it
    profile_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    row_version: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    __table_args__ = (
        CheckConstraint(
            "(profile_version IS NULL AND profile_hash IS NULL AND profile_snapshot IS NULL) OR "
            "(project_id IS NOT NULL AND profile_version IS NOT NULL AND profile_hash IS NOT NULL "
            "AND profile_snapshot IS NOT NULL AND length(profile_hash) = 64)", name="ck_tasks_profile_all_or_none"),
        ForeignKeyConstraint(["project_id", "profile_version"], ["project_profiles.project_id", "project_profiles.version"],
                             name="fk_tasks_profile"),
    )


class ProjectProfile(Base):
    """One immutable version of a project's profile (a trigger rejects UPDATE/DELETE). A change is always a new row."""
    __tablename__ = "project_profiles"
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", name="fk_project_profiles_project_id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _now()
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_project_profiles_project_version"),
        CheckConstraint("version > 0", name="ck_project_profiles_version"),
        CheckConstraint("length(content_hash) = 64", name="ck_project_profiles_hash"),
    )


class TaskBaseline(Base):
    """What the workspace looked like when a task took the project write lock (one row per task, never overwritten).

    Only the manifest and bookkeeping are stored, never file content. A row is AVAILABLE (a usable manifest) or UNAVAILABLE
    (the reason code is stored and every payload column is NULL: partial data is never an authoritative baseline).
    `proof_epoch` is the project epoch when the fingerprint was measured (before acquisition), `acquired_epoch` the epoch the
    actual lock acquisition produced (proof_epoch + 1 for origin ACQUISITION), `recorded_epoch` the epoch when this row was
    written. A recovered row can have acquired_epoch NULL: the original acquisition epoch is then unknown and is not invented."""
    __tablename__ = "task_baselines"
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", name="fk_task_baselines_task_id"),
                                               primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", name="fk_task_baselines_project_id"), nullable=False)
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    proof_epoch: Mapped[int | None] = mapped_column(BigInteger)
    acquired_epoch: Mapped[int | None] = mapped_column(BigInteger)
    recorded_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    proof_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(8))
    head_kind: Mapped[str | None] = mapped_column(String(8))
    head_sha: Mapped[str | None] = mapped_column(String(64))
    manifest: Mapped[bytes | None] = mapped_column(LargeBinary)
    manifest_version: Mapped[str | None] = mapped_column(String(32))
    uncompressed_bytes: Mapped[int | None] = mapped_column(Integer)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    file_count: Mapped[int | None] = mapped_column(Integer)
    dirty_paths: Mapped[list | None] = mapped_column(JSONB(none_as_null=True))
    unavailable_reason: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = _now()
    __table_args__ = (
        CheckConstraint("status IN ('AVAILABLE','UNAVAILABLE')", name="ck_task_baselines_status"),
        CheckConstraint("origin IN ('ACQUISITION','REENTRY_RECOVERY')", name="ck_task_baselines_origin"),
        CheckConstraint("head_kind IS NULL OR head_kind IN ('COMMIT','UNBORN')", name="ck_task_baselines_head_kind"),
        CheckConstraint("(proof_epoch IS NULL OR proof_epoch >= 0) AND (acquired_epoch IS NULL OR acquired_epoch >= 1) "
                        "AND recorded_epoch >= 1", name="ck_task_baselines_epochs"),
        CheckConstraint("proof_fingerprint ~ '^WORKSPACE_FINGERPRINT_V[0-9]+:[0-9a-f]{64}$'",
                        name="ck_task_baselines_proof_fp"),
        CheckConstraint(
            "status <> 'AVAILABLE' OR ((mode IN ('GIT','CONTENT') AND manifest IS NOT NULL AND manifest_version IS NOT NULL "
            "AND manifest_sha256 IS NOT NULL AND length(manifest_sha256) = 64 AND uncompressed_bytes IS NOT NULL "
            "AND uncompressed_bytes >= 0 AND file_count IS NOT NULL AND file_count >= 0 AND dirty_paths IS NOT NULL "
            "AND jsonb_typeof(dirty_paths) = 'array' "
            "AND ((mode = 'GIT' AND head_kind = 'COMMIT' AND head_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$') "
            "OR (mode = 'GIT' AND head_kind = 'UNBORN' AND head_sha IS NULL) "
            "OR (mode = 'CONTENT' AND head_kind IS NULL AND head_sha IS NULL)) "
            "AND proof_epoch IS NOT NULL AND acquired_epoch IS NOT NULL AND acquired_epoch >= proof_epoch "
            "AND recorded_epoch >= acquired_epoch) IS TRUE)", name="ck_task_baselines_available"),
        CheckConstraint(
            "origin <> 'ACQUISITION' OR ((proof_epoch IS NOT NULL AND acquired_epoch = proof_epoch + 1 "
            "AND recorded_epoch = acquired_epoch) IS TRUE)", name="ck_task_baselines_acquisition"),
        CheckConstraint(
            "status <> 'UNAVAILABLE' OR (unavailable_reason IS NOT NULL AND manifest IS NULL AND manifest_version IS NULL "
            "AND manifest_sha256 IS NULL AND uncompressed_bytes IS NULL AND file_count IS NULL AND mode IS NULL "
            "AND head_kind IS NULL AND head_sha IS NULL AND dirty_paths IS NULL)", name="ck_task_baselines_unavailable"),
        CheckConstraint("status = 'UNAVAILABLE' OR unavailable_reason IS NULL", name="ck_task_baselines_reason_only_unavailable"),
        CheckConstraint(
            "unavailable_reason IS DISTINCT FROM 'BASELINE_MISSING_AT_REENTRY' OR (origin = 'REENTRY_RECOVERY' "
            "AND status = 'UNAVAILABLE' AND acquired_epoch IS NULL AND proof_epoch IS NULL)",
            name="ck_task_baselines_reentry_missing"),
    )


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
    __table_args__ = (
        # B2 (migration 0005): the partial predicates are the canonical raw SQL of app/repositories/tracked_sql.py, written as
        # literals (an index predicate cannot hold a bind); a test compares them with the frozen migration text.
        Index("ix_executions_tracked_unresolved", "task_id", postgresql_where=text(U_SQL)),
        Index("ix_executions_tracked_anomaly", "task_id", postgresql_where=text(ANOMALY_SQL)),
    )


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
        # At most one pending PROJECT_DIRTY recovery gate per task (back-stop for the row-lock check).
        Index("uq_approvals_project_dirty_pending", "task_id", unique=True,
              postgresql_where=text("status = 'PENDING' AND (target ->> 'reason_code') = 'PROJECT_DIRTY'")),
        # B2 (migration 0005): at most one direct successor per approval (the lineage lives in `target`, written once).
        Index("uq_approvals_lineage_successor", text("(target ->> 'supersedes_approval_id')"), unique=True,
              postgresql_where=text("(target ->> 'supersedes_approval_id') IS NOT NULL")),
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
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))    # fences a stale coroutine
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # persistent backoff
    defer_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
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


class RuntimeCapability(Base):
    """Process-tracking capability shared with API-only instances (design 6.2): one row per key (`process_tracking`)."""
    __tablename__ = "runtime_capabilities"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProcessRiskRequest(Base):
    """An operator's risk-acceptance or recheck request (design 7.5 item 4); the request id is the approval id.
    `target_execution_ids` is the final snapshot (sorted ascending); no foreign key points into `executions` on purpose."""
    __tablename__ = "process_risk_requests"
    approval_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("approvals.id", name="fk_process_risk_requests_approval_id"), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", name="fk_process_risk_requests_task_id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", name="fk_process_risk_requests_project_id"), nullable=False)
    dirty_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    target_execution_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    requested_by: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    claimed_by_incarnation: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claim_generation: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("kind IN ('ACCEPT','RECHECK')", name="ck_process_risk_requests_kind"),
        CheckConstraint("status IN ('REQUESTED','VERIFYING','APPLIED','REFUSED_ALIVE','RETRYABLE','NEEDS_REDECISION')",
                        name="ck_process_risk_requests_status"),
        # The absolute limit is fixed in the migration (a corruption/runaway guard); it never reads a runtime constant.
        CheckConstraint(f"cardinality(target_execution_ids) BETWEEN 1 AND {P.PROCESS_RISK_TARGETS_DB_LIMIT}",
                        name="ck_process_risk_requests_target_count"),
        CheckConstraint(f"requested_by IS NULL OR char_length(requested_by) BETWEEN 1 AND {P.REQUESTED_BY_MAX_LENGTH}",
                        name="ck_process_risk_requests_requested_by"),
        CheckConstraint(f"char_length(rationale) BETWEEN 1 AND {P.PROCESS_RISK_RATIONALE_MAX_LENGTH}",
                        name="ck_process_risk_requests_rationale"),
        Index("ix_process_risk_requests_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_process_risk_requests_project_finished", "project_id", "finished_at",
              postgresql_where=text("finished_at IS NOT NULL")),
        Index("uq_process_risk_recheck_active", "project_id", "dirty_epoch", unique=True,
              postgresql_where=text("kind = 'RECHECK' AND status IN ('REQUESTED','VERIFYING','RETRYABLE')")),
    )
