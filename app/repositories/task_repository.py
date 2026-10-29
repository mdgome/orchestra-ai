"""Tasks, runs (leases), steps, executions, transitions. Repositories never commit."""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Execution, StateTransition, Task, TaskRun, TaskStep


class TaskRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def add(self, task: Task) -> Task:
        self.s.add(task)
        await self.s.flush()
        return task

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return (await self.s.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()

    async def lock(self, task_id: uuid.UUID) -> Task | None:
        """SELECT ... FOR UPDATE: the serialisation point for lease acquisition and state changes."""
        return (await self.s.execute(select(Task).where(Task.id == task_id).with_for_update())).scalar_one_or_none()

    async def list(self, *, status: str | None = None, limit: int = 50, offset: int = 0) -> list[Task]:
        stmt = select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
        if status:
            stmt = stmt.where(Task.status == status)
        return list((await self.s.execute(stmt)).scalars())

    async def tasks_in_states(self, states: tuple[str, ...]) -> list[Task]:
        return list((await self.s.execute(select(Task).where(Task.status.in_(states)))).scalars())

    async def add_transition(self, task_id: uuid.UUID, to_state: str, from_state: str | None = None,
                             reason: str | None = None, event: dict | None = None, kind: str = "TASK_STATE") -> None:
        self.s.add(StateTransition(task_id=task_id, kind=kind, from_state=from_state, to_state=to_state,
                                   reason=reason, event=event))
        await self.s.flush()

    async def list_transitions(self, task_id: uuid.UUID) -> list[StateTransition]:
        rows = await self.s.execute(
            select(StateTransition).where(StateTransition.task_id == task_id).order_by(StateTransition.id))
        return list(rows.scalars())

    # --- runs / lease ---------------------------------------------------------------------
    async def running_run(self, task_id: uuid.UUID) -> TaskRun | None:
        return (await self.s.execute(
            select(TaskRun).where(TaskRun.task_id == task_id, TaskRun.status == "RUNNING"))).scalar_one_or_none()

    async def get_run(self, run_id: uuid.UUID) -> TaskRun | None:
        return (await self.s.execute(select(TaskRun).where(TaskRun.id == run_id))).scalar_one_or_none()

    async def insert_run(self, task_id: uuid.UUID, runner_id: str, now: datetime, ttl_seconds: int) -> TaskRun:
        run = TaskRun(id=uuid.uuid4(), task_id=task_id, runner_id=runner_id, status="RUNNING",
                      lease_expires_at=now + timedelta(seconds=ttl_seconds), heartbeat_at=now, row_version=0)
        self.s.add(run)
        await self.s.flush()
        return run

    async def close_run(self, run_id: uuid.UUID, status: str, reason: str, expected_status: str = "RUNNING") -> bool:
        result = await self.s.execute(
            update(TaskRun).where(TaskRun.id == run_id, TaskRun.status == expected_status)
            .values(status=status, finished_at=func.now(), finish_reason=reason, row_version=TaskRun.row_version + 1))
        return result.rowcount == 1

    async def heartbeat(self, run_id: uuid.UUID, runner_id: str, now: datetime, ttl_seconds: int) -> bool:
        """Extends the lease only for the owner and only while it has not expired."""
        result = await self.s.execute(
            update(TaskRun)
            .where(TaskRun.id == run_id, TaskRun.runner_id == runner_id, TaskRun.status == "RUNNING",
                   TaskRun.lease_expires_at > now)
            .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=ttl_seconds),
                    row_version=TaskRun.row_version + 1))
        return result.rowcount == 1

    async def running_runs(self) -> list[TaskRun]:
        return list((await self.s.execute(select(TaskRun).where(TaskRun.status == "RUNNING"))).scalars())

    # --- steps -----------------------------------------------------------------------------
    async def add_step(self, step: TaskStep) -> TaskStep:
        self.s.add(step)
        await self.s.flush()
        return step

    async def get_step(self, step_id: uuid.UUID) -> TaskStep | None:
        return (await self.s.execute(select(TaskStep).where(TaskStep.id == step_id))).scalar_one_or_none()

    async def steps_for_task(self, task_id: uuid.UUID) -> list[TaskStep]:
        rows = await self.s.execute(
            select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.created_at, TaskStep.step_attempt))
        return list(rows.scalars())

    async def latest_step(self, task_id: uuid.UUID, step_key: str, *, plan_revision: int | None = None,
                          workflow_iteration: int | None = None) -> TaskStep | None:
        stmt = select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step_key == step_key)
        if plan_revision is not None:
            stmt = stmt.where(TaskStep.plan_revision == plan_revision)
        if workflow_iteration is not None:
            stmt = stmt.where(TaskStep.workflow_iteration == workflow_iteration)
        stmt = stmt.order_by(TaskStep.created_at.desc(), TaskStep.step_attempt.desc()).limit(1)
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def max_attempt(self, task_id: uuid.UUID, plan_revision: int, workflow_iteration: int, step_key: str) -> int:
        value = (await self.s.execute(
            select(func.max(TaskStep.step_attempt)).where(
                TaskStep.task_id == task_id, TaskStep.plan_revision == plan_revision,
                TaskStep.workflow_iteration == workflow_iteration, TaskStep.step_key == step_key))).scalar()
        return value or 0

    async def failed_attempts(self, task_id: uuid.UUID, plan_revision: int, workflow_iteration: int,
                              step_key: str) -> int:
        """Technical failures of one (revision, iteration, step): FAILED / INTERRUPTED / NEEDS_MANUAL_RECOVERY.
        Human Gate waits and successful re-runs never consume the technical-retry budget."""
        return (await self.s.execute(
            select(func.count()).select_from(TaskStep).where(
                TaskStep.task_id == task_id, TaskStep.plan_revision == plan_revision,
                TaskStep.workflow_iteration == workflow_iteration, TaskStep.step_key == step_key,
                TaskStep.status.in_(("FAILED", "INTERRUPTED", "NEEDS_MANUAL_RECOVERY"))))).scalar() or 0

    # --- executions ------------------------------------------------------------------------
    async def add_execution(self, execution: Execution) -> Execution:
        self.s.add(execution)
        await self.s.flush()
        return execution

    async def get_execution(self, execution_id: uuid.UUID) -> Execution | None:
        return (await self.s.execute(select(Execution).where(Execution.id == execution_id))).scalar_one_or_none()

    async def executions_for_task(self, task_id: uuid.UUID) -> list[Execution]:
        rows = await self.s.execute(
            select(Execution).where(Execution.task_id == task_id).order_by(Execution.started_at, Execution.id))
        return list(rows.scalars())

    async def executions_for_request(self, request_id: uuid.UUID) -> list[Execution]:
        rows = await self.s.execute(select(Execution).where(Execution.request_id == request_id))
        return list(rows.scalars())

    async def unterminated_executions(self, task_id: uuid.UUID) -> list[Execution]:
        """CLI executions that were started but never reported a finish."""
        rows = await self.s.execute(
            select(Execution).where(Execution.task_id == task_id, Execution.finished_at.is_(None)))
        return list(rows.scalars())
