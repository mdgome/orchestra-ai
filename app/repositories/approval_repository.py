"""Approvals, decisions, design decisions, and execution requests. Repositories never commit."""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Approval, ApprovalDecision, DesignDecision, ExecutionRequest


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

    async def claim_next(self, runner_id: str) -> ExecutionRequest | None:
        """PENDING -> CLAIMED with SKIP LOCKED so a request is consumed at most once."""
        pick = (select(ExecutionRequest.id).where(ExecutionRequest.status == "PENDING")
                .order_by(ExecutionRequest.created_at).limit(1).with_for_update(skip_locked=True).scalar_subquery())
        result = await self.s.execute(
            update(ExecutionRequest).where(ExecutionRequest.id == pick)
            .values(status="CLAIMED", claimed_at=func.now(), runner_id=runner_id).returning(ExecutionRequest.id))
        row = result.first()
        if row is None:
            return None
        return await self.get(row[0])

    async def attach_run(self, request_id: uuid.UUID, run_id: uuid.UUID) -> None:
        await self.s.execute(update(ExecutionRequest).where(ExecutionRequest.id == request_id)
                             .values(task_run_id=run_id))

    async def finish(self, request_id: uuid.UUID, status: str, note: str | None = None) -> None:
        await self.s.execute(update(ExecutionRequest).where(ExecutionRequest.id == request_id)
                             .values(status=status, note=note, finished_at=func.now()))

    async def requeue(self, request_id: uuid.UUID, note: str) -> None:
        await self.s.execute(update(ExecutionRequest).where(ExecutionRequest.id == request_id)
                             .values(status="PENDING", claimed_at=None, runner_id=None, task_run_id=None, note=note))

    async def stale_claimed(self, now: datetime, timeout_seconds: int) -> list[ExecutionRequest]:
        rows = await self.s.execute(
            select(ExecutionRequest).where(ExecutionRequest.status == "CLAIMED",
                                           ExecutionRequest.claimed_at <= now - timedelta(seconds=timeout_seconds)))
        return list(rows.scalars())
