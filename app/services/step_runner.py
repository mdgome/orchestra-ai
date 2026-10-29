"""Runs one CLI step with the transaction discipline required by the plan:

    short txn (verify lease, record STARTING)  ->  CLI runs OUTSIDE any transaction  ->  short txn (verify lease, record)

A result that arrives after lease ownership was lost is never recorded as a normal completion.
"""
import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.infra.event_bus import EventBus
from app.infra.process_control import CONFIRMED_GONE, ProcessIdentity
from app.infra.workspace_snapshot import take_snapshot
from app.models.states import CliOutcome, StepState
from app.models.tables import Execution, Task, TaskStep
from app.providers.base import CliProvider, ProviderRequest, ProviderResult
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.postgres_session_store import PostgresSessionStore
from app.repositories.task_repository import TaskRepository
from app.services.hashing import stable_hash
from app.services.lease_service import Lease, LeaseService
from app.services.session_service import PreparedSession, SessionService
from app.services.state_service import change_step_state
from app.validation import policies as P
from app.validation.exceptions import AppError, FailClosed, LeaseLost

log = logging.getLogger(__name__)


class TaskCancelled(AppError):
    status_code = 409
    code = "TASK_CANCELLED"


class LimitExceeded(AppError):
    status_code = 409
    code = "LIMIT_EXCEEDED"

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class StepAttemptsExhausted(LimitExceeded):
    pass


class TerminationUnconfirmed(AppError):
    """A CLI process could not be proven stopped: never continue as if it had."""
    status_code = 409
    code = "TERMINATION_UNCONFIRMED"

    def __init__(self, message: str, step: "TaskStep | None" = None, execution_id: uuid.UUID | None = None):
        super().__init__(message)
        self.execution_id = execution_id


@dataclass
class StepHandle:
    step_id: uuid.UUID
    task_id: uuid.UUID
    step_key: str
    role: str
    plan_revision: int
    workflow_iteration: int
    attempt: int
    input_hash: str
    snapshot_before: dict


