"""Runs one CLI step with the transaction discipline required by the plan:

    short txn (verify lease, record STARTING)  ->  CLI runs OUTSIDE any transaction  ->  short txn (verify lease, record)

A result that arrives after lease ownership was lost is never recorded as a normal completion.
"""
import asyncio
import logging
import os
import socket
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.infra.event_bus import EventBus
from app.infra.command_executor import OnStarted, TrackedRun
from app.infra.process_control import CONFIRMED_GONE, IdentityFenced, ProcessIdentity, capture_identity
from app.infra.workspace_snapshot import take_snapshot
from app.models.states import TERMINAL_TASK_STATES, CliOutcome, ProcessLifecycle, StepState, TaskState
from app.models.tables import Execution, Task, TaskStep
from app.providers.base import CliProvider, ProviderRequest, ProviderResult
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.postgres_session_store import PostgresSessionStore
from app.repositories.task_repository import TaskRepository
from app.services.hashing import stable_hash
from app.services.lease_service import Lease, LeaseService
from app.services.session_service import PreparedSession, SessionService
from app.services.state_service import change_step_state
from app.services.tracked_classifier import classify_row
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

    def __init__(self, message: str, step: "TaskStep | None" = None, execution_id: uuid.UUID | None = None,
                 checkpoint_step_key: str | None = None):
        super().__init__(message)
        self.execution_id = execution_id
        # B2 (7.2): where a continue of the RECOVERY gate re-enters when no step is RUNNING/INTERRUPTED (STEP_TEST for the
        # test commands and `collect_ids`, which run before the tester step begins).
        self.checkpoint_step_key = checkpoint_step_key


class ProcessTrackingUnavailable(FailClosed):
    """The tracked-command infrastructure failed (capability gate, Job creation/assignment, identity, resume): no user code
    ran and the Task fails closed with this code, never a "failed test" that would loop the developer CLI (7.2 item 6)."""

    def __init__(self, message: str = P.PROCESS_TRACKING_UNAVAILABLE, details: dict | None = None):
        super().__init__(message, details)


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
    workspace_root: Path | None = None    # trusted root of a project workspace (outside WORKSPACE_ROOT)
    test_preset: str | None = None        # the task's test preset: selects tool permissions per role


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
                 cancel_poll_seconds: float = 1.0, process_tracking_state: Callable[[], str] | None = None):
        self.db, self.leases, self.sessions, self.providers = db, leases, sessions, providers
        self.events = events or EventBus()
        self._now = clock
        self._cancel_poll = cancel_poll_seconds
        # Plain seam of design 6.2: SUPPORTED | UNAVAILABLE | UNSUPPORTED_PLATFORM | UNKNOWN. The default trusts the host
        # (the executor decides platform support); phase 4/6 wires the Runner's capability state here.
        self._tracking_state = process_tracking_state or (lambda: "SUPPORTED")

    # --- begin ---------------------------------------------------------------------------------
    async def begin(self, lease: Lease, task_id: uuid.UUID, step_key: str, role: str, input_hash: str,
                    workspace: Path, workspace_root: Path | None = None,
                    on_begin: Callable[[AsyncSession, Task], Awaitable[None]] | None = None) -> StepHandle:
        snapshot = await asyncio.to_thread(take_snapshot, workspace)
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, lease)
            await self._preconditions(s, task)
            if on_begin:
                await on_begin(s, task)      # e.g. verify the project write lock and start a new write generation
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
                                attempt, input_hash, snapshot, workspace_root,
                                (task.test_policy or {}).get("preset"))
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
                                      output_schema=schema, run_key=run_key, one_off_tool=one_off_tool,
                                      workspace_root=handle.workspace_root, test_preset=handle.test_preset)

            async def on_started(identity: ProcessIdentity | None, _eid=execution_id) -> None:
                if identity is not None:
                    async with self.db.transaction() as s:
                        await s.execute(update(Execution).where(Execution.id == _eid)
                                        .values(pid=identity.pid, process_create_time=identity.create_time))

            watcher = asyncio.create_task(self.watch_cancel(handle.task_id, provider.cancel, run_key))
            started = time.monotonic()
            try:
                if prepared.resume_id:
                    result = await provider.resume(prepared.resume_id, request, on_started)
                else:
                    result = await provider.execute(request, on_started)
            finally:
                watcher.cancel()
            elapsed = time.monotonic() - started
            # The CLI result and the evidence that the process ended are recorded BEFORE anything that can fail
            # (a snapshot). LeaseLost -> discarded_result, an unconfirmed termination -> TerminationUnconfirmed: both
            # surface here, so a later SnapshotError can never overwrite or hide them, and no Execution is left
            # STARTING / unfinished because a snapshot failed.
            action = await self._record_finish(lease, handle, execution_id, prepared, result, elapsed)
            if result.outcome is CliOutcome.SESSION_MISSING and prepared.resume_id and not replaced_once:
                replaced_once = True       # explicit evidence that nothing ran: start a fresh native session
                continue                   # (this execution's result is already recorded; no snapshot is needed)
            self.events.publish(handle.task_id, {"event": "execution.finished", "step": handle.step_key,
                                                 "outcome": str(result.outcome)})
            # SnapshotError propagates: the step is not SUCCEEDED / reusable and the task fails closed.
            snapshot_after = await asyncio.to_thread(take_snapshot, workspace)
            return StepExecution(result, execution_id, snapshot_after, action)

    async def watch_cancel(self, task_id: uuid.UUID, cancel: Callable[[str], None], key: str) -> None:
        """Polls the Task's cancel request and calls `cancel(key)` once: a provider's run key for an agent CLI, a scope key
        for a group of tracked commands (`CommandExecutor.cancel` reaches every command under the key)."""
        while True:
            await asyncio.sleep(self._cancel_poll)
            try:
                async with self.db.transaction() as s:
                    task = await TaskRepository(s).get(task_id)
                if task is not None and task.cancel_requested_at is not None:
                    cancel(key)
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


