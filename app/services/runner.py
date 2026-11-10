"""The Runner / Coordinator: consumes persisted execution requests, owns leases, recovers after restarts.

Three guards, for three different problems:
  * process-level: a PostgreSQL session advisory lock makes a second Runner process refuse to start (this is the
                   Coordinator singleton guard; it is unchanged by parallel execution);
  * task-level:    the lease (task_runs partial unique index) stops two runs of the same task;
  * claim-level:   every claim carries a token. Every later write of that claim's coroutine is fenced by it, so a
                   request that was requeued meanwhile can never be executed by its stale previous owner.

Dispatch is bounded: a free slot is checked BEFORE claiming, so a claimed request never waits for a slot. Running
requests live in a registry; a done-callback collects the exception, frees the slot and wakes the loop.
"""
import asyncio
import enum
import logging
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, async_sessionmaker

from app.db import Database
from app.models.states import ApprovalType, StepState, TaskState, TERMINAL_TASK_STATES
from app.models.tables import ExecutionRequest, Task
from app.repositories.approval_repository import ExecutionRequestRepository
from app.repositories.task_repository import TaskRepository
from app.services.human_gate_service import HumanGateService
from app.services.lease_service import AcquireStatus, Lease, LeaseService
from app.services.recovery_transaction import (
    Candidate, DbOutcome, Intent, Phase, RecoveryEvidence, RecoveryOutcome, RecoveryResult, ResourceOutcome,
    Verdict,
    reconcile_body, resettle_resources, run_recovery_transaction)
from app.services.request_backoff import defer_delay
from app.services.session_service import SessionService
from app.services.state_service import change_step_state, change_task_state
from app.services.workflow_service import WorkflowService
from app.validation import policies as P
from app.validation.validators import validate_recovery_timeouts

log = logging.getLogger(__name__)


class RunnerAlreadyActive(RuntimeError):
    pass


class RunnerStopIncomplete(RuntimeError):
    """stop() could not end every running task and/or settle the request recovery. The advisory lock connection and the
    registry are KEPT: a live coroutine (a task, or a recovery transaction) may still change the database or a
    workspace, so the lock must not be reported as released. Call stop() again."""

    def __init__(self, run_ids: list[str], recovery: str | None = None):
        what = [f"{len(run_ids)} task(s) are still running after the shutdown grace period"] if run_ids else []
        if recovery:
            what.append(f"request recovery is unsettled ({recovery})")
        super().__init__("; ".join(what) or "the Runner could not settle its resources")
        self.run_ids = run_ids
        self.recovery = recovery


class RecoveryError(Exception):
    """Base of the recovery orchestration errors. Deliberately NOT a RuntimeError: `_loop` treats a RuntimeError outside
    RUNNING as "stop() won the race" and leaves quietly; these are logged and the loop survives."""


class RecoveryTimeout(RecoveryError):
    """The recovery operation did not finish within `recovery_timeout_seconds`. Raised even when the cancelled
    operation rolled back (or committed) afterwards; `pending` means the operation task is still alive (kept)."""

    def __init__(self, result: "RecoveryResult | None", pending: bool):
        super().__init__("recovery operation timed out" + (" and is still pending" if pending else ""))
        self.result = result
        self.pending = pending


class RecoveryUnsettled(RecoveryError):
    """The recovery result is not settled: the commit outcome is unknown (UNCERTAIN) or the connection/session could not
    be confirmed released. The evidence is kept by the Runner; nothing was applied on top of it."""

    def __init__(self, result: "RecoveryResult | None", reason: str):
        super().__init__(f"recovery unsettled: {reason}")
        self.result = result
        self.reason = reason


class RecoveryKind(enum.Enum):
    MUTATION = "MUTATION"                # the normal startup / periodic request-recovery transaction
    RECONCILIATION = "RECONCILIATION"    # read-only check of an UNCERTAIN MUTATION result
    CLEANUP = "CLEANUP"                  # another attempt to release a session an operation could not release


class CancelReason(enum.Enum):
    NONE = "NONE"
    CALLER_CANCEL = "CALLER_CANCEL"          # the task that called recover() was cancelled
    OPERATION_DEADLINE = "OPERATION_DEADLINE"  # recovery_timeout_seconds elapsed
    SHUTDOWN = "SHUTDOWN"                    # stop() / a failed start asked for the cancellation


@dataclass(eq=False)
class _RecoveryCtx:
    """The registered recovery operation. Fields are changed only under `_lifecycle_lock` (the evidence objects are
    written by the operation task itself, which owns them while it runs)."""
    kind: RecoveryKind
    startup: bool
    generation: int
    evidence: RecoveryEvidence
    original: RecoveryEvidence | None = None      # RECONCILIATION: the UNCERTAIN evidence being checked
    reason: CancelReason = CancelReason.NONE
    cancel_calls: int = 0
    task: asyncio.Task | None = None
    result: RecoveryResult | None = None          # set by the reap, read by whoever waited for the task


class RunnerLifecycle(enum.Enum):
    """Process-internal state of one Runner object. Never persisted: it is not a DB model, migration or task state."""
    STOPPED = "STOPPED"                  # no loop, no advisory-lock connection, nothing in flight
    STARTING = "STARTING"                # start() is installing its resources (lock connection, startup recovery)
    RUNNING = "RUNNING"                  # lock connection held, loop alive
    STOPPING = "STOPPING"                # stop() is draining; nothing new is claimed
    # a stop (or a failed start) could not confirm everything is gone: a live in-flight task and/or a runtime connection
    # (the advisory-lock connection, or one whose close failed) is kept; no loop. start/recover refuse; stop() retries.
    STOP_INCOMPLETE = "STOP_INCOMPLETE"


# One `Runner._recover` call runs at most these recovery stages, in this order, and never the same non-mutation stage
# twice without progress: CLEANUP (a session an earlier operation could not release) -> RECONCILIATION (an UNCERTAIN
# commit) -> CLEANUP (the session the reconciliation itself could not release) -> MUTATION. Every non-mutation stage must
# (1) release exactly one piece of evidence, by identity, and let the call go on, or (2) keep it unresolved and end the
# call with RecoveryUnsettled at once, or (3) end with a timeout / cancellation, or (4) be stopped by a lifecycle /
# generation change. So the limit is never reached by a stage that made no progress.
_MAX_RECOVERY_STAGES = 4
_LIFECYCLE_CHANGED = "the Runner lifecycle changed while the recovery stages ran"


