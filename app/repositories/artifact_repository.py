"""Read queries for the artifact / report views. Queries only; the service owns the transaction.

The step manifests (`snapshot_before` / `snapshot_after`) are deferred on every query here: they are large, internal audit
data that no artifact view returns, so they are never even loaded."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.models.tables import AgentSession, Approval, ApprovalDecision, Execution, StateTransition, TaskStep

_NO_SNAPSHOTS = (defer(TaskStep.snapshot_before), defer(TaskStep.snapshot_after))


class ArtifactRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def steps_page(self, task_id: uuid.UUID, step_keys: tuple[str, ...] | None, limit: int, offset: int
                         ) -> tuple[list[TaskStep], int]:
        """One page of the task's steps in a stable order (created_at, id) and the total number of matching steps."""
        where = [TaskStep.task_id == task_id]
        if step_keys:
            where.append(TaskStep.step_key.in_(step_keys))
        total = (await self.s.execute(select(func.count()).select_from(TaskStep).where(*where))).scalar() or 0
        rows = await self.s.execute(select(TaskStep).where(*where).options(*_NO_SNAPSHOTS)
                                    .order_by(TaskStep.created_at, TaskStep.id).limit(limit).offset(offset))
        return list(rows.scalars()), total

    async def all_steps(self, task_id: uuid.UUID) -> list[TaskStep]:
        rows = await self.s.execute(select(TaskStep).where(TaskStep.task_id == task_id).options(*_NO_SNAPSHOTS)
                                    .order_by(TaskStep.created_at, TaskStep.id))
        return list(rows.scalars())

    async def step_of_task(self, task_id: uuid.UUID, step_id: uuid.UUID) -> TaskStep | None:
        """The step only if it belongs to this task: another task's artifact id is simply 'not found'."""
        return (await self.s.execute(select(TaskStep).where(TaskStep.id == step_id, TaskStep.task_id == task_id)
                                     .options(*_NO_SNAPSHOTS))).scalar_one_or_none()

    async def orchestrator_test_run(self, step_id: uuid.UUID) -> Execution | None:
        """The LOCAL `Execution` row the orchestrator wrote for its own test run of this Tester step
        (`detail.orchestrator_test_run`), or None when none was recorded. Tracked command rows share the step id and
        are skipped."""
        rows = await self.s.execute(select(Execution).where(
            Execution.step_id == step_id, Execution.provider == "LOCAL", Execution.role == "TESTER")
            .order_by(Execution.started_at.desc(), Execution.id.desc()))
        return next((r for r in rows.scalars() if (r.detail or {}).get("orchestrator_test_run") is True), None)

    async def known_session_ids(self, task_id: uuid.UUID) -> tuple[str, ...]:
        """The task's native session ids, used ONLY to mask them in returned text. Never returned."""
        rows = await self.s.execute(select(AgentSession.native_session_id).where(
            AgentSession.task_id == task_id, AgentSession.native_session_id.is_not(None)))
        return tuple(r for r in rows.scalars() if r)

    async def gates(self, task_id: uuid.UUID) -> list[tuple[Approval, ApprovalDecision | None]]:
        rows = await self.s.execute(
            select(Approval, ApprovalDecision).outerjoin(ApprovalDecision, ApprovalDecision.approval_id == Approval.id)
            .where(Approval.task_id == task_id).order_by(Approval.created_at, Approval.id))
        return [(a, d) for a, d in rows.all()]

    async def last_transition_to(self, task_id: uuid.UUID, state: str) -> StateTransition | None:
        return (await self.s.execute(
            select(StateTransition).where(StateTransition.task_id == task_id, StateTransition.kind == "TASK_STATE",
                                          StateTransition.to_state == state)
            .order_by(StateTransition.id.desc()).limit(1))).scalar_one_or_none()

    async def last_task_transition(self, task_id: uuid.UUID) -> StateTransition | None:
        return (await self.s.execute(
            select(StateTransition).where(StateTransition.task_id == task_id, StateTransition.kind == "TASK_STATE")
            .order_by(StateTransition.id.desc()).limit(1))).scalar_one_or_none()