class TrackedCommandPort(ABC):
    """What `TestPolicyService` needs to run a group of commands as TRACKED commands (design 7.1): an abstract base (no
    Protocol), implemented by `TrackedCommandSession`. The service never sees the database."""
    scope_key: str                  # `{run_id}:tracked:{scope_id}`: the cancel scope of the whole group
    checkpoint_step_key: str        # where a RECOVERY `continue` re-enters (STEP_TEST)

    @abstractmethod
    def check_capability(self) -> None:
        """Raises FailClosed(PROCESS_TRACKING_UNAVAILABLE) when the capability state forbids tracked commands."""

    @abstractmethod
    async def begin_command(self) -> tuple[TrackedRun, OnStarted]:
        """Registers the STARTING row (own short transaction) and returns the TrackedRun and its fenced `on_started`."""

    @abstractmethod
    async def finish_command(self, tracked: TrackedRun, raised: BaseException | None) -> None:
        """Closes (or deliberately leaves open) the row from `tracked.result`. Never raises."""

    @abstractmethod
    async def watch_cancel(self, cancel: Callable[[str], None]) -> None:
        """Runs until cancelled; calls `cancel(scope_key)` once when the Task's cancellation was requested."""


class TrackedCommandSession(TrackedCommandPort):
    """The tracked-run factory of one command group (design 7.1): the registration of the STARTING row, the fenced identity
    write and the closing write of each command. It is the concrete form of `TrackedCommandPort`
    (test_policy_service.py), which `TestPolicyService` drives without knowing the database.

    Every closing write is ONE lease-independent UPDATE with the JSONB-merged proof (6.1): `Runner.stop` revokes the lease
    before it cancels, and the rows must still close."""

    def __init__(self, steps: "StepRunner", lease: Lease, task_id: uuid.UUID, *, scope_id: str, checkpoint_step_key: str,
                 step_id: uuid.UUID | None = None,
                 owner_check: Callable[[AsyncSession, Task], Awaitable[None]] | None = None):
        self._steps, self._lease, self._task_id = steps, lease, task_id
        self._step_id, self._owner_check = step_id, owner_check
        self.scope_key = f"{lease.run_id}:tracked:{scope_id}"
        self.checkpoint_step_key = checkpoint_step_key

    def check_capability(self) -> None:
        state = self._steps._tracking_state()
        if state == "UNSUPPORTED_PLATFORM":
            return                                  # the POSIX degrade: untracked, decided by the executor (6.2)
        if state != "SUPPORTED":
            raise ProcessTrackingUnavailable(details={"state": str(state)})

    async def watch_cancel(self, cancel: Callable[[str], None]) -> None:
        await self._steps.watch_cancel(self._task_id, cancel, self.scope_key)

    async def begin_command(self) -> tuple[TrackedRun, OnStarted]:
        """Registration transaction (7.1 step 1): Task row lock -> lease -> Task not terminal -> owner of the project write
        lock -> INSERT. The order Task -> TaskRun is that of `_record_start`; no new lock edge."""
        steps = self._steps
        identity = capture_identity(os.getpid())
        detail = {"tracked": True, "v": P.DETAIL_SCHEMA_VERSION, "checkpoint_step_key": self.checkpoint_step_key,
                  "runner_process": {"host": socket.gethostname(), "pid": os.getpid(),
                                     "create_time": identity.create_time if identity else None}}
        async with steps.db.transaction() as s:
            task = await TaskRepository(s).lock(self._task_id)
            await steps.leases.verify(s, self._lease)
            if task is None or TaskState(task.status) in TERMINAL_TASK_STATES:
                raise FailClosed("the task is terminal: no tracked command can be registered")
            if task.cancel_requested_at is not None:
                raise TaskCancelled("cancellation requested")
            if self._owner_check is not None:
                await self._owner_check(s, task)
            execution = await TaskRepository(s).add_execution(Execution(
                id=uuid.uuid4(), task_id=task.id, step_id=self._step_id, run_id=self._lease.run_id, role="TESTER",
                provider="LOCAL", outcome=str(CliOutcome.STARTING), runner_id=self._lease.runner_id, detail=detail))
            execution_id = execution.id
        tracked = TrackedRun(execution_id, run_key=f"{self.scope_key}:{execution_id}", cancel_scope=self.scope_key)

        async def on_started(proc_identity: ProcessIdentity | None) -> None:
            """The fenced identity update (5.1 step 5): only while the row is open and the lease holds; a lost fence is an
            IdentityFenced, so the executor kills the suspended process and the command never runs."""
            if proc_identity is None:
                raise IdentityFenced("process identity unavailable")
            try:
                async with steps.db.transaction() as s2:
                    await TaskRepository(s2).lock(self._task_id)
                    await steps.leases.verify(s2, self._lease)
                    if not await TaskRepository(s2).set_tracked_identity(
                            execution_id, proc_identity.pid, proc_identity.create_time):
                        raise IdentityFenced("the execution row is no longer open")
            except LeaseLost as exc:
                raise IdentityFenced("lease lost before the identity was persisted") from exc

        return tracked, on_started

    async def finish_command(self, tracked: TrackedRun, raised: BaseException | None) -> None:
        """The shielded caller `finally` (7.1 step 4, table of 6.1). Never raises: a failing write leaves the row open (the
        conservative state), which the terminal rules and the sweeper then handle."""
        try:
            await self._finish(tracked, raised)
        except Exception:  # noqa: BLE001 - a DB error here must not hide the command's own outcome
            log.exception("closing write of tracked execution %s failed; the row stays open", tracked.execution_id)

    async def _finish(self, tracked: TrackedRun, raised: BaseException | None) -> None:
        result = tracked.result
        close, proof, merge = True, P.PROOF_NEVER_RAN, {}
        outcome, exit_code, terminated, out, err = str(CliOutcome.ERROR), None, False, None, None
        if result is None:
            if tracked.spawned:
                close, proof = False, None                      # RESULT_MISSING: stays unresolved
                merge = {"reason": P.TRACKED_RESULT_MISSING}
                outcome = None
            else:                                               # raised before the process existed: user code never ran
                merge = {"lifecycle": str(ProcessLifecycle.NOT_STARTED), "tree": "EMPTY",
                         "reason": type(raised).__name__ if raised is not None else "NOT_STARTED"}
                if isinstance(raised, asyncio.CancelledError):
                    outcome = str(CliOutcome.CANCELLED)
        else:
            outcome, exit_code = str(result.outcome), result.exit_code
            out, err = (result.stdout or "")[-4000:], (result.stderr or "")[-4000:]
            merge = {"lifecycle": str(result.lifecycle), "tree": str(result.tree), "survivors": result.survivors}
            if result.termination:
                merge["termination"] = result.termination
            if result.reason:
                merge["reason"] = result.reason
            if result.lifecycle in (ProcessLifecycle.EXITED, ProcessLifecycle.TERMINATED):
                proof = P.PROOF_JOB_EMPTY
                terminated = result.lifecycle is ProcessLifecycle.TERMINATED or result.survivors
            elif result.spawn_failed or result.lifecycle in (ProcessLifecycle.NOT_STARTED, ProcessLifecycle.SPAWN_FAILED):
                proof = P.PROOF_NEVER_RAN
            else:                                               # TERMINATION_UNCONFIRMED (or no lifecycle): never closed
                close, proof = False, None
                if result.lifecycle is not ProcessLifecycle.TERMINATION_UNCONFIRMED:
                    merge["reason"] = "LIFECYCLE_MISSING"
        if proof is not None:
            merge["proof"] = proof
        async with self._steps.db.transaction() as s:
            written = await TaskRepository(s).write_tracked_result(
                tracked.execution_id, merge=merge, outcome=outcome, exit_code=exit_code, close=close,
                terminated=terminated, stdout_tail=out, stderr_tail=err)
            row = None if written else await TaskRepository(s).tracked_row(tracked.execution_id)
        if not written:
            # rowcount 0 is never success (6.1): re-read and classify; a row somebody else already proved is settled
            verdict = classify_row(row.id, row.pid, row.finished_at, row.detail_text) if row is not None else None
            log.warning("closing write of tracked execution %s matched no open row (now %s)", tracked.execution_id,
                        verdict.row_class if verdict else "MISSING")
