"""The only place a Task State changes: validates the transition and appends the history row."""
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.states import (
    StepState, TaskState, TERMINAL_TASK_STATES, check_step_transition, check_task_transition,
)
from app.models.tables import Task, TaskStep
from app.repositories.task_repository import TaskRepository
from app.services.project_terminal import release_on_terminal


async def change_task_state(session: AsyncSession, task: Task, to_state: TaskState, *, reason: str | None = None,
                            event: dict | None = None, failure_reason: str | None = None,
                            project_proof=None) -> None:
    """Caller must hold the task row lock (TaskRepository.lock) and the surrounding transaction.

    A terminal state also releases the task's project write lock (project_terminal.release_on_terminal) in this
    same transaction. `project_proof` is only supplied by task_finalizer / the step-completion wrapper."""
    current = TaskState(task.status)
    check_task_transition(current, to_state)
    if to_state in TERMINAL_TASK_STATES:
        await release_on_terminal(session, task, to_state, project_proof)
    values = {"status": str(to_state), "updated_at": func.now(), "row_version": Task.row_version + 1}
    if failure_reason:
        values["failure_reason"] = failure_reason
    await session.execute(update(Task).where(Task.id == task.id).values(**values))
    await TaskRepository(session).add_transition(task.id, str(to_state), str(current), reason, event)
    await session.refresh(task)


async def change_step_state(session: AsyncSession, step: TaskStep, to_state: StepState, **fields) -> None:
    check_step_transition(StepState(step.status), to_state)
    await session.execute(update(TaskStep).where(TaskStep.id == step.id).values(status=str(to_state), **fields))
    await session.refresh(step)
