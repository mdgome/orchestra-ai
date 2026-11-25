"""process_risk_requests (design 7.5 item 4). Queries only; the caller owns the transaction.

Insert with the sorted target snapshot, fetch by id, discovery by (status, next_attempt_at), the claim of item 5 (one
conditional UPDATE that bumps `claim_generation`), the fenced result write (`WHERE status='VERIFYING' AND claim_generation=:g`),
the single APPLIED -> NEEDS_REDECISION supersede, the batch read by approval ids and the project-level observation queries
(recheck active count, latest terminal request, refused-alive streak, rolling window). None of them commits."""
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import ProcessRiskRequest

WORKER_RESULT_STATUSES = ("APPLIED", "REFUSED_ALIVE", "RETRYABLE", "NEEDS_REDECISION")


class ProcessRiskRequestRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def insert(self, *, approval_id: uuid.UUID, kind: str, task_id: uuid.UUID, project_id: uuid.UUID, dirty_epoch: int,
                     target_execution_ids: list[uuid.UUID], rationale: str, requested_by: str | None = None,
                     next_attempt_at: datetime | None = None) -> ProcessRiskRequest:
        """One REQUESTED row; the id is the approval id, so a second insert for the same approval violates the primary key
        and is never silently merged. The snapshot is stored sorted ascending (the final snapshot of the decision);
        an empty one is refused by the database CHECK, the service decides that case before it gets here."""
        row = ProcessRiskRequest(
            approval_id=approval_id, kind=kind, task_id=task_id, project_id=project_id, dirty_epoch=dirty_epoch,
            status="REQUESTED", target_execution_ids=sorted(target_execution_ids), requested_by=requested_by,
            rationale=rationale, next_attempt_at=func.now() if next_attempt_at is None else next_attempt_at)
        self.s.add(row)
        await self.s.flush()
        return row

    async def get(self, approval_id: uuid.UUID) -> ProcessRiskRequest | None:
        return (await self.s.execute(select(ProcessRiskRequest)
                                     .where(ProcessRiskRequest.approval_id == approval_id))).scalar_one_or_none()

    async def list_due(self, statuses: tuple[str, ...], now: datetime, limit: int) -> list[ProcessRiskRequest]:
        """Requests in `statuses` whose next_attempt_at has come, oldest first (the order of design 7.5 item 5: next_attempt_at,
        created_at, approval_id); served by ix_process_risk_requests_status_next_attempt."""
        stmt = (select(ProcessRiskRequest)
                .where(ProcessRiskRequest.status.in_(statuses), ProcessRiskRequest.next_attempt_at <= now)
                .order_by(ProcessRiskRequest.next_attempt_at, ProcessRiskRequest.created_at,
                          ProcessRiskRequest.approval_id)
                .limit(limit))
        return list((await self.s.execute(stmt)).scalars())

    # --- worker: discovery, claim, fenced result ---------------------------------------------------------------------
    @staticmethod
    def _claimable(incarnation: uuid.UUID, claim_timeout_seconds: float):
        R = ProcessRiskRequest
        stale = func.now() - timedelta(seconds=claim_timeout_seconds)
        return (((R.status == "REQUESTED") & (R.next_attempt_at <= func.now()))
                | ((R.status == "RETRYABLE") & (R.next_attempt_at <= func.now()))
                | ((R.status == "VERIFYING")
                   & (R.claimed_by_incarnation.is_distinct_from(incarnation) | (R.claimed_at <= stale))))

    async def list_claimable(self, incarnation: uuid.UUID, claim_timeout_seconds: float, limit: int
                             ) -> list[ProcessRiskRequest]:
        """REQUESTED, RETRYABLE and due, or VERIFYING and reclaimable (another incarnation, or claimed_at older than the claim
        timeout), ordered by next_attempt_at, created_at, approval_id (design 7.5 item 5)."""
        R = ProcessRiskRequest
        stmt = (select(R).where(self._claimable(incarnation, claim_timeout_seconds))
                .order_by(R.next_attempt_at, R.created_at, R.approval_id).limit(limit))
        return list((await self.s.execute(stmt)).scalars())

    async def claim(self, approval_id: uuid.UUID, incarnation: uuid.UUID, claim_timeout_seconds: float) -> int | None:
        """ONE conditional UPDATE: status -> VERIFYING, claim_generation + 1. None = lost the race (nothing changed)."""
        R = ProcessRiskRequest
        result = await self.s.execute(
            update(R).where(R.approval_id == approval_id, self._claimable(incarnation, claim_timeout_seconds))
            .values(status="VERIFYING", claim_generation=R.claim_generation + 1, claimed_by_incarnation=incarnation,
                    claimed_at=func.now(), updated_at=func.now())
            .returning(R.claim_generation))
        row = result.first()
        return None if row is None else int(row[0])

    async def lock_fenced(self, approval_id: uuid.UUID, generation: int) -> ProcessRiskRequest | None:
        """The request row FOR UPDATE, only while it is still VERIFYING under this claim generation."""
        R = ProcessRiskRequest
        return (await self.s.execute(select(R).where(R.approval_id == approval_id, R.status == "VERIFYING",
                                                     R.claim_generation == generation).with_for_update())).scalar_one_or_none()

    async def write_result(self, approval_id: uuid.UUID, generation: int, *, status: str, result: dict | None = None,
                           reason: str | None = None, last_error: str | None = None, attempts: int | None = None,
                           next_attempt_at: datetime | None = None) -> bool:
        """The fenced status transition of the worker. REQUESTED and VERIFYING are never written here; a terminal status sets
        `finished_at`, RETRYABLE sets `next_attempt_at` and leaves it NULL. False = the claim was lost (0 rows)."""
        if status not in WORKER_RESULT_STATUSES:
            raise ValueError(f"not a worker result status: {status}")
        values: dict = {"status": status, "reason": reason, "last_error": last_error, "updated_at": func.now(),
                        "result": result or {}}
        if attempts is not None:
            values["attempts"] = attempts
        if status == "RETRYABLE":
            if next_attempt_at is None:
                raise ValueError("RETRYABLE needs next_attempt_at")
            values["next_attempt_at"] = next_attempt_at
            values["finished_at"] = None
        else:
            values["finished_at"] = func.now()
        R = ProcessRiskRequest
        res = await self.s.execute(update(R).where(R.approval_id == approval_id, R.status == "VERIFYING",
                                                   R.claim_generation == generation).values(**values))
        return res.rowcount == 1

    async def supersede_applied(self, approval_id: uuid.UUID, supersede: dict, reason: str) -> bool:
        """The single APPLIED -> NEEDS_REDECISION transition (item 10 row 12): only while APPLIED and only once (the audit key
        `result.supersede` is written by this statement and its absence is part of the guard)."""
        res = await self.s.execute(text(
            "UPDATE process_risk_requests SET status = 'NEEDS_REDECISION', reason = :reason, updated_at = now(), "
            "result = result || CAST(:sup AS jsonb) "
            "WHERE approval_id = :id AND status = 'APPLIED' AND NOT jsonb_exists(result, 'supersede')"),
            {"id": approval_id, "reason": reason, "sup": json.dumps({"supersede": supersede})})
        return res.rowcount == 1

    # --- reads ---------------------------------------------------------------------------------------------------------
    async def by_approvals(self, approval_ids: list[uuid.UUID]) -> dict[uuid.UUID, ProcessRiskRequest]:
        """One batch read; an id without a request is simply absent from the result."""
        if not approval_ids:
            return {}
        rows = (await self.s.execute(select(ProcessRiskRequest)
                                     .where(ProcessRiskRequest.approval_id.in_(list(approval_ids))))).scalars()
        return {r.approval_id: r for r in rows}

    async def active_recheck_count(self, project_id: uuid.UUID, dirty_epoch: int) -> int:
        """REQUESTED, VERIFYING or RETRYABLE rechecks of the project and epoch (the set of uq_process_risk_recheck_active)."""
        R = ProcessRiskRequest
        return int((await self.s.execute(select(func.count()).select_from(R).where(
            R.project_id == project_id, R.dirty_epoch == dirty_epoch, R.kind == "RECHECK",
            R.status.in_(("REQUESTED", "VERIFYING", "RETRYABLE"))))).scalar_one())

    async def open_for_project(self, project_id: uuid.UUID, limit: int) -> list[ProcessRiskRequest]:
        """REQUESTED, VERIFYING and RETRYABLE requests of one project, oldest first, at most `limit` (diagnostics only)."""
        R = ProcessRiskRequest
        stmt = (select(R).where(R.project_id == project_id, R.status.in_(("REQUESTED", "VERIFYING", "RETRYABLE")))
                .order_by(R.next_attempt_at, R.approval_id).limit(limit))
        return list((await self.s.execute(stmt)).scalars())

    async def open_for_project(self, project_id: uuid.UUID, limit: int) -> list[ProcessRiskRequest]:
        """REQUESTED, VERIFYING and RETRYABLE requests of one project, oldest first, at most `limit` (diagnostics only)."""
        R = ProcessRiskRequest
        stmt = (select(R).where(R.project_id == project_id, R.status.in_(("REQUESTED", "VERIFYING", "RETRYABLE")))
                .order_by(R.next_attempt_at, R.approval_id).limit(limit))
        return list((await self.s.execute(stmt)).scalars())

    async def latest_terminal(self, project_id: uuid.UUID, *, kind: str | None = None, dirty_epoch: int | None = None
                              ) -> ProcessRiskRequest | None:
        """The request with the latest `finished_at` of the project (served by ix_process_risk_requests_project_finished)."""
        R = ProcessRiskRequest
        stmt = select(R).where(R.project_id == project_id, R.finished_at.is_not(None))
        if kind is not None:
            stmt = stmt.where(R.kind == kind)
        if dirty_epoch is not None:
            stmt = stmt.where(R.dirty_epoch == dirty_epoch)
        return (await self.s.execute(stmt.order_by(R.finished_at.desc(), R.approval_id.desc()).limit(1))).scalar_one_or_none()

    async def recheck_refused_streak(self, project_id: uuid.UUID, dirty_epoch: int, limit: int) -> int:
        """k of item 8 (a): the consecutive most recent terminal RECHECK requests of the project and epoch that ended
        REFUSED_ALIVE (stops at the first other outcome; reads at most `limit` rows)."""
        R = ProcessRiskRequest
        rows = (await self.s.execute(select(R.status).where(
            R.project_id == project_id, R.dirty_epoch == dirty_epoch, R.kind == "RECHECK", R.finished_at.is_not(None))
            .order_by(R.finished_at.desc(), R.approval_id.desc()).limit(limit))).all()
        k = 0
        for (status,) in rows:
            if status != "REFUSED_ALIVE":
                break
            k += 1
        return k

    async def recheck_window(self, project_id: uuid.UUID, dirty_epoch: int, window_seconds: float
                             ) -> tuple[int, ProcessRiskRequest | None]:
        """Item 8 (b): the number of RECHECK requests (any status) created within the window and the oldest of them (the one
        whose leaving the window lifts the limit)."""
        R = ProcessRiskRequest
        since = func.now() - timedelta(seconds=window_seconds)
        base = (R.project_id == project_id, R.dirty_epoch == dirty_epoch, R.kind == "RECHECK", R.created_at > since)
        count = int((await self.s.execute(select(func.count()).select_from(R).where(*base))).scalar_one())
        oldest = (await self.s.execute(select(R).where(*base).order_by(R.created_at, R.approval_id).limit(1))).scalar_one_or_none()
        return count, oldest

    async def latest_observation_overlapping(self, project_id: uuid.UUID, execution_ids: list[uuid.UUID]
                                             ) -> ProcessRiskRequest | None:
        """Design 7.5 item 1(e): among the process-observation requests of the project (REFUSED_ALIVE, APPLIED, or
        NEEDS_REDECISION with reason NO_LIVE_PROCESS_UNPROVEN) whose targets overlap `execution_ids` (`&&`), the one with the
        greatest finished_at. The project is alive-blocked when that request is REFUSED_ALIVE."""
        if not execution_ids:
            return None
        R = ProcessRiskRequest
        stmt = (select(R).where(
            R.project_id == project_id, R.finished_at.is_not(None), R.target_execution_ids.overlap(list(execution_ids)),
            (R.status.in_(("REFUSED_ALIVE", "APPLIED"))) | ((R.status == "NEEDS_REDECISION") & (R.reason == "NO_LIVE_PROCESS_UNPROVEN")))
            .order_by(R.finished_at.desc(), R.approval_id.desc()).limit(1))
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def redecide_if_status(self, approval_id: uuid.UUID, seen_status: str, reason: str) -> bool:
        """Item 10 row 8: `REQUESTED | VERIFYING | RETRYABLE -> NEEDS_REDECISION`, fenced `WHERE status = :seen`, which also stops a
        running worker (its fenced write then matches 0 rows). False = the status moved meanwhile."""
        if seen_status not in ("REQUESTED", "VERIFYING", "RETRYABLE"):
            raise ValueError("only a non-terminal status is redecided here")
        res = await self.s.execute(text(
            "UPDATE process_risk_requests SET status = 'NEEDS_REDECISION', reason = :reason, finished_at = now(), updated_at = now() "
            "WHERE approval_id = :id AND status = :seen"), {"id": approval_id, "reason": reason, "seen": seen_status})
        return res.rowcount == 1
