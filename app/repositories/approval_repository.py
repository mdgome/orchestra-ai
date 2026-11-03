"""Approvals, decisions, design decisions, and execution requests. Repositories never commit."""
import uuid
from collections.abc import Collection
from datetime import datetime, timedelta

from sqlalchemy import ARRAY, any_, bindparam, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Approval, ApprovalDecision, DesignDecision, ExecutionRequest, Task, TaskRun


class ApprovalRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def add(self, approval: Approval) -> Approval:
        self.s.add(approval)
        await self.s.flush()
        return approval

    async def get(self, approval_id: uuid.UUID) -> Approval | None:
        return (await self.s.execute(select(Approval).where(Approval.id == approval_id))).scalar_one_or_none()

    async def lock(self, approval_id: uuid.UUID) -> Approval | None:
        return (await self.s.execute(
            select(Approval).where(Approval.id == approval_id).with_for_update())).scalar_one_or_none()

    async def for_task(self, task_id: uuid.UUID) -> list[Approval]:
        rows = await self.s.execute(select(Approval).where(Approval.task_id == task_id).order_by(Approval.created_at))
        return list(rows.scalars())

    async def pending_for_task(self, task_id: uuid.UUID, type_: str | None = None) -> list[Approval]:
        stmt = select(Approval).where(Approval.task_id == task_id, Approval.status == "PENDING")
        if type_:
            stmt = stmt.where(Approval.type == type_)
        return list((await self.s.execute(stmt)).scalars())

    async def mark_decided(self, approval_id: uuid.UUID) -> None:
        await self.s.execute(update(Approval).where(Approval.id == approval_id).values(status="DECIDED"))

    async def get_decision(self, approval_id: uuid.UUID) -> ApprovalDecision | None:
        return (await self.s.execute(
            select(ApprovalDecision).where(ApprovalDecision.approval_id == approval_id))).scalar_one_or_none()

    async def insert_decision_if_absent(self, decision: ApprovalDecision) -> bool:
        """INSERT ... ON CONFLICT (approval_id) DO NOTHING; True when this call stored the decision."""
        stmt = pg_insert(ApprovalDecision).values(
            id=decision.id, approval_id=decision.approval_id, decision=decision.decision,
            selected_option=decision.selected_option, rationale=decision.rationale,
            idempotency_key=decision.idempotency_key, request_fingerprint=decision.request_fingerprint,
            basis=decision.basis).on_conflict_do_nothing(index_elements=["approval_id"])
        result = await self.s.execute(stmt)
        return result.rowcount == 1

    async def add_design_decision(self, task_id: uuid.UUID, approval_id: uuid.UUID, summary: str) -> None:
        self.s.add(DesignDecision(id=uuid.uuid4(), task_id=task_id, approval_id=approval_id, summary=summary))
        await self.s.flush()

    async def design_decisions(self, task_id: uuid.UUID) -> list[DesignDecision]:
        rows = await self.s.execute(
            select(DesignDecision).where(DesignDecision.task_id == task_id).order_by(DesignDecision.created_at,
                                                                                      DesignDecision.id))
        return list(rows.scalars())

    async def consume_permission(self, approval_id: uuid.UUID) -> bool:
        """Atomic single use: exactly one caller ever sees rowcount == 1."""
        result = await self.s.execute(
            update(Approval).where(Approval.id == approval_id, Approval.used_at.is_(None))
            .values(used_at=func.now()))
        return result.rowcount == 1


class ExecutionRequestRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def add(self, req: ExecutionRequest) -> ExecutionRequest:
        self.s.add(req)
        await self.s.flush()
        return req

    async def get(self, request_id: uuid.UUID) -> ExecutionRequest | None:
        return (await self.s.execute(
            select(ExecutionRequest).where(ExecutionRequest.id == request_id))).scalar_one_or_none()

    async def by_approval(self, approval_id: uuid.UUID) -> ExecutionRequest | None:
        return (await self.s.execute(
            select(ExecutionRequest).where(ExecutionRequest.approval_id == approval_id))).scalar_one_or_none()

    async def open_for_task(self, task_id: uuid.UUID) -> ExecutionRequest | None:
        return (await self.s.execute(
            select(ExecutionRequest).where(ExecutionRequest.task_id == task_id,
                                           ExecutionRequest.status.in_(("PENDING", "CLAIMED"))))).scalars().first()

    async def claim_next(self, runner_id: str, *, busy_project_ids: Collection[uuid.UUID] = ()
                         ) -> ExecutionRequest | None:
        """PENDING -> CLAIMED with SKIP LOCKED so a request is consumed at most once.

        Deferred requests (`not_before` in the future) are skipped, not claimed. Requests of tasks that belong to a
        project already at its concurrency cap are skipped too; tasks without a project (legacy, each with a private
        workspace) are never excluded. An empty `busy_project_ids` adds no condition at all. A fresh `claim_token`
        fences every later write made by this claim's coroutine."""
        pick = (select(ExecutionRequest.id).join(Task, Task.id == ExecutionRequest.task_id)
                .where(ExecutionRequest.status == "PENDING",
                       or_(ExecutionRequest.not_before.is_(None), ExecutionRequest.not_before <= func.now())))
        if busy_project_ids:
            busy = bindparam("busy_project_ids", list(busy_project_ids), type_=ARRAY(PG_UUID(as_uuid=True)))
            pick = pick.where(or_(Task.project_id.is_(None), ~(Task.project_id == any_(busy))))
        pick = (pick.order_by(ExecutionRequest.created_at).limit(1)
                .with_for_update(skip_locked=True, of=ExecutionRequest).scalar_subquery())
        result = await self.s.execute(
            update(ExecutionRequest).where(ExecutionRequest.id == pick)
            .values(status="CLAIMED", claimed_at=func.now(), runner_id=runner_id, claim_token=uuid.uuid4())
            .returning(ExecutionRequest.id))
        row = result.first()
        if row is None:
            return None
        return await self.get(row[0])

    async def fence_ok(self, request_id: uuid.UUID, token: uuid.UUID | None) -> bool:
        """True while this claim is still the current one (a requeue or re-claim changes the token)."""
        if token is None:
            return True                                   # a request that was never claimed through claim_next
        found = (await self.s.execute(select(ExecutionRequest.id).where(
            ExecutionRequest.id == request_id, ExecutionRequest.status == "CLAIMED",
            ExecutionRequest.claim_token == token))).first()
        return found is not None

    async def attach_run(self, request_id: uuid.UUID, run_id: uuid.UUID, *, token: uuid.UUID | None = None,
                         reset_defer: bool = False) -> bool:
        stmt = update(ExecutionRequest).where(ExecutionRequest.id == request_id)
        if token is not None:
            stmt = stmt.where(ExecutionRequest.status == "CLAIMED", ExecutionRequest.claim_token == token)
        values: dict = {"task_run_id": run_id}
        if reset_defer:
            values.update(defer_count=0, not_before=None)
        return (await self.s.execute(stmt.values(**values))).rowcount == 1

    async def reset_defer(self, request_id: uuid.UUID) -> None:
        await self.s.execute(update(ExecutionRequest).where(ExecutionRequest.id == request_id)
                             .values(defer_count=0, not_before=None))

    async def finish(self, request_id: uuid.UUID, status: str, note: str | None = None, *,
                     token: uuid.UUID | None = None) -> bool:
        """With a token only the current claim owner may finish; False means this coroutine was fenced out."""
        stmt = update(ExecutionRequest).where(ExecutionRequest.id == request_id)
        if token is not None:
            stmt = stmt.where(ExecutionRequest.status == "CLAIMED", ExecutionRequest.claim_token == token)
        return (await self.s.execute(stmt.values(status=status, note=note, finished_at=func.now()))).rowcount == 1

    async def requeue(self, request_id: uuid.UUID, note: str, *, token: uuid.UUID | None = None,
                      delay_seconds: float | None = None) -> bool:
        """Back to PENDING with every claim field cleared. A `delay_seconds` is a defer: `not_before` is pushed out
        and `defer_count` grows (it survives restarts). Without it (crash recovery) the counter is left alone."""
        stmt = update(ExecutionRequest).where(ExecutionRequest.id == request_id)
        if token is not None:
            stmt = stmt.where(ExecutionRequest.status == "CLAIMED", ExecutionRequest.claim_token == token)
        values: dict = {"status": "PENDING", "claimed_at": None, "runner_id": None, "task_run_id": None,
                        "claim_token": None, "note": note}
        if delay_seconds is not None:
            values["not_before"] = func.now() + timedelta(seconds=delay_seconds)
            values["defer_count"] = ExecutionRequest.defer_count + 1
        return (await self.s.execute(stmt.values(**values))).rowcount == 1

    async def stale_claimed(self, now: datetime, grace_seconds: int) -> list[ExecutionRequest]:
        """CLAIMED requests past the grace period whose task has NO live lease. A live lease means the request is
        being executed; the normal claim-to-lease window is covered by the grace period."""
        live = exists().where(TaskRun.task_id == ExecutionRequest.task_id, TaskRun.status == "RUNNING",
                              TaskRun.lease_expires_at > now)
        rows = await self.s.execute(
            select(ExecutionRequest).where(ExecutionRequest.status == "CLAIMED",
                                           ExecutionRequest.claimed_at <= now - timedelta(seconds=grace_seconds),
                                           ~live))
        return list(rows.scalars())
