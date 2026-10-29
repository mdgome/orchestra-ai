"""PostgreSQL lease: single RUNNING run per task, serialised by the task row lock.

Acquire/takeover is three steps and never holds a row lock while talking to the OS:
  A (short txn)  lock task, inspect the current run, commit
  -- (no txn)    verify / terminate the previous run's CLI processes
  B (short txn)  lock task again, re-validate that nothing changed, ABANDON old + INSERT new
If the previous CLI cannot be proven gone, the takeover is UNCERTAIN and nothing is started.
"""
import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.infra.process_control import CONFIRMED_GONE, ProcessIdentity, TerminationResult, terminate_tree
from app.models.tables import Execution
from app.repositories.task_repository import TaskRepository
from app.validation import policies as P
from app.validation.exceptions import LeaseLost

log = logging.getLogger(__name__)
Terminator = Callable[[ProcessIdentity], TerminationResult]


def utcnow() -> datetime:
    return datetime.now(UTC)


class AcquireStatus(StrEnum):
    ACQUIRED = "ACQUIRED"
    HELD = "HELD"            # a live (unexpired) lease, or the situation changed under us
    UNCERTAIN = "UNCERTAIN"  # expired, but the previous CLI could not be proven terminated


@dataclass(frozen=True)
class Lease:
    run_id: uuid.UUID
    task_id: uuid.UUID
    runner_id: str


@dataclass(frozen=True)
class AcquireResult:
    status: AcquireStatus
    lease: Lease | None = None
    previous_run_id: uuid.UUID | None = None
    reason: str | None = None


def _default_terminator(identity: ProcessIdentity) -> TerminationResult:
    return terminate_tree(identity, P.PROCESS_TERMINATE_GRACE_SECONDS)


class LeaseService:
    def __init__(self, db: Database, runner_id: str, ttl_seconds: int = P.LEASE_TTL_SECONDS,
                 terminator: Terminator = _default_terminator, clock: Callable[[], datetime] = utcnow):
        self.db = db
        self.runner_id = runner_id
        self.ttl = ttl_seconds
        self._terminate = terminator
        self._now = clock

    # --- acquire ---------------------------------------------------------------------------
    async def acquire(self, task_id: uuid.UUID) -> AcquireResult:
        # Txn A
        async with self.db.transaction() as s:
            repo = TaskRepository(s)
            if await repo.lock(task_id) is None:
                raise LookupError("task not found")
            now = self._now()
            current = await repo.running_run(task_id)
            if current is None:
                run = await repo.insert_run(task_id, self.runner_id, now, self.ttl)
                return AcquireResult(AcquireStatus.ACQUIRED, Lease(run.id, task_id, self.runner_id))
            if current.lease_expires_at > now:
                return AcquireResult(AcquireStatus.HELD, previous_run_id=current.id, reason="lease is live")
            seen = (current.id, current.row_version)
            stale = [(e.id, e.pid, e.process_create_time) for e in await repo.unterminated_executions(task_id)
                     if e.run_id == current.id]
        # lock released here

        # OS work, outside any transaction
        verdicts: list[tuple[uuid.UUID, TerminationResult | None]] = []
        for exec_id, pid, create_time in stale:
            if pid is None or create_time is None:
                verdicts.append((exec_id, None))   # STARTING without identity: may or may not have spawned
                continue
            verdicts.append((exec_id, await asyncio.to_thread(self._terminate, ProcessIdentity(pid, create_time))))
        unproven = [(eid, v) for eid, v in verdicts if v is None or v not in CONFIRMED_GONE]
        if unproven:
            return AcquireResult(AcquireStatus.UNCERTAIN, previous_run_id=seen[0],
                                 reason="previous CLI process termination not confirmed: "
                                        + ",".join(str(v) for _, v in unproven))

        # Txn B
        async with self.db.transaction() as s:
            repo = TaskRepository(s)
            await repo.lock(task_id)
            now = self._now()
            current = await repo.running_run(task_id)
            if current is None or (current.id, current.row_version) != seen or current.lease_expires_at > now:
                return AcquireResult(AcquireStatus.HELD, previous_run_id=seen[0], reason="takeover raced")
            for exec_id, _ in verdicts:
                await s.execute(update(Execution).where(Execution.id == exec_id, Execution.finished_at.is_(None))
                                .values(finished_at=func.now(), terminated_at=func.now(), outcome="ERROR",
                                        detail={"abandoned_by_takeover": True}))
            await repo.close_run(current.id, "ABANDONED", "lease_expired_takeover")
            run = await repo.insert_run(task_id, self.runner_id, now, self.ttl)
            return AcquireResult(AcquireStatus.ACQUIRED, Lease(run.id, task_id, self.runner_id), seen[0])

    # --- ownership -------------------------------------------------------------------------
    async def verify(self, session: AsyncSession, lease: Lease) -> None:
        """Re-validate ownership inside the caller's transaction (rowcount==1 or LeaseLost)."""
        if not await TaskRepository(session).heartbeat(lease.run_id, lease.runner_id, self._now(), self.ttl):
            raise LeaseLost("lease ownership lost", {"run_id": str(lease.run_id)})

    async def heartbeat(self, lease: Lease) -> bool:
        async with self.db.transaction() as s:
            return await TaskRepository(s).heartbeat(lease.run_id, lease.runner_id, self._now(), self.ttl)

    async def release(self, lease: Lease, reason: str, session: AsyncSession | None = None) -> None:
        async def _do(s: AsyncSession) -> None:
            await TaskRepository(s).close_run(lease.run_id, "FINISHED", reason)
        if session is not None:
            await _do(session)
        else:
            async with self.db.transaction() as s:
                await _do(s)

    def start_heartbeat(self, lease: Lease, on_lost: Callable[[], Awaitable[None] | None]) -> asyncio.Task:
        async def loop() -> None:
            interval = max(self.ttl / 3, 0.05)
            while True:
                await asyncio.sleep(interval)
                try:
                    alive = await self.heartbeat(lease)
                except Exception:  # noqa: BLE001 - DB outage: treat as lost, fail closed
                    log.exception("heartbeat failed")
                    alive = False
                if not alive:
                    res = on_lost()
                    if asyncio.iscoroutine(res):
                        await res
                    return
        return asyncio.create_task(loop())
