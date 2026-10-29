"""Task State and Step State are separate machines (plan P1-7)."""
from enum import StrEnum

from app.validation.exceptions import InvalidStateTransition


class TaskState(StrEnum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    DESIGN_REVIEWING = "DESIGN_REVIEWING"
    DEVELOPING = "DEVELOPING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    RETRYING = "RETRYING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StepState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    NEEDS_MANUAL_RECOVERY = "NEEDS_MANUAL_RECOVERY"
    CANCELLED = "CANCELLED"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    FINISHED = "FINISHED"
    ABANDONED = "ABANDONED"


class ApprovalType(StrEnum):
    DESIGN = "DESIGN"
    RECOVERY = "RECOVERY"
    PERMISSION = "PERMISSION"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    DECIDED = "DECIDED"


class RequestStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DONE = "DONE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class RequestKind(StrEnum):
    START = "START"
    RESUME = "RESUME"


class CliOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    IDLE = "IDLE"
    CANCELLED = "CANCELLED"
    PERMISSION_REQUESTED = "PERMISSION_REQUESTED"
    SESSION_MISSING = "SESSION_MISSING"
    STARTING = "STARTING"


# Step keys, in workflow order. Each maps to one Task State and one agent role.
STEP_PLAN = "PLAN"
STEP_DESIGN_REVIEW = "DESIGN_REVIEW"
STEP_DEVELOP = "DEVELOP"
STEP_TEST = "TEST"
STEP_CODE_REVIEW = "CODE_REVIEW"

STEP_TASK_STATE = {
    STEP_PLAN: TaskState.PLANNING,
    STEP_DESIGN_REVIEW: TaskState.DESIGN_REVIEWING,
    STEP_DEVELOP: TaskState.DEVELOPING,
    STEP_TEST: TaskState.TESTING,
    STEP_CODE_REVIEW: TaskState.REVIEWING,
}
STEP_ROLE = {
    STEP_PLAN: "PLANNER",
    STEP_DESIGN_REVIEW: "DESIGN_REVIEWER",
    STEP_DEVELOP: "DEVELOPER",
    STEP_TEST: "TESTER",
    STEP_CODE_REVIEW: "CODE_REVIEWER",
}

_ACTIVE = (TaskState.PLANNING, TaskState.DESIGN_REVIEWING, TaskState.DEVELOPING,
           TaskState.TESTING, TaskState.REVIEWING)
_ALWAYS = (TaskState.FAILED, TaskState.CANCELLED)

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.PLANNING, *_ALWAYS}),
    TaskState.PLANNING: frozenset({TaskState.DESIGN_REVIEWING, TaskState.WAITING_APPROVAL, *_ALWAYS}),
    TaskState.DESIGN_REVIEWING: frozenset(
        {TaskState.DEVELOPING, TaskState.PLANNING, TaskState.WAITING_APPROVAL, *_ALWAYS}),
    # DEVELOPING -> DESIGN_REVIEWING: the reviewed basis changed, so Development must not proceed.
    TaskState.DEVELOPING: frozenset({TaskState.TESTING, TaskState.DESIGN_REVIEWING, TaskState.WAITING_APPROVAL, *_ALWAYS}),
    TaskState.TESTING: frozenset(
        {TaskState.REVIEWING, TaskState.RETRYING, TaskState.WAITING_APPROVAL, *_ALWAYS}),
    TaskState.REVIEWING: frozenset(
        {TaskState.COMPLETED, TaskState.RETRYING, TaskState.TESTING, TaskState.WAITING_APPROVAL, *_ALWAYS}),
    TaskState.RETRYING: frozenset({TaskState.DEVELOPING, TaskState.PLANNING, TaskState.DESIGN_REVIEWING, *_ALWAYS}),
    # Approval resumes at the stage named by the approval's checkpoint.
    TaskState.WAITING_APPROVAL: frozenset({*_ACTIVE, *_ALWAYS}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}
TERMINAL_TASK_STATES = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})

STEP_TRANSITIONS: dict[StepState, frozenset[StepState]] = {
    StepState.PENDING: frozenset({StepState.RUNNING, StepState.CANCELLED}),
    StepState.RUNNING: frozenset({
        StepState.SUCCEEDED, StepState.FAILED, StepState.WAITING_APPROVAL, StepState.INTERRUPTED,
        StepState.NEEDS_MANUAL_RECOVERY, StepState.CANCELLED}),
    StepState.WAITING_APPROVAL: frozenset({StepState.CANCELLED, StepState.FAILED}),
    StepState.INTERRUPTED: frozenset({StepState.NEEDS_MANUAL_RECOVERY, StepState.FAILED, StepState.CANCELLED}),
    StepState.NEEDS_MANUAL_RECOVERY: frozenset({StepState.FAILED, StepState.CANCELLED}),
    StepState.SUCCEEDED: frozenset(),
    StepState.FAILED: frozenset(),
    StepState.CANCELLED: frozenset(),
}


def check_task_transition(current: TaskState, target: TaskState) -> None:
    if target not in TASK_TRANSITIONS[current]:
        raise InvalidStateTransition(
            f"task transition {current} -> {target} is not allowed",
            {"from": str(current), "to": str(target)})


def check_step_transition(current: StepState, target: StepState) -> None:
    if target not in STEP_TRANSITIONS[current]:
        raise InvalidStateTransition(
            f"step transition {current} -> {target} is not allowed",
            {"from": str(current), "to": str(target)})