@dataclass
class StepExecution:
    result: ProviderResult
    execution_id: uuid.UUID
    snapshot_after: dict
    session_action: str | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class StepRunner:
    def __init__(self, db: Database, leases: LeaseService, sessions: SessionService,
                 providers: dict[str, CliProvider], events: EventBus | None = None, clock=_utcnow,
                 cancel_poll_seconds: float = 1.0):
        self.db, self.leases, self.sessions, self.providers = db, leases, sessions, providers
        self.events = events or EventBus()
        self._now = clock
        self._cancel_poll = cancel_poll_seconds

    # --- begin ---------------------------------------------------------------------------------
    async def begin(self, lease: Lease, task_id: uuid.UUID, step_key: str, role: str, input_hash: str,
                    workspace: Path) -> StepHandle:
        snapshot = await asyncio.to_thread(take_snapshot, workspace)
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, lease)
            await self._preconditions(s, task)
            repo = TaskRepository(s)
            attempt = await repo.max_attempt(task.id, task.plan_revision, task.workflow_iteration, step_key) + 1
            failed = await repo.failed_attempts(task.id, task.plan_revision, task.workflow_iteration, step_key)
            if failed >= task.limits["max_step_attempts"]:
                raise StepAttemptsExhausted("STEP_ATTEMPTS_EXHAUSTED",
                                            f"{step_key} exceeded {task.limits['max_step_attempts']} attempts")
            step = await repo.add_step(TaskStep(
                id=uuid.uuid4(), task_id=task.id, run_id=lease.run_id, plan_revision=task.plan_revision,
                workflow_iteration=task.workflow_iteration, step_key=step_key, step_attempt=attempt,
                status=str(StepState.RUNNING), input_hash=input_hash, snapshot_before=snapshot,
                started_at=self._now()))
            handle = StepHandle(step.id, task.id, step_key, role, task.plan_revision, task.workflow_iteration,
                                attempt, input_hash, snapshot)
        self.events.publish(task_id, {"event": "step.started", "step": step_key, "attempt": attempt})
        return handle

    async def _preconditions(self, s: AsyncSession, task: Task) -> None:
        if task.cancel_requested_at is not None:
            raise TaskCancelled("cancellation requested")
        limits = task.limits
        if task.run_seconds >= limits["task_max_wall_seconds"]:
            raise LimitExceeded("WALL_TIME_EXCEEDED", "task wall-time limit reached")
        if task.cli_invocations >= limits["max_cli_invocations"]:
            raise LimitExceeded("CLI_INVOCATIONS_EXCEEDED", "task CLI invocation limit reached")
        if await ApprovalRepository(s).pending_for_task(task.id):
            raise FailClosed("an unresolved Human Gate blocks further execution")

    # --- execute -------------------------------------------------------------------------------
    async def execute(self, lease: Lease, handle: StepHandle, *, prompt: str, schema: dict | None, workspace: Path,
                      allow_resume: bool = True, one_off_tool: str | None = None,
                      timeout: int = P.DEFAULT_CLI_TIMEOUT_SECONDS,
                      before_spawn: Callable[[AsyncSession, Task], Awaitable[None]] | None = None) -> StepExecution:
        provider = self.providers[P.ROLE_PROVIDER[handle.role]]
        run_key = f"{lease.run_id}:{handle.step_id}"
        replaced_once = False
        while True:
            prepared, execution_id = await self._record_start(lease, handle, before_spawn, allow_resume and not replaced_once)
            request = ProviderRequest(role=handle.role, prompt=prompt, cwd=workspace, timeout_seconds=timeout,
                                      output_schema=schema, run_key=run_key, one_off_tool=one_off_tool)

            async def on_started(identity: ProcessIdentity | None, _eid=execution_id) -> None:
                if identity is not None:
                    async with self.db.transaction() as s:
                        await s.execute(update(Execution).where(Execution.id == _eid)
                                        .values(pid=identity.pid, process_create_time=identity.create_time))

            watcher = asyncio.create_task(self._watch_cancel(handle.task_id, provider, run_key))
            started = time.monotonic()
            try:
                if prepared.resume_id:
                    result = await provider.resume(prepared.resume_id, request, on_started)
                else:
                    result = await provider.execute(request, on_started)
            finally:
                watcher.cancel()
            elapsed = time.monotonic() - started
            snapshot_after = await asyncio.to_thread(take_snapshot, workspace)
            action = await self._record_finish(lease, handle, execution_id, prepared, result, elapsed)
            if result.outcome is CliOutcome.SESSION_MISSING and prepared.resume_id and not replaced_once:
                replaced_once = True       # explicit evidence that nothing ran: start a fresh native session
                continue
            self.events.publish(handle.task_id, {"event": "execution.finished", "step": handle.step_key,
                                                 "outcome": str(result.outcome)})
            return StepExecution(result, execution_id, snapshot_after, action)

    async def _watch_cancel(self, task_id: uuid.UUID, provider: CliProvider, run_key: str) -> None:
        while True:
            await asyncio.sleep(self._cancel_poll)
            try:
                async with self.db.transaction() as s:
                    task = await TaskRepository(s).get(task_id)
                if task is not None and task.cancel_requested_at is not None:
                    provider.cancel(run_key)
                    return
            except Exception:  # noqa: BLE001 - DB hiccup must not kill the watcher silently
                log.exception("cancel watcher failed")

    async def _record_start(self, lease, handle, before_spawn, allow_resume) -> tuple[PreparedSession, uuid.UUID]:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(handle.task_id)
            await self.leases.verify(s, lease)
            await self._preconditions(s, task)
            if before_spawn:
                await before_spawn(s, task)          # e.g. consume a PERMISSION approval atomically
            prepared = await self.sessions.prepare(PostgresSessionStore(s), task, handle.role,
                                                   allow_resume=allow_resume)
            await s.execute(update(Task).where(Task.id == task.id)
                            .values(cli_invocations=Task.cli_invocations + 1))
            execution = await TaskRepository(s).add_execution(Execution(
                id=uuid.uuid4(), task_id=task.id, step_id=handle.step_id, run_id=lease.run_id, role=handle.role,
                provider=P.ROLE_PROVIDER[handle.role], outcome=str(CliOutcome.STARTING),
                runner_id=lease.runner_id, detail={"resumed": prepared.resume_id is not None}))
            return prepared, execution.id          # STARTING is committed before any process exists

    async def _record_finish(self, lease: Lease, handle: StepHandle, execution_id: uuid.UUID,
                             prepared: PreparedSession, result: ProviderResult, elapsed: float) -> str | None:
        try:
            async with self.db.transaction() as s:
                task = await TaskRepository(s).lock(handle.task_id)
                await self.leases.verify(s, lease)                 # ownership must still hold
                unconfirmed = (result.outcome in (CliOutcome.TIMEOUT, CliOutcome.IDLE, CliOutcome.CANCELLED)
                               and result.termination is not None
                               and not self._confirmed_gone(result.termination))
                values = dict(
                    outcome=str(result.outcome), exit_code=result.exit_code, command_fingerprint=result.command_fingerprint,
                    stdout_tail=result.stdout_tail, stderr_tail=result.stderr_tail,
                    detail={"resumed": prepared.resume_id is not None, "termination": result.termination,
                            "permission_requests": result.permission_requests, **result.detail})
                if not unconfirmed:
                    values["finished_at"] = func.now()
                    if result.termination:
                        values["terminated_at"] = func.now()
                await s.execute(update(Execution).where(Execution.id == execution_id).values(**values))
                await s.execute(update(Task).where(Task.id == task.id).values(run_seconds=Task.run_seconds + elapsed))
                action = await self.sessions.record_result(s, PostgresSessionStore(s), task, handle.role, prepared, result)
        except LeaseLost:
            await self._discard_result(handle, execution_id, result)
            raise
        if unconfirmed:
            raise TerminationUnconfirmed(f"CLI process termination not confirmed ({result.termination})",
                                         execution_id=execution_id)
        return action

    @staticmethod
    def _confirmed_gone(termination: str) -> bool:
        return termination in {str(r) for r in CONFIRMED_GONE} or termination == "KILLED_BY_HANDLE"

    async def _discard_result(self, handle: StepHandle, execution_id: uuid.UUID, result: ProviderResult) -> None:
        """Ownership was lost: keep the result for audit only; never as a normal completion."""
        async with self.db.transaction() as s:
            await s.execute(update(Execution).where(Execution.id == execution_id).values(
                outcome="ERROR", finished_at=func.now(), exit_code=result.exit_code,
                discarded_result={"outcome": str(result.outcome), "reason": "lease lost before the result arrived"}))
            step = await TaskRepository(s).get_step(handle.step_id)
            if step is not None and StepState(step.status) is StepState.RUNNING:
                await change_step_state(s, step, StepState.INTERRUPTED, error="lease lost")

    # --- complete ------------------------------------------------------------------------------
    async def complete(self, lease: Lease, handle: StepHandle, status: StepState, *, output: dict | None = None,
                       error: str | None = None, snapshot_after: dict | None = None,
                       mutate: Callable[[AsyncSession, Task, TaskStep], Awaitable[None]] | None = None) -> TaskStep:
        """Step result + any task transition / new approval / lease release: one atomic transaction."""
        try:
            async with self.db.transaction() as s:
                task = await TaskRepository(s).lock(handle.task_id)
                await self.leases.verify(s, lease)
                step = await TaskRepository(s).get_step(handle.step_id)
                fields: dict = {"finished_at": func.now()}
                if output is not None:
                    fields.update(output=output, output_hash=stable_hash(output))
                if error is not None:
                    fields["error"] = error
                if snapshot_after is not None:
                    fields["snapshot_after"] = snapshot_after
                await change_step_state(s, step, status, **fields)
                if mutate:
                    await mutate(s, task, step)
        except LeaseLost:
            async with self.db.transaction() as s:
                step = await TaskRepository(s).get_step(handle.step_id)
                if step is not None and StepState(step.status) is StepState.RUNNING:
                    await change_step_state(s, step, StepState.INTERRUPTED, error="lease lost")
            raise
        self.events.publish(handle.task_id, {"event": "step.finished", "step": handle.step_key, "status": str(status)})
        return step