@dataclass(frozen=True)
class _StageSnapshot:
    """What a recovery-stage decision may look at: taken in ONE critical section under `_lifecycle_lock`, judged
    afterwards by the pure `Runner._stage_blocked`, which reads no Runner field."""
    reaped: bool
    cleanup_unsettled: RecoveryEvidence | None
    unsettled: RecoveryEvidence | None
    generation: int
    state: "RunnerLifecycle"
    lock_conn_held: bool


def _combine(errors: list[BaseException]) -> BaseException:
    """One error as itself, several as a group (BaseExceptionGroup picks ExceptionGroup when all are Exceptions)."""
    return errors[0] if len(errors) == 1 else BaseExceptionGroup("runner lifecycle errors", errors)


def new_runner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass
class _InFlight:
    project_id: uuid.UUID | None
    task: asyncio.Task | None = None
    run_id: uuid.UUID | None = field(default=None)      # set once the lease exists (used by shutdown)


class Runner:
    def __init__(self, db: Database, leases: LeaseService, workflow: WorkflowService, gates: HumanGateService,
                 cancel_providers: list, advisory_key: int, claim_timeout_seconds: int = 300,
                 poll_seconds: float = 0.5, clock=lambda: datetime.now(UTC),
                 max_concurrent_tasks: int = P.DEFAULT_MAX_CONCURRENT_TASKS,
                 max_tasks_per_project: int = P.DEFAULT_MAX_TASKS_PER_PROJECT,
                 shutdown_grace_seconds: float = P.DEFAULT_RUNNER_SHUTDOWN_GRACE_SECONDS,
                 recovery_statement_timeout_seconds: float = P.DEFAULT_RECOVERY_STATEMENT_TIMEOUT_SECONDS,
                 recovery_timeout_seconds: float = P.DEFAULT_RUNNER_RECOVERY_TIMEOUT_SECONDS,
                 recovery_shutdown_wait_seconds: float = P.DEFAULT_RUNNER_RECOVERY_SHUTDOWN_WAIT_SECONDS):
        validate_recovery_timeouts(recovery_statement_timeout_seconds, recovery_timeout_seconds,
                                   recovery_shutdown_wait_seconds)
        self.db, self.leases, self.workflow, self.gates = db, leases, workflow, gates
        self._recovery_statement_ms = int(recovery_statement_timeout_seconds * 1000)
        self._recovery_timeout = recovery_timeout_seconds
        self._recovery_shutdown_wait = recovery_shutdown_wait_seconds
        # The recovery transaction gets its own sessionmaker (Database._factory stays private to Database).
        self._recovery_sessions = async_sessionmaker(db.engine, expire_on_commit=False)
        self._providers = cancel_providers
        self._key = advisory_key
        self._claim_grace = claim_timeout_seconds
        self._poll = poll_seconds
        self._now = clock
        self._max_tasks = max(1, max_concurrent_tasks)
        self._max_per_project = max(1, max_tasks_per_project)
        self._shutdown_grace = shutdown_grace_seconds
        self._lock_conn: AsyncConnection | None = None
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._inflight: dict[uuid.UUID, _InFlight] = {}      # execution request id -> running coroutine
        self._state = RunnerLifecycle.STOPPED
        # Serialises lifecycle TRANSITIONS and the short recovery transaction; asyncio.Lock is NOT re-entrant, so
        # nothing holding it may call a public method that takes it again. It is never held across CLI execution,
        # the shutdown grace waits, provider cancellation or the in-flight drain, and done-callbacks never need it.
        self._lifecycle_lock = asyncio.Lock()
        # Request recovery. The lifecycle lock guards only the checks and the registration / reaping below; the recovery
        # DB I/O runs in its own operation task OUTSIDE the lock. Registering `_recovery` IS the per-Runner single-run
        # gate (no separate lock: lock.locked() followed by acquire() would be a TOCTOU). Every field here changes only
        # under `_lifecycle_lock`; the done callback only consumes the exception, logs and wakes.
        # NOTE the recovery DB connection is NOT the advisory-lock connection (`_lock_conn`). The advisory lock is kept
        # while a recovery is live or unsettled so that no other Runner can start and the singleton ownership is
        # preserved, not because the two connections are related.
        self._recovery_done = asyncio.Event()                  # set by the done callback (observability only)
        self._generation = 0                                   # bumped by start()/stop() entry; see `_recover`
        self._recovery: _RecoveryCtx | None = None             # the registered, not yet reaped operation
        self._recovery_unsettled: RecoveryEvidence | None = None          # DB outcome of a MUTATION is UNCERTAIN
        self._recovery_cleanup_unsettled: RecoveryEvidence | None = None  # a session could not be confirmed released

    @property
    def runner_id(self) -> str:
        return self.leases.runner_id

    @property
    def in_flight(self) -> int:
        return len(self._inflight)

    @property
    def lifecycle(self) -> RunnerLifecycle:
        return self._state

    @property
    def recovery_unsettled(self) -> bool:
        """Read-only view for /health: a recovery commit outcome or a session release is still unconfirmed."""
        return self._recovery_unsettled is not None or self._recovery_cleanup_unsettled is not None

    # --- lifecycle (FastAPI lifespan) -----------------------------------------------------------------------
    # Every lifecycle FINAL transition (STOPPED / STOP_INCOMPLETE) is made by a private "settle" task that the public
    # method awaits through `asyncio.shield`: extra cancellations can neither interrupt the cleanup nor leave the
    # Runner in STARTING / STOPPING. `_lifecycle_lock` is taken only to enter STARTING/STOPPING and for the final
    # transition, never while waiting for the loop, the grace periods, provider cancellation or connection unlock/close;
    # it is not re-entrant, so no settle coroutine calls a public start/stop/recover while holding it.
    async def start(self) -> None:
        """STOPPED -> STARTING -> RUNNING. Only a STOPPED, empty Runner may start (no implicit grace recovery).
        A second start()/stop()/recover() issued during STARTING is refused immediately instead of queueing."""
        async with self._lifecycle_lock:
            if self._state is not RunnerLifecycle.STOPPED:
                raise RuntimeError(f"Runner cannot start from {self._state.value}")
            if (self._inflight or self._lock_conn is not None or self._loop_task is not None
                    or not self._recovery_clean()):
                raise RuntimeError("Runner is STOPPED but still holds resources")
            self._state = RunnerLifecycle.STARTING
            self._generation += 1
            self._wake.clear()
        local_conn: AsyncConnection | None = None            # until the lock is held, start() owns this connection
        try:
            local_conn = await self.db.engine.connect()
            local_conn = await local_conn.execution_options(isolation_level="AUTOCOMMIT")
            got = (await local_conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": self._key})).scalar()
            if not got:
                raise RunnerAlreadyActive("another Runner process already holds the runner lock")
            self._lock_conn = local_conn                     # from here on the Runner field owns the cleanup
            await self.recover(startup=True)                 # re-checks STARTING + lock held, under the lifecycle lock
            async with self._lifecycle_lock:
                self._loop_task = asyncio.create_task(self._loop())
                self._state = RunnerLifecycle.RUNNING
        except BaseException as exc:
            await self._fail_start(local_conn, exc)          # always raises
            raise

    async def _fail_start(self, local_conn: AsyncConnection | None, original: BaseException) -> None:
        """Settles a failed start and raises the right error. Priority: an original start error stays the main error
        (cleanup problems become notes); a CancelledError is never swallowed — a cancellation that is the original
        error stays final, a cancellation that arrives while cleaning up becomes final and keeps the start error and the
        cleanup errors as its cause. Further cancellations only add a note."""
        settle = asyncio.ensure_future(self._settle_failed_start(local_conn))
        cancels = await self._wait_shielded(settle)
        if settle.cancelled():
            problems: list[BaseException] = []
        elif settle.exception() is not None:
            problems = [settle.exception()]                  # the lifecycle transition itself failed
        else:
            problems = settle.result()
        for problem in problems:
            log.error("cleanup after a failed Runner start also failed: %r", problem)
        if isinstance(original, asyncio.CancelledError):
            final, causes = original, problems
            for extra in cancels:
                original.add_note(f"further cancellation while cleaning up the failed start: {extra!r}")
        elif cancels:
            final, causes = cancels[0], [original, *problems]
            for extra in cancels[1:]:
                final.add_note(f"further cancellation while cleaning up the failed start: {extra!r}")
        else:
            for problem in problems:
                original.add_note(f"cleanup after the failed start also failed: {problem!r}")
            raise original
        if not causes:
            raise final
        raise final from _combine(causes)

    async def _settle_failed_start(self, local_conn: AsyncConnection | None) -> list[BaseException]:
        """Decides ONCE which connection to clean up (the Runner field if the lock was taken, else the local one),
        unlocks (only when the lock was held) and closes it, then makes the final lifecycle transition: STOPPED when
        the close is confirmed, STOP_INCOMPLETE (connection kept in `_lock_conn`) when it is not."""
        problems: list[BaseException] = []
        owned = self._lock_conn if self._lock_conn is not None else local_conn
        closed = owned is None
        try:
            try:                                             # a startup recovery may still be live / unsettled
                await self._settle_recovery_for_stop()
            except Exception as exc:  # noqa: BLE001 - recorded; the lock decision below uses the real state
                problems.append(exc)
            if owned is not None and self._recovery_clean():
                if owned is self._lock_conn:
                    await self._unlock_best_effort(owned)
                try:
                    await owned.close()
                    closed = True
                except Exception as exc:  # noqa: BLE001 - recorded; the connection is kept for a later stop()
                    problems.append(exc)
            elif owned is not None:
                log.error("failed start: the request recovery is unsettled; the advisory lock is kept")
        finally:
            async with self._lifecycle_lock:
                self._loop_task = None
                if closed:
                    self._lock_conn = None
                    self._wake.clear()
                    self._state = RunnerLifecycle.STOPPED
                else:
                    self._lock_conn = owned
                    self._state = RunnerLifecycle.STOP_INCOMPLETE
        return problems

    async def stop(self) -> None:
        """STOPPED: no-op. RUNNING: STOPPING -> settle -> STOPPED. STOP_INCOMPLETE: settle what is left again.
        STARTING / STOPPING: RuntimeError, nothing changes. When a task cannot be ended, or the lock/runtime connection
        cannot be confirmed closed, the Runner becomes STOP_INCOMPLETE (connection and registry kept) and the original
        error is raised (RunnerStopIncomplete for live tasks). Nothing here converts or hides a cleanup error.

        Outcome: no cancellation + clean -> returns; cancellation only -> the first CancelledError after the settle
        finished; cleanup error only -> that error; both -> the first CancelledError `from` the cleanup error.
        If the process itself is going away the OS closes the connection (and so the lock); a caller that keeps
        running after a failed stop still holds the lock/connection until a later stop() succeeds. Process-level
        failures (interpreter exit) are not covered."""
        async with self._lifecycle_lock:
            if self._state is RunnerLifecycle.STOPPED:
                return
            if self._state in (RunnerLifecycle.STARTING, RunnerLifecycle.STOPPING):
                raise RuntimeError(f"Runner cannot stop from {self._state.value}")
            self._state = RunnerLifecycle.STOPPING          # the lock is released HERE: nothing below holds it
            self._generation += 1                           # a recovery of the previous generation does no side effects
        settle = asyncio.ensure_future(self._settle_stop())
        cancels = await self._wait_shielded(settle)
        error = None if settle.cancelled() else settle.exception()
        for extra in cancels[1:]:
            log.warning("further cancellation while stopping the Runner: %r", extra)
        if error is not None:
            log.error("Runner stop did not complete cleanly: %r", error)
        if cancels and error is not None:
            raise cancels[0] from error
        if cancels:
            raise cancels[0]
        if error is not None:
            raise error

    async def _settle_stop(self) -> None:
        """Cleans up, then makes the final lifecycle transition from the REAL resources — even when the cleanup failed —
        and finally re-raises the original cleanup error unchanged."""
        error: BaseException | None = None
        try:
            await self._shutdown_resources()
        except BaseException as exc:  # noqa: BLE001 - re-raised below after the lifecycle is settled
            error = exc
        try:
            async with self._lifecycle_lock:
                self._prune_done()
                if self._loop_task is not None and self._loop_task.done():
                    self._loop_task = None
                self._reap_recovery_locked()
                clean = (not self._inflight and self._lock_conn is None and self._loop_task is None
                         and self._recovery_clean())
                if clean:
                    self._wake.clear()
                self._state = RunnerLifecycle.STOPPED if clean else RunnerLifecycle.STOP_INCOMPLETE
        except BaseException as exc:  # noqa: BLE001
            log.critical("the Runner lifecycle could not be settled and may stay in %s", self._state.value)
            if error is None:
                error = exc
            else:
                error.add_note(f"the lifecycle transition also failed: {exc!r}")
        if error is None and self._state is RunnerLifecycle.STOP_INCOMPLETE:
            raise RunnerStopIncomplete([str(e.run_id) if e.run_id else "<no lease yet>"
                                        for e in self._inflight.values()], self._recovery_description())
        if error is not None:
            raise error

    async def _shutdown_resources(self) -> None:
        """Best-effort, in order: loop task end -> request-recovery settlement -> in-flight drain -> advisory-lock
        connection cleanup. Every step runs even when an earlier one failed (a recovery that cannot be settled never
        prevents the drain); errors are collected and combined (one error is raised as itself, several as a group).
        The advisory-lock connection is released only when the drain succeeded AND no recovery is live or unsettled
        (recomputed AFTER the settlement: an error on the way does not by itself keep the lock). Its close is not
        bounded here (current contract). Never changes the lifecycle; RunnerStopIncomplete if tasks are still alive."""
        errors: list[BaseException] = []
        self._wake.set()
        loop_task = self._loop_task
        if loop_task is not None:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
            self._loop_task = None                          # only after the loop task is confirmed finished
        try:
            await self._settle_recovery_for_stop()
        except BaseException as exc:  # noqa: BLE001 - collected; the drain still runs
            errors.append(exc)
        drained = True
        try:
            await self._drain()
        except BaseException as exc:  # noqa: BLE001
            drained = False
            errors.append(exc)
        if drained and self._recovery_clean():
            try:
                await self._release_lock_connection()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        elif drained:
            log.error("runner stop: the request recovery is unsettled (%s); the advisory lock is kept",
                      self._recovery_description())
        if errors:
            raise _combine(errors)

    async def _unlock_best_effort(self, conn: AsyncConnection) -> None:
        """Closing the connection releases a session lock anyway, so an unlock problem (or a false result) is logged."""
        try:
            await conn.execute(text("select pg_advisory_unlock(:k)"), {"k": self._key})
        except Exception as exc:  # noqa: BLE001
            log.warning("advisory unlock failed (the lock is released by closing its connection): %r", exc)

    async def _release_lock_connection(self) -> None:
        """The Runner keeps owning `_lock_conn` until the close is CONFIRMED; a failed or cancelled close leaves it in
        place (and raises) so that a later stop() can retry the same connection."""
        conn = self._lock_conn
        if conn is None:
            return
        await self._unlock_best_effort(conn)
        await conn.close()
        if self._lock_conn is conn:
            self._lock_conn = None

    @staticmethod
    async def _wait_shielded(settle: "asyncio.Future") -> list[asyncio.CancelledError]:
        """Waits until `settle` is finished no matter how often the caller is cancelled; returns the cancellations
        received (in order). The settle's own outcome is read by the caller."""
        cancels: list[asyncio.CancelledError] = []
        while True:
            try:
                await asyncio.shield(settle)
                return cancels
            except asyncio.CancelledError as exc:
                if settle.done():
                    own = settle.cancelled() or isinstance(settle.exception(), asyncio.CancelledError)
                    if not own:
                        cancels.append(exc)                  # the caller's cancellation arrived as the settle ended
                    return cancels
                cancels.append(exc)
            except BaseException:  # noqa: BLE001 - the settle itself failed; the caller reads and combines its error
                return cancels

    def _prune_done(self) -> None:
        """Drops registry entries of tasks that already ended. Only pops: the done callback (`_on_done`) owns the
        exception consumption, the error log and the wake-up, and it stays correct when the entry is already gone."""
        for request_id, entry in list(self._inflight.items()):
            if entry.task is not None and entry.task.done():
                self._inflight.pop(request_id, None)

    async def _drain(self) -> None:
        """Grace wait -> revoke + CLI cancel for the STILL PENDING tasks -> bounded wait -> coroutine cancel -> bounded
        wait. Raises RunnerStopIncomplete when something is still alive."""
        self._prune_done()
        pending = {e.task for e in self._inflight.values() if e.task is not None and not e.task.done()}
        if pending:
            _done, pending = await asyncio.wait(pending, timeout=self._shutdown_grace)
        if pending:
            log.warning("runner stopping: asking %d running task(s) to stop", len(pending))
            for entry in list(self._inflight.values()):
                if entry.task in pending and entry.run_id is not None:   # never a task that already finished
                    self.leases.revoke(entry.run_id)          # the run records nothing more ...
                    self._cancel_cli(entry.run_id)            # ... and its CLI processes are stopped
            _done, pending = await asyncio.wait(pending, timeout=self._shutdown_grace)
        if pending:
            log.error("runner stopping: %d task(s) did not stop in time; cancelling their coroutines", len(pending))
            for task in pending:
                task.cancel()
            _done, pending = await asyncio.wait(pending, timeout=self._shutdown_grace)
        self._prune_done()
        alive = [e for e in self._inflight.values() if e.task is not None and not e.task.done()]
        if alive:
            log.error("runner stop incomplete: %d task(s) are still alive; the advisory lock is kept", len(alive))
            raise RunnerStopIncomplete([str(e.run_id) if e.run_id else "<no lease yet>" for e in alive])

    def _cancel_cli(self, run_id: uuid.UUID) -> None:
        for provider in self._providers:
            provider.cancel(str(run_id))

    def notify(self) -> None:
        self._wake.set()

    # --- recovery ---------------------------------------------------------------------------------------------
    async def recover(self, *, startup: bool = False) -> dict:
        """The only way recovery runs (callers: `start`, `_loop`, tests).

          startup=True   lifecycle STARTING and the advisory-lock connection held: every CLAIMED request belongs to a
                         dead runner;
          startup=False  lifecycle RUNNING: only CLAIMED requests past the grace period with NO live lease.
        Any other state raises RuntimeError without touching the database.

        The lifecycle lock is held only for the state check and for registering / reaping the operation; the DB work runs
        in its own task outside it, so `stop()` can always take the lock and switch to STOPPING. Return value: the summary
        dict (`requeued` / `closed` / `fenced` counts of the conditional UPDATEs that applied, plus `outcome`); a periodic
        call that finds a recovery already registered returns `{"outcome": "SKIPPED", ...}`. Otherwise it raises:
        the original error (FAILED_BEFORE_COMMIT), `RecoveryTimeout`, `RecoveryUnsettled`, the caller's own
        CancelledError, or RuntimeError when a shutdown cancelled the recovery."""
        return await self._recover(startup, checked=True)

    async def _recover(self, startup: bool, *, checked: bool) -> dict:
        """The recovery STAGES of one call: unresolved evidence first (CLEANUP of a session that could not be released,
        RECONCILIATION of an UNCERTAIN commit), then the MUTATION itself. A non-mutation stage lets the call go on only
        when it really resolved its evidence; the decision is taken from one `_stage_snapshot` made under the lifecycle
        lock. A live operation owned by somebody else is NOT judged here: the next `_register_recovery` (the
        authoritative gate) answers SKIPPED / RuntimeError for it.
        `checked=False` skips the lifecycle-state check (tests drive a Runner that was never start()ed)."""
        generation: int | None = None
        for _ in range(_MAX_RECOVERY_STAGES):
            ctx, skipped = await self._register_recovery(startup, checked, generation)
            if ctx is None:
                return skipped
            generation = ctx.generation
            result = await self._drive_recovery(ctx)                   # returns only once `ctx` has been reaped
            if ctx.kind is RecoveryKind.MUTATION:
                return await self._finish_mutation(ctx, result, startup, checked)
            async with self._lifecycle_lock:
                snap = self._stage_snapshot(ctx)
            stop = self._stage_blocked(ctx, snap, startup, checked)    # pure: reads only the snapshot
            if stop is not None:
                error_type, reason = stop
                raise error_type(result, reason) if error_type is RecoveryUnsettled else error_type(reason)
        raise RecoveryUnsettled(None, "recovery did not converge")    # unreachable: see _MAX_RECOVERY_STAGES

    @staticmethod
    def _recovery_not_allowed(startup: bool, state: RunnerLifecycle, lock_conn_held: bool) -> str | None:
        """Pure: why a recovery may not run in this lifecycle state (None = allowed). One definition for the gate
        (`_require_recovery_allowed`) and for the between-stages check."""
        if startup:
            if state is not RunnerLifecycle.STARTING or not lock_conn_held:
                return (f"startup recovery needs STARTING with the advisory lock held (state "
                        f"{state.value}, lock {'held' if lock_conn_held else 'not held'})")
        elif state is not RunnerLifecycle.RUNNING:
            return f"periodic recovery needs RUNNING (state {state.value})"
        return None

    def _require_recovery_allowed(self, startup: bool) -> None:
        reason = self._recovery_not_allowed(startup, self._state, self._lock_conn is not None)
        if reason is not None:
            raise RuntimeError(reason)

    def _stage_snapshot(self, ctx: "_RecoveryCtx") -> "_StageSnapshot":
        """THE CALLER MUST HOLD `_lifecycle_lock`: it reads every shared field the stage decision needs in one consistent
        view. (Same convention as `_reap_recovery_locked`.)"""
        return _StageSnapshot(
            reaped=ctx.result is not None and self._recovery is not ctx,
            cleanup_unsettled=self._recovery_cleanup_unsettled, unsettled=self._recovery_unsettled,
            generation=self._generation, state=self._state, lock_conn_held=self._lock_conn is not None)

    @staticmethod
    def _stage_blocked(ctx: "_RecoveryCtx", snap: "_StageSnapshot", startup: bool, checked: bool
                       ) -> "tuple[type[Exception], str] | None":
        """Pure (no Runner field is read): None = this non-mutation stage advanced, the call may register the next one;
        otherwise the error type and reason that end the call. Evidence is compared by IDENTITY."""
        if not snap.reaped:
            return RecoveryUnsettled, "the recovery operation was not reaped"
        if ctx.kind is RecoveryKind.CLEANUP:
            if snap.cleanup_unsettled is not None:
                if snap.cleanup_unsettled is ctx.evidence:
                    return RecoveryUnsettled, "connection release unconfirmed"
                # Impossible under the single-operation gate (nothing else registers while this cleanup runs): an
                # invariant violation, never a successful cleanup. The reap does not touch the foreign object.
                return RecoveryUnsettled, (f"recovery evidence invariant violation: cleanup ownership is "
                                           f"{id(snap.cleanup_unsettled):#x} but this cleanup settled "
                                           f"{id(ctx.evidence):#x}")
        else:                                                       # RECONCILIATION: `ctx.original` is being checked
            if snap.unsettled is ctx.original:
                verdict = ((ctx.result.summary or {}).get("verdict") if ctx.result is not None else None)
                detail = verdict.value if verdict is not None else "check did not complete"
                return RecoveryUnsettled, f"commit outcome unresolved ({detail})"
            if snap.unsettled is not None:                         # replaced by another evidence object: never "released"
                return RecoveryUnsettled, (f"recovery evidence invariant violation: unsettled evidence is "
                                           f"{id(snap.unsettled):#x} but the reconciliation checked "
                                           f"{id(ctx.original):#x}")
        if snap.generation != ctx.generation:
            return RuntimeError, _LIFECYCLE_CHANGED
        if checked and Runner._recovery_not_allowed(startup, snap.state, snap.lock_conn_held) is not None:
            return RuntimeError, _LIFECYCLE_CHANGED
        return None

    # --- recovery registration / reaping (everything below that touches the shared fields holds the lifecycle lock) ---
    def _recovery_clean(self) -> bool:
        return (self._recovery is None and self._recovery_unsettled is None
                and self._recovery_cleanup_unsettled is None)

    def _recovery_description(self) -> str | None:
        parts = []
        if self._recovery is not None:
            parts.append(f"{self._recovery.kind.value} operation still registered")
        if self._recovery_unsettled is not None:
            parts.append("commit outcome unconfirmed")
        if self._recovery_cleanup_unsettled is not None:
            parts.append("connection release unconfirmed")
        return ", ".join(parts) or None

    async def _register_recovery(self, startup: bool, checked: bool, generation: int | None = None
                                 ) -> tuple["_RecoveryCtx | None", dict | None]:
        """ONE critical section: state check -> generation check -> reap a finished operation -> single-run gate ->
        register the new one. Unresolved evidence is dealt with first (cleanup, then reconciliation); a mutation never
        starts on top of it. `generation` (given for the 2nd and later stage of one `_recover` call) must still be the
        current one: a stop()/start() in between ends the call instead of continuing under a newer lifecycle."""
        async with self._lifecycle_lock:
            if checked:
                self._require_recovery_allowed(startup)
            if generation is not None and generation != self._generation:
                raise RuntimeError(_LIFECYCLE_CHANGED)
            self._reap_recovery_locked()
            if self._recovery is not None:                 # a live (or unreaped, unsettled) operation owns the gate
                if startup:
                    raise RuntimeError("startup recovery found a registered recovery operation (invariant violation)")
                return None, {"outcome": RecoveryOutcome.SKIPPED.value, "requeued": 0, "cancelled": 0, "closed": 0,
                              "fenced": 0}
            return self._register_locked(startup), None

    def _register_locked(self, startup: bool) -> "_RecoveryCtx":
        if self._recovery_cleanup_unsettled is not None:
            ctx = _RecoveryCtx(RecoveryKind.CLEANUP, startup, self._generation, self._recovery_cleanup_unsettled)
        elif self._recovery_unsettled is not None:
            ctx = _RecoveryCtx(RecoveryKind.RECONCILIATION, startup, self._generation, RecoveryEvidence(),
                               original=self._recovery_unsettled)
        else:
            ctx = _RecoveryCtx(RecoveryKind.MUTATION, startup, self._generation, RecoveryEvidence())
        ctx.task = asyncio.create_task(self._recovery_operation(ctx), name=f"recovery-{ctx.kind.value.lower()}")
        ctx.task.add_done_callback(self._on_recovery_done)
        self._recovery = ctx
        return ctx

    def _on_recovery_done(self, task: asyncio.Task) -> None:
        """Consumes the outcome so nothing is left unretrieved, logs it and sets `_recovery_done`. It changes NO shared
        field: reaping (and everything it records) happens under the lifecycle lock. It must not set `_wake`: every
        loop iteration runs a recovery, so a wake-up per finished recovery would make the loop spin."""
        try:
            if task.cancelled():
                log.info("recovery operation was cancelled before it ran")
            elif task.exception() is not None:
                log.error("recovery operation ended with an unexpected error", exc_info=task.exception())
        except BaseException:  # noqa: BLE001 - a done callback must never leak
            log.exception("recovery done-callback failed")
        self._recovery_done.set()

    def _reap_recovery_locked(self) -> None:
        """Caller holds `_lifecycle_lock`. Collects a finished operation's result, records its outcome and only then
        removes it. Ownership of an unreleased connection / an unsettled commit moves to the `_recovery_*_unsettled`
        fields, so a finished task is never dropped while something is unresolved."""
        ctx = self._recovery
        if ctx is None or ctx.task is None or not ctx.task.done():
            return
        ctx.result = result = self._collect_result(ctx)
        ev = result.evidence
        if ctx.kind is RecoveryKind.MUTATION:
            if result.db is DbOutcome.UNCERTAIN:
                self._recovery_unsettled = ev
        elif ctx.kind is RecoveryKind.RECONCILIATION:
            verdict = (result.summary or {}).get("verdict")
            if (verdict in (Verdict.NO_CHANGES, Verdict.ROLLED_BACK, Verdict.COMMITTED)
                    and self._recovery_unsettled is ctx.original):
                self._recovery_unsettled = None            # only a CONFIRMED predicate releases the original evidence
        if not result.released:
            self._recovery_cleanup_unsettled = ev
        elif ctx.kind is RecoveryKind.CLEANUP and self._recovery_cleanup_unsettled is ctx.evidence:
            self._recovery_cleanup_unsettled = None
        self._recovery = None

    @staticmethod
    def _collect_result(ctx: "_RecoveryCtx") -> RecoveryResult:
        task, ev = ctx.task, ctx.evidence
        if not task.cancelled() and task.exception() is None:
            return task.result()
        # cancelled before it ran, or an unexpected error: classify from the evidence, keep the ownership
        released = ev.session is None or ev.conn_released
        if ev.phase is Phase.COMMITTING:
            db = DbOutcome.UNCERTAIN
        elif ev.phase is Phase.DONE:
            db = DbOutcome.COMMITTED
        else:
            db = DbOutcome.ROLLED_BACK if task.cancelled() else DbOutcome.FAILED_BEFORE_COMMIT
        return RecoveryResult(ev.db_outcome or db, ResourceOutcome.RELEASED if released else ResourceOutcome.FAILED, ev,
                              error=None if task.cancelled() else task.exception(), cancelled=task.cancelled())

    async def _recovery_operation(self, ctx: "_RecoveryCtx") -> RecoveryResult:
        ms = self._recovery_statement_ms
        if ctx.kind is RecoveryKind.MUTATION:
            return await run_recovery_transaction(
                self._recovery_sessions, lambda s, ev: self._recovery_body(ctx.startup, s, ev), ctx.evidence, ms)
        if ctx.kind is RecoveryKind.RECONCILIATION:
            return await run_recovery_transaction(
                self._recovery_sessions, lambda s, ev: reconcile_body(ctx.original, s, ev), ctx.evidence, ms)
        return await resettle_resources(ctx.evidence)

    async def _drive_recovery(self, ctx: "_RecoveryCtx") -> RecoveryResult:
        """Waits for the registered operation with bounded waits only. `asyncio.wait_for` is deliberately not used: after
        its timeout it waits for the inner cancellation to finish, which has no bound. Here: wait up to the operation
        timeout -> cancel once -> wait up to the shutdown wait -> a task that is still alive stays registered (live).
        Why it was cancelled decides the outcome: the caller's own cancellation is re-raised (the original object), a
        deadline is always a RecoveryTimeout even if the cancelled operation rolled back or committed meanwhile."""
        caller_cancel: asyncio.CancelledError | None = None
        done: set = set()
        try:
            done, _pending = await asyncio.wait({ctx.task}, timeout=self._recovery_timeout)
        except asyncio.CancelledError as exc:
            caller_cancel = exc
        if caller_cancel is None and done:
            async with self._lifecycle_lock:
                self._reap_recovery_locked()
            return ctx.result
        why = CancelReason.CALLER_CANCEL if caller_cancel is not None else CancelReason.OPERATION_DEADLINE
        settle = asyncio.ensure_future(self._settle_recovery(ctx, why))
        cancels = await self._wait_shielded(settle)        # further cancellations cannot interrupt the settlement
        error = None if settle.cancelled() else settle.exception()
        if caller_cancel is not None:
            for extra in cancels:
                caller_cancel.add_note(f"further cancellation while settling the recovery: {extra!r}")
            if error is not None:
                raise caller_cancel from error
            raise caller_cancel
        if cancels:                                        # cancelled while the deadline settlement ran: the cancel wins
            for extra in cancels[1:]:
                cancels[0].add_note(f"further cancellation while settling the recovery: {extra!r}")
            if error is not None:
                raise cancels[0] from error
            raise cancels[0]
        timeout = RecoveryTimeout(ctx.result, pending=ctx.result is None)
        if error is not None:
            raise timeout from error
        raise timeout

    async def _settle_recovery(self, ctx: "_RecoveryCtx", why: CancelReason) -> bool:
        """Cancels `ctx`'s operation ONCE (a second caller waits again but never cancels twice or replaces the task),
        waits at most the shutdown wait, then reaps. True when the operation is finished and reaped; False means it is
        still live and stays registered (a later call settles the same task again)."""
        task = ctx.task
        async with self._lifecycle_lock:
            if not task.done():
                if ctx.reason is CancelReason.NONE:
                    ctx.reason = why
                if ctx.cancel_calls == 0:                  # issued at most once per operation
                    ctx.cancel_calls = 1
                    task.cancel()
        await asyncio.wait({task}, timeout=self._recovery_shutdown_wait)
        async with self._lifecycle_lock:
            self._reap_recovery_locked()
        return ctx.result is not None

    async def _settle_recovery_for_stop(self) -> None:
        """stop() / failed start: cancel a live operation (bounded), then try to resolve unresolved evidence — a CLEANUP
        pass (release the session) before a RECONCILIATION pass (read-only commit check) — each through the normal gate.
        What cannot be resolved stays recorded and makes the Runner STOP_INCOMPLETE; errors are raised after the
        attempts. Reconciliation settles the DB outcome only; it never counts as a connection release."""
        async with self._lifecycle_lock:
            self._reap_recovery_locked()
            ctx = self._recovery
        if ctx is not None:
            await self._settle_recovery(ctx, CancelReason.SHUTDOWN)
            if self._recovery is not None:
                return                                     # still live: unresolved, the next stop() settles it again
        for _ in range(2):
            if self._recovery_cleanup_unsettled is None and self._recovery_unsettled is None:
                return
            registered, _skipped = await self._register_recovery(False, False)
            if registered is None:
                return
            await self._drive_recovery(registered)         # RecoveryTimeout / cancellation propagate to the collector

    async def _finish_mutation(self, ctx: "_RecoveryCtx", result: RecoveryResult, startup: bool, checked: bool) -> dict:
        """Turns the reaped MUTATION result into the return value / exception (see `recover`)."""
        if result.db is DbOutcome.UNCERTAIN:
            raise RecoveryUnsettled(result, "the commit outcome is unknown")
        if result.db is DbOutcome.FAILED_BEFORE_COMMIT and result.error is not None:
            for problem in result.cleanup_errors:
                result.error.add_note(f"recovery cleanup problem: {problem!r}")
            raise result.error
        if not result.released:
            raise RecoveryUnsettled(result, f"the connection release is {result.resource.value}")
        if result.db is not DbOutcome.COMMITTED:           # cancelled by a shutdown while this caller waited
            raise RuntimeError(f"recovery was cancelled ({ctx.reason.value}) and rolled back")
        summary = dict(result.summary or {})
        summary["outcome"] = RecoveryOutcome.COMMITTED.value
        # The DB result stands whatever happened to the lifecycle, but follow-up side effects only run for the same
        # generation, a lifecycle that still allows recovery, and a released connection (checked above).
        if ctx.generation != self._generation or (
                checked and self._state not in (RunnerLifecycle.STARTING, RunnerLifecycle.RUNNING)):
            summary["rest_skipped"] = True
            return summary
        return await self._recover_rest(summary)

    async def _recovery_body(self, startup: bool, session, evidence: RecoveryEvidence) -> dict:
        """The request changes, inside the recovery transaction. Candidates (id, claim token, intent) are all recorded
        BEFORE the first UPDATE and every UPDATE is the token-fenced conditional one: a request that changed after it was
        read (another owner, a new claim) is `fenced` and left exactly as it is. Only applied UPDATEs are counted."""
        summary = {"requeued": 0, "cancelled": 0, "closed": 0, "fenced": 0}
        repo = ExecutionRequestRepository(session)
        stale = (list((await session.execute(select(ExecutionRequest).where(ExecutionRequest.status == "CLAIMED")))
                      .scalars())
                 if startup else await repo.stale_claimed(self._now(), self._claim_grace))
        planned = []
        for req in stale:
            task = await TaskRepository(session).get(req.task_id)
            finished = task is None or TaskState(task.status) in TERMINAL_TASK_STATES
            planned.append(Candidate(req.id, req.claim_token, Intent.FINISH_DONE if finished else Intent.REQUEUE))
        evidence.candidates = planned
        evidence.candidates_complete = True                # candidates and intents are final; UPDATEs start now
        for cand in planned:
            # A NULL token cannot occur for a CLAIMED row (claim_next always sets one); it would fall back to the
            # unconditional legacy update.
            if cand.intent is Intent.FINISH_DONE:
                applied = await repo.finish(cand.request_id, "DONE", "task already finished",
                                            token=cand.original_claim_token)
            else:
                # Re-consumption is safe: acquire() re-verifies the old run's CLI processes and, when it cannot
                # prove they are gone (or whether one was ever spawned), parks the task in a RECOVERY gate
                # instead of running anything. requeue() clears claim_token, so the old coroutine is fenced out.
                applied = await repo.requeue(cand.request_id, "requeued_after_stale_claim",
                                             token=cand.original_claim_token)
            cand.update_applied = bool(applied)
            if applied:
                summary["closed" if cand.intent is Intent.FINISH_DONE else "requeued"] += 1
            else:
                evidence.fenced_count += 1
                summary["fenced"] += 1
        return summary

    async def _recover_rest(self, summary: dict) -> dict:
        """Work that may measure a workspace (slow on a big repository), so it runs OUTSIDE the lifecycle lock. It only
        touches cancelled tasks without a run and write locks of finished tasks, never a live claim."""
        summary["cancelled"] = await self._cancel_idle_tasks()
        summary["reconciled"] = await self.workflow.locks.reconcile()    # write locks left by finished tasks
        if summary["requeued"]:
            self.notify()
        return summary

    async def _cancel_idle_tasks(self) -> int:
        """A cancellation request for a task nobody is running: nothing to terminate, finish it now (through the
        common terminal path, so a project write lock held by a parked task is released at once)."""
        async with self.db.transaction() as s:
            repo = TaskRepository(s)
            candidates = []
            for task_id in (await s.execute(select(Task.id).where(
                    Task.cancel_requested_at.is_not(None),
                    Task.status.notin_([str(x) for x in TERMINAL_TASK_STATES])))).scalars().all():
                if await repo.running_run(task_id) is None and not await repo.unterminated_executions(task_id):
                    candidates.append(task_id)               # cheap pre-check: no hashing for a live run
        done = 0
        for task_id in candidates:
            done += await self._cancel_one_idle(task_id)
        return done

    async def _cancel_one_idle(self, task_id: uuid.UUID) -> int:
        cancelled = 0

        async def work(s, task: Task, proof) -> None:
            nonlocal cancelled
            repo = TaskRepository(s)
            if await repo.running_run(task_id) is not None or await repo.unterminated_executions(task_id):
                return                                     # a live/unproven run exists: not ours to cancel here
            await s.execute(
                text("update execution_requests set status='DONE', finished_at=now(), note='task cancelled' "
                     "where task_id=:t and status in ('PENDING','CLAIMED')"), {"t": task_id})
            await SessionService().close_all_for_task(s, task_id)
            for step in await repo.steps_for_task(task_id):
                if StepState(step.status) in (StepState.RUNNING, StepState.WAITING_APPROVAL):
                    await change_step_state(s, step, StepState.CANCELLED, error="cancelled")
            await change_task_state(s, task, TaskState.CANCELLED, reason="cancelled", project_proof=proof)
            cancelled = 1

        await self.workflow.finalizer.finalize_task_with_proof(task_id, work)
        return cancelled

    # --- dispatch ---------------------------------------------------------------------------------------------
    def _busy_projects(self) -> list[uuid.UUID]:
        counts: dict[uuid.UUID, int] = {}
        for entry in self._inflight.values():
            if entry.project_id is not None:
                counts[entry.project_id] = counts.get(entry.project_id, 0) + 1
        return [pid for pid, n in counts.items() if n >= self._max_per_project]

    async def _dispatch(self) -> int:
        """Claims while a slot is free. The slot is checked first: nothing claimed ever waits for one."""
        started = 0
        while self._state is RunnerLifecycle.RUNNING and len(self._inflight) < self._max_tasks:
            async with self.db.transaction() as s:
                request = await ExecutionRequestRepository(s).claim_next(
                    self.runner_id, busy_project_ids=self._busy_projects())
                project_id = None
                if request is not None:
                    project_id = (await s.execute(select(Task.project_id).where(Task.id == request.task_id))
                                  ).scalar_one_or_none()
            if request is None:
                break
            self._launch(request, project_id)
            started += 1
        return started

    def _launch(self, request: ExecutionRequest, project_id: uuid.UUID | None) -> None:
        entry = _InFlight(project_id)
        entry.task = asyncio.create_task(self._run_request(request, entry), name=f"request-{request.id}")
        self._inflight[request.id] = entry
        entry.task.add_done_callback(lambda t, rid=request.id: self._on_done(rid, t))

    def _on_done(self, request_id: uuid.UUID, task: asyncio.Task) -> None:
        self._inflight.pop(request_id, None)
        if not task.cancelled() and task.exception() is not None:
            log.error("request %s ended with an unexpected error", request_id, exc_info=task.exception())
        self._wake.set()                                       # a slot is free: look for more work

    async def _run_request(self, request: ExecutionRequest, entry: _InFlight) -> None:
        try:
            await self.process(request, on_lease=lambda lease: setattr(entry, "run_id", lease.run_id))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one task's failure must not stop the others; the claim is recovered
            log.exception("request %s failed; it stays CLAIMED and is recovered after the grace period", request.id)

    # --- main loop ---------------------------------------------------------------------------------------------
    async def _loop(self) -> None:
        while self._state is RunnerLifecycle.RUNNING:
            try:
                recovered = await self.recover(startup=False)  # stale claims + cancellations, independent of slots
                if recovered.get("outcome") == RecoveryOutcome.SKIPPED.value:
                    # an earlier recovery is still registered (it timed out and could not be cancelled yet): nothing is
                    # claimed until it is reaped, the same as after a RecoveryTimeout / RecoveryUnsettled
                    await asyncio.sleep(self._poll)
                    continue
                await self._dispatch()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._poll)
                except TimeoutError:
                    pass
                self._wake.clear()                             # dispatch runs AFTER the clear: no wake-up is lost
            except asyncio.CancelledError:
                raise
            except RecoveryError as exc:                       # timeout / unsettled: logged, the loop survives; no dispatch
                log.error("runner recovery did not settle: %s", exc)  # happens until the next recover() resolved it
                await asyncio.sleep(self._poll)
            except RuntimeError as exc:
                if self._state is not RunnerLifecycle.RUNNING:
                    return                                 # stop() won the race: leave quietly
                log.exception("runner iteration failed")
                await asyncio.sleep(self._poll)
            except Exception:  # noqa: BLE001 - the loop must survive; claims are recovered by the grace rule
                log.exception("runner iteration failed")
                await asyncio.sleep(self._poll)

    async def process(self, request: ExecutionRequest, *, on_lease=None) -> str:
        """Handle one claimed request end to end. Returns a short result label (for tests / logs)."""
        token = request.claim_token
        async with self.db.transaction() as s:
            if not await ExecutionRequestRepository(s).fence_ok(request.id, token):
                return "FENCED"                                # requeued/re-claimed since: not ours any more
        acquired = await self.leases.acquire(request.task_id, fence=(request.id, token))
        if acquired.status is AcquireStatus.FENCED:
            return "FENCED"
        if acquired.status is AcquireStatus.HELD:
            await self._defer(request, f"held:{acquired.reason}")
            return "HELD"
        if acquired.status is AcquireStatus.UNCERTAIN:
            await self._park_uncertain(request, acquired.reason or "previous CLI not proven terminated")
            return "UNCERTAIN"
        lease = acquired.lease
        async with self.db.transaction() as s:
            project_id = (await s.execute(select(Task.project_id).where(Task.id == request.task_id))).scalar_one_or_none()
            attached = await ExecutionRequestRepository(s).attach_run(
                request.id, lease.run_id, token=token, reset_defer=project_id is None)
        if not attached:                                       # requeued between acquire and attach
            await self.leases.release(lease, "fenced")
            return "FENCED"
        if on_lease is not None:
            on_lease(lease)
        heartbeat = self.leases.start_heartbeat(lease, on_lost=self._lost(lease))
        try:
            status = await self.workflow.run_task(lease, request.task_id, resume_approval_id=request.approval_id,
                                                  request_id=request.id)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        async with self.db.transaction() as s:
            repo = ExecutionRequestRepository(s)
            if status == "LEASE_LOST":
                done = await repo.requeue(request.id, "lease_lost", token=token)   # the normal acquire/takeover continues
            elif status == "BLOCKED":                      # project busy: persistent, growing backoff, nothing consumed
                done = await repo.requeue(request.id, "project_busy", token=token,
                                          delay_seconds=defer_delay(request.defer_count))
            else:
                done = await repo.finish(request.id, "DONE", status, token=token)
        if not done:
            log.warning("request %s was fenced out before it could be finished (%s)", request.id, status)
        return status

    async def _defer(self, request: ExecutionRequest, note: str) -> None:
        """Back to PENDING with a persistent, exponentially growing `not_before` (never a hot requeue loop)."""
        async with self.db.transaction() as s:
            await ExecutionRequestRepository(s).requeue(
                request.id, note, token=request.claim_token, delay_seconds=defer_delay(request.defer_count))

    def _lost(self, lease: Lease):
        async def on_lost():
            log.error("lease %s lost: cancelling its CLI processes", lease.run_id)
            self._cancel_cli(lease.run_id)
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
            await ExecutionRequestRepository(s).finish(request.id, "NEEDS_REVIEW", reason[:120],
                                                       token=request.claim_token)

    @staticmethod
    async def _pending(s, task_id):
        from app.repositories.approval_repository import ApprovalRepository
        return await ApprovalRepository(s).pending_for_task(task_id)
