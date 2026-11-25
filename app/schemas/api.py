"""Request / response shapes. Shape and type only: every business rule lives in app/validation.

Responses deliberately omit native session ids and any column that could carry one.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from app.validation import policies as P


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- conversations ---------------------------------------------------------------------------------------
class ConversationCreate(BaseModel):
    ttl_seconds: int | None = None


class ConversationOut(_Out):
    id: uuid.UUID
    status: str
    created_at: datetime
    expires_at: datetime | None = None


class MessageCreate(BaseModel):
    role: str = "USER"
    content: str


class MessageOut(_Out):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    created_at: datetime


# --- projects ----------------------------------------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str
    workspace_path: str | None = None       # connect an existing local workspace ...
    repository_url: str | None = None       # ... or clone a git repository into the managed clone root
    test_policy: dict | None = None
    profile: dict | None = None             # omitted: the neutral profile (v1); validated, unknown keys are rejected


class ProjectPatch(BaseModel):
    name: str | None = None
    status: str | None = None
    test_policy: dict | None = None


class ProjectOut(_Out):
    id: uuid.UUID
    name: str
    workspace_path: str
    repository_url: str | None = None
    status: str
    test_policy: dict | None = None
    workspace_state: str
    profile_version: int | None = None      # the ACTIVE profile version
    created_at: datetime
    updated_at: datetime


class ProfileVersionOut(_Out):
    version: int
    content: dict
    content_hash: str
    created_at: datetime
    active: bool


class ProfileVersionResult(ProfileVersionOut):
    created: bool                           # false: identical to the active version, nothing was written


class ProfileVersionList(_Out):
    items: list[ProfileVersionOut]
    total: int
    limit: int
    offset: int


class SweeperStallOut(_Out):
    """The external stall view (14.2): the nine fields of `SweeperStallSnapshot`; the default object has every boolean false,
    every count 0 and every other field null. No SQL text, exception text or process identity is a field."""
    stalled: bool = False
    stalled_since: datetime | None = None
    pass_start_cursor: uuid.UUID | None = None
    first_failure_cursor: uuid.UUID | None = None
    current_failure_cursor: uuid.UUID | None = None
    consecutive_failures: int = 0
    last_failure_code: str | None = None
    last_failure_at: datetime | None = None
    next_retry_at: datetime | None = None


class SweeperConfigInvalidOut(_Out):
    """Only on the SWEEPER_CONFIG_INVALID diagnostics cause (never in /health or the report). `next_retry_at` is the audit value."""
    active: bool = False
    config_currently_valid: bool | None = None
    config_evaluated_at: datetime | None = None
    clearance_pending: bool = False
    detected_at: datetime | None = None
    trigger_position: uuid.UUID | None = None
    next_retry_at: datetime | None = None
    next_recheck_at: datetime | None = None
    reasons: list[str] = Field(default_factory=list)


class BackgroundComponentOut(_Out):
    """`reason_code` and `since` are null for a derived item (display only, table SUP-DS)."""
    component: str
    reason_code: str | None = None
    since: datetime | None = None
    restart_exhausted: bool = False
    display_state: str                # DEGRADED | BACKOFF_ORPHANED | EXITED_UNSUPERVISED | BACKOFF_WAITER_DONE_UNSUPERVISED


class TrackedAnomalyOut(_Out):
    execution_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    anomaly_kind: str
    sub_reason: str | None = None


class DiagnosticCauseOut(_Out):
    reason_code: str
    severity: str                     # FAIL | WARN
    blocks_execution: bool
    cause: str
    recommendation: str
    related_execution_ids: list[uuid.UUID] = Field(default_factory=list)
    related_request_ids: list[uuid.UUID] = Field(default_factory=list)
    anomalies: list[TrackedAnomalyOut] = Field(default_factory=list)
    anomalies_returned: int = 0
    anomalies_truncated: bool = False
    anomalies_total: int | None = None
    anomalies_total_is_exact: bool = False
    may_be_permanently_stuck: bool = False
    sweeper: SweeperStallOut | None = None
    config_invalid: SweeperConfigInvalidOut | None = None
    components: list[BackgroundComponentOut] = Field(default_factory=list)


class DiagnosticItemOut(_Out):
    code: str
    status: str                       # PASS | WARN | FAIL | UNKNOWN
    checked_at: datetime
    cause: str
    recommendation: str
    blocks_execution: bool
    causes: list[DiagnosticCauseOut] = Field(default_factory=list)
    blocks_features: list[str] = Field(default_factory=list)      # only PROCESS_TRACKING, only when not SUPPORTED

    @model_serializer(mode="wrap")
    def _omit_empty_blocks_features(self, handler):
        """`blocks_features` is present only when it names a feature (6.2): a SUPPORTED item carries no such key."""
        data = handler(self)
        if not data.get("blocks_features"):
            data.pop("blocks_features", None)
        return data


class DiagnosticsOut(_Out):
    project_id: uuid.UUID
    generated_at: datetime
    can_start: bool                   # false when a check that blocks execution FAILED; never a guarantee of success
    blockers: list[str]
    items: list[DiagnosticItemOut]
    disclaimer: str
    cached: bool


# --- tasks -------------------------------------------------------------------------------------------------
class TaskCreate(BaseModel):
    requirements: str
    conversation_id: uuid.UUID | None = None
    limits: dict | None = None
    test_policy: dict | None = None


class TaskPatch(BaseModel):
    requirements: str | None = None
    limits: dict | None = None


class LeaseOut(_Out):
    run_id: uuid.UUID
    runner_id: str
    lease_expires_at: datetime
    heartbeat_at: datetime


class CheckpointOut(BaseModel):
    step_key: str
    status: str
    step_attempt: int
    plan_revision: int
    workflow_iteration: int


class TaskOut(_Out):
    id: uuid.UUID
    conversation_id: uuid.UUID
    project_id: uuid.UUID | None = None
    status: str
    requirements: str
    limits: dict
    plan_revision: int
    workflow_iteration: int
    replan_count: int
    cli_invocations: int
    run_seconds: float
    failure_reason: str | None = None
    cancel_requested_at: datetime | None = None
    profile_version: int | None = None      # the profile version this task copied; null = the built-in rules
    profile_hash: str | None = None
    created_at: datetime
    updated_at: datetime


class TaskDetailOut(TaskOut):
    lease: LeaseOut | None = None
    checkpoint: CheckpointOut | None = None
    pending_approvals: list[uuid.UUID] = []


class ExecutionRequestOut(_Out):
    id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    status: str
    created_at: datetime


class ExecutionOut(_Out):
    id: uuid.UUID
    task_id: uuid.UUID
    step_id: uuid.UUID | None = None
    role: str
    provider: str
    outcome: str
    exit_code: int | None = None
    started_at: datetime
    finished_at: datetime | None = None
    terminated_at: datetime | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None


class TransitionOut(_Out):
    id: int
    kind: str
    from_state: str | None = None
    to_state: str
    reason: str | None = None
    event: dict | None = None
    created_at: datetime


# --- approvals ------------------------------------------------------------------------------------------------
class HiddenOptionOut(_Out):
    option_id: str
    reason_code: str
    retryable: bool
    next_available_at: datetime | None = None
    basis_request_id: uuid.UUID | None = None
    message: str | None = None


class OperatorRequestOut(_Out):
    request_id: uuid.UUID
    kind: str
    status: str
    target_count: int
    requested_at: datetime
    finished_at: datetime | None = None
    reason: str | None = None
    retryable: bool
    attempts: int
    next_attempt_at: datetime | None = None
    last_error: str | None = None


class ApprovalOut(_Out):
    id: uuid.UUID
    task_id: uuid.UUID
    type: str
    status: str
    scope: str
    target: dict
    request_payload: dict
    checkpoint_step_key: str
    valid_until: datetime
    used_at: datetime | None = None
    created_at: datetime
    # additive (design 7.5 item 2): null for a static gate / when no options provider is wired
    options: list[dict] | None = None
    options_hidden: list[HiddenOptionOut] | None = None
    operator_request: OperatorRequestOut | None = None


class DecisionCreate(BaseModel):
    decision: str
    selected_option: str | None = None
    rationale: str | None = Field(default=None, max_length=P.PROCESS_RISK_RATIONALE_MAX_LENGTH)
    requested_by: str | None = Field(default=None, max_length=P.REQUESTED_BY_MAX_LENGTH)


class DecisionOut(_Out):
    id: uuid.UUID
    approval_id: uuid.UUID
    decision: str
    selected_option: str | None = None
    rationale: str | None = None
    decided_at: datetime


def build_task_detail(detail) -> TaskDetailOut:
    """Presentation mapping only (TaskService.detail already gathered the data)."""
    base = TaskOut.model_validate(detail.task).model_dump()
    lease = LeaseOut.model_validate(detail.lease) if detail.lease is not None else None
    cp = detail.checkpoint
    checkpoint = (CheckpointOut(step_key=cp.step_key, status=cp.status, step_attempt=cp.step_attempt,
                                plan_revision=cp.plan_revision, workflow_iteration=cp.workflow_iteration)
                  if cp is not None else None)
    return TaskDetailOut(**base, lease=lease, checkpoint=checkpoint, pending_approvals=detail.pending_approvals)
