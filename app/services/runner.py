"""The single Runner: consumes persisted execution requests, owns leases, recovers after restarts.

Single-runner is the supported scope. Two guards, for two different problems:
  * process-level: a PostgreSQL session advisory lock makes a second Runner process refuse to start;
  * task-level:    the lease (task_runs partial unique index) stops two runs of the same task.
"""
import asyncio
import logging
import os
import socket
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import Database
from app.models.states import ApprovalType, StepState, TaskState, TERMINAL_TASK_STATES
from app.models.tables import Execution, ExecutionRequest, Task
from app.repositories.approval_repository import ExecutionRequestRepository
from app.repositories.task_repository import TaskRepository
from app.services.human_gate_service import HumanGateService
from app.services.lease_service import AcquireStatus, LeaseService
from app.services.session_service import SessionService
from app.services.state_service import change_step_state, change_task_state
from app.services.workflow_service import WorkflowService

log = logging.getLogger(__name__)


class RunnerAlreadyActive(RuntimeError):
    pass


def new_runner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Runner:
    def __init__(self, db: Database, leases: LeaseService, workflow: WorkflowService, gates: HumanGateService,
                 cancel_providers: list, advisory_key: int, claim_timeout_seconds: int = 300,
                 poll_seconds: float = 0.5, clock=lambda: datetime.now(UTC)):
        self.db, self.leases, self.workflow, self.gates = db, leases, workflow, gates
        self._providers = cancel_providers
        self._key = advisory_key
        self._claim_timeout = claim_timeout_seconds
        self._poll = poll_seconds
        self._now = clock
        self._lock_conn: AsyncConnection | None = None
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    @property
    def runner_id(self) -> str:
        return self.leases.runner_id

    # --- lifecycle (FastAPI lifespan) -----------------------------------------------------------------------
    async def start(self) -> None:
        conn = await self.db.engine.connect()
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        got = (await conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": self._key})).scalar()
        if not got:
            await conn.close()
            raise RunnerAlreadyActive("another Runner process already holds the runner lock")
        self._lock_conn = conn
        await self.recover()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        if self._lock_conn is not None:
            try:
                await self._lock_conn.execute(text("select pg_advisory_unlock(:k)"), {"k": self._key})
            finally:
                await self._lock_conn.close()
            self._lock_conn = None

    def notify(self) -> None:
        self._wake.set()

    # --- recovery ---------------------------------------------------------------------------------------------
    async def recover(self) -> dict:
        """Run at start-up (we hold the runner lock, so every CLAIMED request belongs to a dead runner)
        and periodically for requests claimed longer than the claim timeout."""
        summary = {"requeued": 0, "cancelled": 0, "closed": 0}
        startup = self._lock_conn is not None and self._loop_task is None
        async with self.db.transaction() as s:
            repo = ExecutionRequestRepository(s)
            stale = (list((await s.execute(select(ExecutionRequest).where(ExecutionRequest.status == "CLAIMED"))).scalars())
                     if startup else await repo.stale_claimed(self._now(), self._claim_timeout))
            for req in stale:
                task = await TaskRepository(s).get(req.task_id)
                if task is None or TaskState(task.status) in TERMINAL_TASK_STATES:
                    await repo.finish(req.id, "DONE", "task already finished")
                    summary["closed"] += 1
                    continue
                # Re-consumption is safe: acquire() re-verifies the old run's CLI processes and, when it cannot
                # prove they are gone (or whether one was ever spawned), parks the task in a RECOVERY gate
                # instead of running anything.
                await repo.requeue(req.id, "requeued_after_stale_claim")
                summary["requeued"] += 1
        summary["cancelled"] = await self._cancel_idle_tasks()
        if summary["requeued"]:
            self.notify()
        return summary

    async def _cancel_idle_tasks(self) -> int:
        """A cancellation request for a task nobody is running: nothing to terminate, finish it now."""
        done = 0
        async with self.db.transaction() as s:
            repo = TaskRepository(s)
            rows = (await s.execute(select(Task.id).where(Task.cancel_requested_at.is_not(None),
                                                          Task.status.notin_([str(x) for x in TERMINAL_TASK_STATES])))).scalars().all()
            for task_id in rows:
                task = await repo.lock(task_id)
                run = await repo.running_run(task_id)
                if run is not None or TaskState(task.status) in TERMINAL_TASK_STATES:
                    continue                                   # a live/unproven run exists: not ours to cancel here
                if await repo.unterminated_executions(task_id):
                    continue
                await ExecutionRequestRepository(s).s.execute(
                    text("update execution_requests set status='DONE', finished_at=now(), note='task cancelled' "
                         "where task_id=:t and status in ('PENDING','CLAIMED')"), {"t": task_id})
                await SessionService().close_all_for_task(s, task_id)
                for step in await repo.steps_for_task(task_id):
                    if StepState(step.status) in (StepState.RUNNING, StepState.WAITING_APPROVAL):
                        await change_step_state(s, step, StepState.CANCELLED, error="cancelled")
                await change_task_state(s, task, TaskState.CANCELLED, reason="cancelled")
                done += 1
        return done

    # --- main loop ---------------------------------------------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stopping:
            try:
                async with self.db.transaction() as s:
                    request = await ExecutionRequestRepository(s).claim_next(self.runner_id)
                if request is None:
                    await self.recover()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self._poll)
                    except TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                await self.process(request)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive; the request stays CLAIMED and is recovered
                log.exception("runner iteration failed")
                await asyncio.sleep(self._poll)

    async def process(self, request: ExecutionRequest) -> str:
        """Handle one claimed request end to end. Returns a short result label (for tests / logs)."""
        acquired = await self.leases.acquire(request.task_id)
        if acquired.status is AcquireStatus.HELD:
            async with self.db.transaction() as s:
                await ExecutionRequestRepository(s).requeue(request.id, f"held:{acquired.reason}")
            return "HELD"
        if acquired.status is AcquireStatus.UNCERTAIN:
            await self._park_uncertain(request, acquired.reason or "previous CLI not proven terminated")
            return "UNCERTAIN"
        lease = acquired.lease
        async with self.db.transaction() as s:
            await ExecutionRequestRepository(s).attach_run(request.id, lease.run_id)
        heartbeat = self.leases.start_heartbeat(lease, on_lost=self._lost(lease))
        try:
            status = await self.workflow.run_task(lease, request.task_id, resume_approval_id=request.approval_id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        async with self.db.transaction() as s:
            repo = ExecutionRequestRepository(s)
            if status == "LEASE_LOST":
                await repo.requeue(request.id, "lease_lost")      # the normal acquire/takeover path continues it
            else:
                await repo.finish(request.id, "DONE", status)
        return status

    def _lost(self, lease):
        async def on_lost():
            log.error("lease %s lost: cancelling its CLI processes", lease.run_id)
            for provider in self._providers:
                provider.cancel(str(lease.run_id))
        return on_lost

    async def _park_uncertain(self, request: ExecutionRequest, reason: str) -> None:
        """Takeover impossible without proof: never run; one transaction moves Task/Approval/Step together."""
        async with self.db.transaction() as s:
            repo = TaskRepository(s)
            task = await repo.lock(request.task_id)
            step = next((st for st in reversed(await repo.steps_for_task(task.id))
                         if StepState(st.status) in (StepState.RUNNING, StepState.INTERRUPTED)), None)
            pending_recovery = [a for a in await self._pending(s, task.id) if a.type == str(ApprovalType.RECOVERY)]
            if not pending_recovery:
                key = step.step_key if step else (task.resume_step_key or "PLAN")
                await self.gates.open_recovery(s, task, step, reason=reason, checkpoint_step_key=key)
            await ExecutionRequestRepository(s).finish(request.id, "NEEDS_REVIEW", reason[:120])

    @staticmethod
    async def _pending(s, task_id):
        from app.repositories.approval_repository import ApprovalRepository
        return await ApprovalRepository(s).pending_for_task(task_id)
