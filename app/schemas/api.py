"""Request / response shapes. Shape and type only: every business rule lives in app/validation.

Responses deliberately omit native session ids and any column that could carry one.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


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


class DecisionCreate(BaseModel):
    decision: str
    selected_option: str | None = None
    rationale: str | None = None


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
