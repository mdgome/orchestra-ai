"""The only place a Task State changes: validates the transition and appends the history row."""
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.states import StepState, TaskState, check_step_transition, check_task_transition
from app.models.tables import Task, TaskStep
from app.repositories.task_repository import TaskRepository


async def change_task_state(session: AsyncSession, task: Task, to_state: TaskState, *, reason: str | None = None,
                            event: dict | None = None, failure_reason: str | None = None) -> None:
    """Caller must hold the task row lock (TaskRepository.lock) and the surrounding transaction."""
    current = TaskState(task.status)
    check_task_transition(current, to_state)
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
