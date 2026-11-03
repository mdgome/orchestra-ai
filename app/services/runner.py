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
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import Database
from app.models.states import ApprovalType, StepState, TaskState, TERMINAL_TASK_STATES
from app.models.tables import ExecutionRequest, Task
from app.repositories.approval_repository import ExecutionRequestRepository
from app.repositories.task_repository import TaskRepository
from app.services.human_gate_service import HumanGateService
from app.services.lease_service import AcquireStatus, Lease, LeaseService
from app.services.request_backoff import defer_delay
from app.services.session_service import SessionService
from app.services.state_service import change_step_state, change_task_state
from app.services.workflow_service import WorkflowService
from app.validation import policies as P

log = logging.getLogger(__name__)


class RunnerAlreadyActive(RuntimeError):
    pass


class RunnerStopIncomplete(RuntimeError):
    """stop() could not end every running task. The advisory lock connection and the registry are KEPT: a live
    coroutine may still change the database or a workspace, so the lock must not be reported as released. Call stop()
    again."""

    def __init__(self, run_ids: list[str]):
        super().__init__(f"{len(run_ids)} task(s) are still running after the shutdown grace period")
        self.run_ids = run_ids


class RunnerLifecycle(enum.Enum):
    """Process-internal state of one Runner object. Never persisted: it is not a DB model, migration or task state."""
    STOPPED = "STOPPED"                  # no loop, no advisory-lock connection, nothing in flight
    STARTING = "STARTING"                # start() is installing its resources (lock connection, startup recovery)
    RUNNING = "RUNNING"                  # lock connection held, loop alive
    STOPPING = "STOPPING"                # stop() is draining; nothing new is claimed
    # a stop (or a failed start) could not confirm everything is gone: a live in-flight task and/or a runtime connection
    # (the advisory-lock connection, or one whose close failed) is kept; no loop. start/recover refuse; stop() retries.
    STOP_INCOMPLETE = "STOP_INCOMPLETE"


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
                 shutdown_grace_seconds: float = P.DEFAULT_RUNNER_SHUTDOWN_GRACE_SECONDS):
        self.db, self.leases, self.workflow, self.gates = db, leases, workflow, gates
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

    @property
    def runner_id(self) -> str:
        return self.leases.runner_id

    @property
    def in_flight(self) -> int:
        return len(self._inflight)

    @property
    def lifecycle(self) -> RunnerLifecycle:
        return self._state

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
            if self._inflight or self._lock_conn is not None or self._loop_task is not None:
                raise RuntimeError("Runner is STOPPED but still holds resources")
            self._state = RunnerLifecycle.STARTING
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
            if owned is not None:
                if owned is self._lock_conn:
                    await self._unlock_best_effort(owned)
                try:
                    await owned.close()
                    closed = True
                except Exception as exc:  # noqa: BLE001 - recorded; the connection is kept for a later stop()
                    problems.append(exc)
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
                clean = not self._inflight and self._lock_conn is None and self._loop_task is None
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
                                        for e in self._inflight.values()])
        if error is not None:
            raise error

    async def _shutdown_resources(self) -> None:
        """Loop task end -> in-flight drain -> runtime connection cleanup, nothing else. Never changes the lifecycle.
        RunnerStopIncomplete if tasks are still alive; any other error propagates unchanged."""
        self._wake.set()
        loop_task = self._loop_task
        if loop_task is not None:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
            self._loop_task = None                          # only after the loop task is confirmed finished
        await self._drain()
        await self._release_lock_connection()

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
        """The only way recovery runs (callers: `start`, `_loop`, tests). The state is checked under the lifecycle lock
        and the short request-recovery transaction completes while it is held, so a concurrent stop() either runs after
        it or has already left RUNNING (then this raises and changes nothing).

          startup=True   lifecycle STARTING and the advisory-lock connection held: every CLAIMED request belongs to a
                         dead runner;
          startup=False  lifecycle RUNNING: only CLAIMED requests past the grace period with NO live lease.
        Any other state raises RuntimeError without touching the database."""
        async with self._lifecycle_lock:
            self._require_recovery_allowed(startup)
            summary = await self._recover_requests_locked(startup)
        return await self._recover_rest(summary)

    def _require_recovery_allowed(self, startup: bool) -> None:
        if startup:
            if self._state is not RunnerLifecycle.STARTING or self._lock_conn is None:
                raise RuntimeError(f"startup recovery needs STARTING with the advisory lock held (state "
                                   f"{self._state.value}, lock {'held' if self._lock_conn else 'not held'})")
        elif self._state is not RunnerLifecycle.RUNNING:
            raise RuntimeError(f"periodic recovery needs RUNNING (state {self._state.value})")

    async def _recover_requests_locked(self, startup: bool) -> dict:
        """The short transaction that requeues / closes CLAIMED requests. THE CALLER MUST HOLD `_lifecycle_lock` and
        have passed `_require_recovery_allowed`; never call it from another module."""
        summary = {"requeued": 0, "cancelled": 0, "closed": 0}
        async with self.db.transaction() as s:
            repo = ExecutionRequestRepository(s)
            stale = (list((await s.execute(select(ExecutionRequest).where(ExecutionRequest.status == "CLAIMED"))).scalars())
                     if startup else await repo.stale_claimed(self._now(), self._claim_grace))
            for req in stale:
                task = await TaskRepository(s).get(req.task_id)
                if task is None or TaskState(task.status) in TERMINAL_TASK_STATES:
                    await repo.finish(req.id, "DONE", "task already finished")
                    summary["closed"] += 1
                    continue
                # Re-consumption is safe: acquire() re-verifies the old run's CLI processes and, when it cannot
                # prove they are gone (or whether one was ever spawned), parks the task in a RECOVERY gate
                # instead of running anything. requeue() clears claim_token, so the old coroutine is fenced out.
                await repo.requeue(req.id, "requeued_after_stale_claim")
                summary["requeued"] += 1
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
                await self.recover(startup=False)              # stale claims + cancellations, independent of slots
                await self._dispatch()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._poll)
                except TimeoutError:
                    pass
                self._wake.clear()                             # dispatch runs AFTER the clear: no wake-up is lost
            except asyncio.CancelledError:
                raise
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
