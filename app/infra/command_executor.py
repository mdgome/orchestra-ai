"""Runs external commands via asyncio.create_subprocess_exec (never a shell).

Responsibilities: argument-list only, env allowlist, cwd validation, timeout / idle detection,
cooperative cancel, process-tree cleanup, bounded output capture, explicit outcome.
The executor never raises on a failing command: failures are returned as CommandResult.outcome.

B2 (docs/design/process-lifecycle-hardening.md): a command started with a `TrackedRun` is owned as a tree through a
Job Object (Windows). The root's exit is read from `proc.returncode` first (never from `proc.wait()`), cleanup runs
in ONE shielded settlement coroutine (`_settle_process`) that starts at the spawn, output draining is bounded for every
command, and a tracked end is `EXITED`/`TERMINATED` only when the Job is proven empty. POSIX has no process tracking:
a TrackedRun there degrades to untracked execution (`TrackedRun.degraded == "UNTRACKED_PLATFORM"`).
"""
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from app.infra.process_control import (
    CONFIRMED_GONE, IdentityFenced, JobError, ProcessIdentity, TreeJob, capture_identity, default_job_factory,
    terminate_tree,
)
from app.infra.workspace_guard import WorkspaceGuard
from app.models.states import CliOutcome, ProcessLifecycle, TreeProof
from app.validation import policies as P
from app.validation.exceptions import FailClosed

log = logging.getLogger(__name__)


@dataclass
class CommandResult:
    argv: list[str]
    outcome: CliOutcome
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    identity: ProcessIdentity | None = None
    termination: str | None = None
    truncated: bool = False
    # B2 additions (4): descriptive fields next to `outcome`/`termination`, which keep today's vocabulary.
    lifecycle: ProcessLifecycle | None = None
    tree: TreeProof = TreeProof.UNKNOWN
    spawn_failed: bool = False
    survivors: bool = False          # descendants outlived the root and were terminated
    reason: str | None = None        # JOB_ASSIGN_FAILED, PUMP_DRAIN_TIMEOUT, ... (never a `termination` value)


@dataclass
class TrackedRun:
    """The caller's handle of a tracked command (7.1). The executor sets `spawned` right after the process exists and
    fills `result` BEFORE it returns or re-raises (also CancelledError), so the caller can always settle its row."""
    execution_id: object
    run_key: str | None = None
    cancel_scope: str | None = None
    spawned: bool = False
    result: CommandResult | None = None
    degraded: str | None = None      # "UNTRACKED_PLATFORM": tracking is not offered here, the command ran untracked


@dataclass
class CommandSpec:
    argv: list[str]
    cwd: Path
    timeout_seconds: int = P.DEFAULT_CLI_TIMEOUT_SECONDS
    idle_seconds: int | None = P.CLI_IDLE_SECONDS
    stdin_text: str | None = None          # None -> stdin is closed (non-interactive)
    extra_env: dict[str, str] = field(default_factory=dict)
    # Trusted root for a registered project workspace outside WORKSPACE_ROOT; None -> the default guard.
    workspace_root: Path | None = None
    # Test commands run project code: they never receive the CLI providers' authentication variables.
    drop_cli_auth: bool = False


# Hook called right after the process is spawned, so callers can persist (pid, create_time).
OnStarted = Callable[[ProcessIdentity | None], Awaitable[None]]
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
_UNPROVEN_OK = {str(t) for t in CONFIRMED_GONE} | {"KILLED_BY_HANDLE"}


def build_env(extra: dict[str, str] | None = None, *, drop_cli_auth: bool = False) -> dict[str, str]:
    allowed = {k.upper() for k in P.ENV_ALLOWLIST}
    if drop_cli_auth:
        allowed -= {k.upper() for k in P.CLI_AUTH_ENV}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(extra or {})
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def resolve_executable(name: str) -> str:
    """Windows: `claude`/`codex` are .cmd/.exe shims; create_subprocess_exec needs the real path."""
    found = shutil.which(name)
    if not found:
        raise FailClosed("executable not found", {"executable": name})
    return found


def _prefix_matches(prefix: str, key: str) -> bool:
    """A cancel prefix P refuses / reaches a key K iff K == P or K starts with P + ':' (7.1 key namespaces)."""
    return key == prefix or key.startswith(prefix + ":")


def _recorded_under_scope(scope: str, prefix: str) -> bool:
    """A cancel(P) is recorded while scope S is open iff it would have matched some key under S (9)."""
    return scope == prefix or scope.startswith(prefix + ":") or prefix.startswith(scope + ":")


class _SpawnAbort(Exception):
    """A deterministic failure while the tracked process is still suspended (user code never ran)."""

    def __init__(self, reason: str, outcome: CliOutcome = CliOutcome.ERROR, cause: BaseException | None = None):
        super().__init__(reason)
        self.reason, self.outcome, self.cause = reason, outcome, cause


@dataclass
class _Run:
    """State of one command from the spawn on; everything `_settle_process` needs."""
    argv: list[str]
    tracking: bool
    started: float
    tracked: TrackedRun | None = None
    job: TreeJob | None = None
    job_assigned: bool = False
    proc: asyncio.subprocess.Process | None = None
    identity: ProcessIdentity | None = None
    phase: str = "RUNNING"            # SUSPENDED (tracked, before resume) | RUNNING
    decision: str | None = None       # the FIRST reason: CANCELLED | TIMEOUT | IDLE | ERROR; None = natural exit
    abort: _SpawnAbort | None = None
    run_started: float = 0.0
    last_output: float = 0.0
    truncated: bool = False
    reason_code: str | None = None
    out: bytearray = field(default_factory=bytearray)
    err: bytearray = field(default_factory=bytearray)
    pumps: list[asyncio.Task] = field(default_factory=list)
    settle: asyncio.Future | None = None

    def decide(self, decision: str) -> None:
        if self.decision is None:
            self.decision = decision


class CommandExecutor:
    def __init__(self, guard: WorkspaceGuard, max_concurrent: int | None = None, *,
                 job_factory: Callable[[], TreeJob] | None = None, tracking: bool | None = None):
        """`tracking=None` follows the platform (Windows: Job Objects; POSIX: none, TrackedRun degrades); `False` forces
        the degrade; `job_factory` replaces the Job implementation (tests)."""
        self._guard = guard
        self._cancel_events: dict[str, asyncio.Event] = {}
        # Bounds concurrently running child processes (CLIs and test commands) across every task.
        self._slots = asyncio.Semaphore(max_concurrent) if max_concurrent else None
        factory = job_factory or default_job_factory()
        self._job_factory = factory if tracking is not False else None
        # 9: a cancel recorded while a tracked scope is open, keyed by the scope; never stored in _cancel_events.
        self._cancelled_prefixes: dict[str, set[str]] = {}
        self._scope_depth: dict[str, int] = {}
        # 5.1 step 11 / 7.3: Jobs whose emptiness could not be proven stay open here (key: execution id).
        self._unconfirmed: dict[object, TreeJob] = {}

    @property
    def tracking_enabled(self) -> bool:
        return self._job_factory is not None

    # --- unconfirmed Jobs (7.3, 9 slot policy) -------------------------------------------------------------------------
    def unconfirmed_job_count(self) -> int:
        return len(self._unconfirmed)

    def retained_job(self, execution_id: object) -> TreeJob | None:
        return self._unconfirmed.get(execution_id)

    def has_retained(self, execution_id: object) -> bool:
        """True while a Job of this execution is retained, i.e. a spawned process was not proven gone."""
        return execution_id in self._unconfirmed

    async def resettle_retained(self, execution_id: object) -> bool:
        """Terminates a retained Job again and polls it; True (and the entry dropped, handle closed) only when it is
        proven empty."""
        job = self._unconfirmed.get(execution_id)
        if job is None:
            return False
        proven, _ = await self._kill_and_prove(job)
        if proven:
            self._unconfirmed.pop(execution_id, None)
            self._close_job(job)
        return proven

    # --- cancellation scopes (9) ---------------------------------------------------------------------------------------
    @contextmanager
    def tracked_scope(self, scope_key: str) -> Iterator[None]:
        """While open, `cancel()` also records its prefix so that a command which is started LATER (the gap between two
        commands of one policy) is refused. The record is removed when the scope ends."""
        self._scope_depth[scope_key] = self._scope_depth.get(scope_key, 0) + 1
        self._cancelled_prefixes.setdefault(scope_key, set())
        try:
            yield
        finally:
            self._scope_depth[scope_key] -= 1
            if self._scope_depth[scope_key] <= 0:
                self._scope_depth.pop(scope_key, None)
                self._cancelled_prefixes.pop(scope_key, None)

    def _refused_by_scope(self, key: str) -> bool:
        return any(_prefix_matches(p, key) for prefixes in self._cancelled_prefixes.values() for p in prefixes)

    async def _acquire_slot(self, cancel_event: asyncio.Event) -> bool:
        """Waits for a process slot. Returns True only when the caller now OWNS one permit (and `run`'s `finally`
        releases it, exactly once); False when no permit is held (a cancellation request ended the wait).

        Every internal task is reaped on every path (normal, cancel event, outer CancelledError, any other
        exception) by `_settle`, which is shielded: a second cancellation arriving while it runs is remembered and
        re-raised only AFTER the permit has been accounted for. Cancel wins when it races with the permit."""
        if cancel_event.is_set():
            return False
        acquire = asyncio.ensure_future(self._slots.acquire())
        waiter = asyncio.ensure_future(cancel_event.wait())
        outer: BaseException | None = None
        try:
            await asyncio.wait({acquire, waiter}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException as exc:      # noqa: BLE001 - external cancellation or anything else: settle first
            outer = exc
        settle = asyncio.ensure_future(self._settle(acquire, waiter))
        while True:
            try:
                got = await asyncio.shield(settle)
                break
            except asyncio.CancelledError as exc:
                if settle.done() and not settle.cancelled():
                    got = settle.result()
                    outer = outer or exc
                    break
                outer = outer or exc      # cancelled again while cleaning up: finish the cleanup, then re-raise
        keep_permit = got and outer is None and not cancel_event.is_set()
        if got and not keep_permit:
            self._slots.release()         # a permit we obtained but will not use: returned exactly once
        if outer is not None:
            raise outer
        return keep_permit

    async def _settle(self, acquire: asyncio.Future, waiter: asyncio.Future) -> bool:
        """Cancels both helper tasks, waits until they have really finished, and reports whether `acquire`
        actually obtained a permit."""
        acquire.cancel()
        waiter.cancel()
        await asyncio.gather(acquire, waiter, return_exceptions=True)
        return acquire.done() and not acquire.cancelled() and acquire.exception() is None

    def cancel(self, run_key: str) -> None:
        """Cancels the run with this key, or every run whose key is `<run_key>:...` (all steps of a lease)."""
        for key, event in list(self._cancel_events.items()):
            if key == run_key or key.startswith(run_key + ":"):
                event.set()
        for scope, prefixes in self._cancelled_prefixes.items():
            if _recorded_under_scope(scope, run_key):
                prefixes.add(run_key)             # what is stored is P, never the scope S

    async def run(self, spec: CommandSpec, *, run_key: str | None = None,
                  on_started: OnStarted | None = None, tracked: TrackedRun | None = None) -> CommandResult:
        if not spec.argv or not all(isinstance(a, str) for a in spec.argv):
            raise FailClosed("argv must be a non-empty list of strings")
        guard = self._guard if spec.workspace_root is None else WorkspaceGuard(spec.workspace_root)
        cwd = guard.resolve_inside(spec.cwd, must_exist=True)
        argv = [resolve_executable(spec.argv[0]), *spec.argv[1:]]
        if run_key is None and tracked is not None:
            run_key = tracked.run_key
        if run_key and self._refused_by_scope(run_key):
            return self._not_started(argv, "cancelled by a pending cancel of its scope", tracked)
        cancel_event = asyncio.Event()
        if run_key:
            self._cancel_events[run_key] = cancel_event
        holds_slot = False
        if self._slots is not None:
            try:
                holds_slot = await self._acquire_slot(cancel_event)
            except BaseException:
                self._cancel_events.pop(run_key or "", None)
                raise
            if not holds_slot:
                self._cancel_events.pop(run_key or "", None)
                return self._not_started(argv, "cancelled before a process slot was free", tracked)
        try:
            if cancel_event.is_set():     # a cancellation that is already pending never spawns a process
                self._cancel_events.pop(run_key or "", None)
                return self._not_started(argv, "cancelled before it started", tracked)
            return await self._execute(spec, argv, cwd, cancel_event, run_key, on_started, tracked)
        finally:
            if holds_slot:
                self._slots.release()

    @staticmethod
    def _not_started(argv: list[str], message: str, tracked: TrackedRun | None) -> CommandResult:
        result = CommandResult(argv, CliOutcome.CANCELLED, None, "", message, 0.0,
                               lifecycle=ProcessLifecycle.NOT_STARTED, tree=TreeProof.EMPTY)
        if tracked is not None:
            tracked.result = result
        return result

    # --- the run ------------------------------------------------------------------------------------------------------
    async def _execute(self, spec: CommandSpec, argv: list[str], cwd: Path, cancel_event: asyncio.Event,
                       run_key: str | None, on_started: OnStarted | None,
                       tracked: TrackedRun | None) -> CommandResult:
        tracking = tracked is not None and self._job_factory is not None
        if tracked is not None and not tracking:
            tracked.degraded = "UNTRACKED_PLATFORM"
        run = _Run(argv=argv, tracking=tracking, started=time.monotonic(), tracked=tracked)
        try:
            return await self._drive(run, spec, cwd, cancel_event, on_started)
        finally:
            self._cancel_events.pop(run_key or "", None)

    async def _drive(self, run: _Run, spec: CommandSpec, cwd: Path, cancel_event: asyncio.Event,
                     on_started: OnStarted | None) -> CommandResult:
        if run.tracking:
            try:
                run.job = self._job_factory()
            except Exception as exc:      # noqa: BLE001 - nothing was spawned: user code never ran
                return self._spawn_failed(run, getattr(exc, "code", "JOB_CREATE_FAILED"), f"{type(exc).__name__}: {exc}")
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            if run.tracking:
                from app.infra.windows_job import CREATE_SUSPENDED
                kwargs["creationflags"] |= CREATE_SUSPENDED      # no user code before the Job assignment (5.1 step 2)
        else:
            kwargs["start_new_session"] = True
        try:
            proc, outer = await self._spawn(run, spec, cwd, kwargs)
        except OSError as exc:
            self._close_job(run.job)
            return self._spawn_failed(run, "SPAWN_OSERROR", str(exc))
        except BaseException:
            self._close_job(run.job)
            raise
        run.proc = proc
        run.phase = "SUSPENDED" if run.tracking else "RUNNING"
        if run.tracked is not None:
            run.tracked.spawned = True

        # The settlement try-block starts at the spawn: a failing on_started also settles the tree.
        try:
            if outer is not None:
                raise outer
            self._start_pumps(run)
            await self._start_phase(run, spec, cancel_event, on_started)
            await self._wait_phase(run, spec, cancel_event)
        except _SpawnAbort as abort:
            run.abort = abort
            outer = None
        except BaseException as exc:      # noqa: BLE001 - including CancelledError
            outer = exc
            run.decide("CANCELLED" if isinstance(exc, asyncio.CancelledError) else "ERROR")
        run.settle = asyncio.ensure_future(self._settle_process(run))      # created once per run
        result, settle_outer = await self._await_shielded(run.settle)
        outer = outer or settle_outer
        if run.tracked is not None:
            run.tracked.result = result       # visible to the caller before anything is raised
        if outer is not None:
            raise outer                       # re-raised only after the tree is settled
        return result

    async def _spawn(self, run: _Run, spec: CommandSpec, cwd: Path, kwargs: dict):
        """create_subprocess_exec, uninterruptible once started: a CancelledError that arrives while the process is
        being created is remembered and handed back with the process so that it is settled, not leaked."""
        task = asyncio.ensure_future(asyncio.create_subprocess_exec(
            *run.argv, cwd=str(cwd), env=build_env(spec.extra_env, drop_cli_auth=spec.drop_cli_auth),
            stdin=asyncio.subprocess.PIPE if spec.stdin_text is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs))
        outer: BaseException | None = None
        while True:
            try:
                return await asyncio.shield(task), outer
            except asyncio.CancelledError as exc:
                outer = outer or exc
                if task.done():
                    if task.cancelled() or task.exception() is not None:
                        raise
                    return task.result(), outer
                # still being created: wait for it, then settle what exists

    @staticmethod
    async def _await_shielded(fut: asyncio.Future):
        """Awaits `fut` to its end however often this task is cancelled meanwhile (the `_acquire_slot` pattern);
        returns (result, the first CancelledError seen or None)."""
        outer: BaseException | None = None
        while True:
            try:
                return await asyncio.shield(fut), outer
            except asyncio.CancelledError as exc:
                outer = outer or exc
                if fut.done() and not fut.cancelled():
                    return fut.result(), outer
                if fut.cancelled():
                    raise

    def _start_pumps(self, run: _Run) -> None:
        async def pump(stream, buf: bytearray):
            while chunk := await stream.read(65536):
                run.last_output = time.monotonic()
                if len(buf) < MAX_CAPTURE_BYTES:
                    buf.extend(chunk)
                else:
                    run.truncated = True

        run.pumps = [asyncio.create_task(pump(run.proc.stdout, run.out)),
                     asyncio.create_task(pump(run.proc.stderr, run.err))]

    async def _start_phase(self, run: _Run, spec: CommandSpec, cancel_event: asyncio.Event,
                           on_started: OnStarted | None) -> None:
        proc = run.proc
        if run.tracking:
            # 5.1: assign (suspended) -> identity -> persist the identity -> resume. Any failure before the resume is a
            # SPAWN_FAILED: the command did not run.
            try:
                run.job.assign(proc.pid)
            except Exception as exc:      # noqa: BLE001
                raise _SpawnAbort(getattr(exc, "code", "JOB_ASSIGN_FAILED"), cause=exc) from exc
            run.job_assigned = True
            run.identity = capture_identity(proc.pid)
            if run.identity is None:
                raise _SpawnAbort("IDENTITY_UNAVAILABLE")
            if on_started is not None:
                await self._persist_identity(run, cancel_event, on_started)
            if cancel_event.is_set():
                raise _SpawnAbort("CANCELLED_BEFORE_RESUME", CliOutcome.CANCELLED)
            try:
                run.job.resume(proc.pid)
            except Exception as exc:      # noqa: BLE001
                raise _SpawnAbort(getattr(exc, "code", "RESUME_FAILED"), cause=exc) from exc
            run.phase = "RUNNING"
        else:
            run.identity = capture_identity(proc.pid)
            if on_started:
                await on_started(run.identity)      # may raise: the tree is settled first, then it propagates
        run.run_started = run.last_output = time.monotonic()      # the clock starts at resume, not at spawn
        if spec.stdin_text is not None:
            try:
                proc.stdin.write(spec.stdin_text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

    async def _persist_identity(self, run: _Run, cancel_event: asyncio.Event, on_started: OnStarted) -> None:
        """The fenced `on_started` of a tracked command, bounded and cancellable (5.1 step 5)."""
        callback = asyncio.ensure_future(on_started(run.identity))
        cancel_waiter = asyncio.ensure_future(cancel_event.wait())
        try:
            await asyncio.wait({callback, cancel_waiter}, timeout=P.TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (callback, cancel_waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(callback, cancel_waiter, return_exceptions=True)
        if callback.cancelled():
            if cancel_event.is_set():
                raise _SpawnAbort("CANCELLED_BEFORE_RESUME", CliOutcome.CANCELLED)
            raise _SpawnAbort("IDENTITY_PERSIST_FAILED")      # timeout (or the callback cancelled itself)
        exc = callback.exception()
        if exc is not None:
            raise _SpawnAbort("IDENTITY_FENCED" if isinstance(exc, IdentityFenced) else "IDENTITY_PERSIST_FAILED",
                              cause=exc)

    async def _wait_phase(self, run: _Run, spec: CommandSpec, cancel_event: asyncio.Event) -> None:
        """Root exit by `returncode` FIRST on every tick, then cancel / timeout / idle (9). `proc.wait()` is never the
        signal: it completes only after the pipes reached EOF."""
        proc = run.proc
        cancel_waiter = asyncio.ensure_future(cancel_event.wait())
        try:
            while True:
                if proc.returncode is not None:
                    return                      # natural exit: decision stays None
                if cancel_event.is_set():
                    run.decide("CANCELLED")
                    return
                now = time.monotonic()
                if now - run.run_started > spec.timeout_seconds:
                    run.decide("TIMEOUT")
                    return
                if spec.idle_seconds and now - run.last_output > spec.idle_seconds:
                    run.decide("IDLE")
                    return
                await asyncio.wait({cancel_waiter}, timeout=P.EXECUTOR_POLL_SECONDS)
        finally:
            cancel_waiter.cancel()
            await asyncio.gather(cancel_waiter, return_exceptions=True)

    # --- settlement (9) ------------------------------------------------------------------------------------------------
    async def _settle_process(self, run: _Run) -> CommandResult:
        """The single settlement coroutine of a run: kills or proves the tree, drains the pipes (bounded) and builds the
        result. Cleanup errors are logged and become TERMINATION_UNCONFIRMED, never swallowed."""
        try:
            if run.tracking:
                result = await (self._settle_suspended(run) if run.phase == "SUSPENDED" else self._settle_tracked(run))
            else:
                result = await self._settle_untracked(run)
        except Exception:      # noqa: BLE001
            log.exception("process settlement failed; reporting TERMINATION_UNCONFIRMED")
            result = self._result(run, ProcessLifecycle.TERMINATION_UNCONFIRMED,
                                  TreeProof.UNKNOWN, "UNKNOWN", reason="CLEANUP_ERROR")
        self._release_job(run, result)
        return result

    async def _settle_tracked(self, run: _Run) -> CommandResult:
        natural = run.decision is None
        proven, termination, survivors = await self._prove_tree(run, natural)
        await self._wait_returncode(run.proc, 0.0 if natural else P.PROCESS_TERMINATE_GRACE_SECONDS)
        await self._drain(run)
        if proven:
            lifecycle = ProcessLifecycle.EXITED if natural else ProcessLifecycle.TERMINATED
            return self._result(run, lifecycle, TreeProof.EMPTY, termination, survivors=survivors)
        tree = TreeProof.NOT_EMPTY if termination == "STILL_ALIVE" else TreeProof.UNKNOWN
        return self._result(run, ProcessLifecycle.TERMINATION_UNCONFIRMED, tree, termination, survivors=survivors)

    async def _prove_tree(self, run: _Run, natural: bool) -> tuple[bool, str | None, bool]:
        """(proven empty, `termination`, survivors). Natural exit: re-poll for TREE_SETTLE_POLL_SECONDS so a transient
        helper is not mistaken for a survivor; survivors are terminated BEFORE any pipe drain (5.1 step 9)."""
        job = run.job
        survivors = False
        if natural:
            try:
                remaining = await self._settle_window(job)
            except Exception as exc:      # noqa: BLE001
                return False, self._termination_of(exc), False
            if remaining == 0:
                return True, None, False
            survivors = True
        proven, termination = await self._kill_and_prove(job)
        return proven, termination, survivors

    async def _settle_window(self, job: TreeJob) -> int:
        deadline = time.monotonic() + P.TREE_SETTLE_POLL_SECONDS
        while True:
            count = job.active_processes()
            if count == 0 or time.monotonic() >= deadline:
                return count
            await asyncio.sleep(P.JOB_POLL_INTERVAL_SECONDS)

    async def _kill_and_prove(self, job: TreeJob) -> tuple[bool, str]:
        """TerminateJobObject (fixed exit code), then poll ActiveProcesses to 0 within the grace period. A single non-zero
        value is never a verdict; (True, "TERMINATED") only for 0."""
        try:
            job.terminate(P.JOB_TERMINATE_EXIT_CODE)
        except Exception as exc:      # noqa: BLE001
            return False, self._termination_of(exc)
        return await self._prove_empty(job)

    async def _prove_empty(self, job: TreeJob) -> tuple[bool, str]:
        deadline = time.monotonic() + P.PROCESS_TERMINATE_GRACE_SECONDS
        while True:
            try:
                count = job.active_processes()
            except Exception as exc:      # noqa: BLE001
                return False, self._termination_of(exc)
            if count == 0:
                return True, "TERMINATED"
            if time.monotonic() >= deadline:
                return False, "STILL_ALIVE"
            await asyncio.sleep(P.JOB_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _termination_of(exc: BaseException) -> str:
        return "ACCESS_DENIED" if getattr(exc, "access_denied", False) else "UNKNOWN"

    async def _settle_suspended(self, run: _Run) -> CommandResult:
        """The process exists but never ran (assignment, identity, persistence or resume failed, or a cancel came first):
        kill it and prove it gone. Proven -> SPAWN_FAILED (user code never ran); not proven -> unconfirmed."""
        proc = run.proc
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        if run.job_assigned:
            proven, termination = await self._kill_and_prove(run.job)
        else:
            proven = await self._wait_returncode(proc, P.PROCESS_TERMINATE_GRACE_SECONDS) is not None
            termination = "TERMINATED" if proven else "UNKNOWN"
        await self._drain(run)
        if proven:
            return self._result(run, ProcessLifecycle.SPAWN_FAILED, TreeProof.EMPTY, None, spawn_failed=True,
                                reason=run.abort.reason if run.abort else "CANCELLED_BEFORE_RESUME", exit_code=None)
        tree = TreeProof.NOT_EMPTY if termination == "STILL_ALIVE" else TreeProof.UNKNOWN
        return self._result(run, ProcessLifecycle.TERMINATION_UNCONFIRMED, tree, termination,
                            reason=run.abort.reason if run.abort else None)

    async def _settle_untracked(self, run: _Run) -> CommandResult:
        termination = None
        if run.decision is not None:
            termination = await self._terminate(run.proc, run.identity)
        await self._drain(run)
        if run.decision is None:
            lifecycle = ProcessLifecycle.EXITED
        elif termination in _UNPROVEN_OK:
            lifecycle = ProcessLifecycle.TERMINATED
        else:
            lifecycle = ProcessLifecycle.TERMINATION_UNCONFIRMED
        return self._result(run, lifecycle, TreeProof.UNKNOWN, termination)

    async def _terminate(self, proc, identity: ProcessIdentity | None) -> str:
        if identity is None:
            # Could not capture an identity: fall back to the Popen handle we own.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            return "KILLED_BY_HANDLE"
        result = await asyncio.to_thread(terminate_tree, identity, P.PROCESS_TERMINATE_GRACE_SECONDS)
        try:
            await asyncio.wait_for(proc.wait(), timeout=P.PROCESS_TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            pass
        return str(result)

    @staticmethod
    async def _wait_returncode(proc, seconds: float):
        """The root's return code, polled (never `proc.wait()`, which waits for the pipes)."""
        deadline = time.monotonic() + seconds
        while proc.returncode is None and time.monotonic() < deadline:
            await asyncio.sleep(P.JOB_POLL_INTERVAL_SECONDS)
        return proc.returncode

    async def _drain(self, run: _Run) -> None:
        """Bounded output draining (PUMP_DRAIN_SECONDS), tracked and untracked: the pumps get the bound after the kill or
        the root exit, then they are cancelled and awaited (never abandoned)."""
        if run.pumps:
            _, pending = await asyncio.wait(run.pumps, timeout=P.PUMP_DRAIN_SECONDS)
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                run.truncated = True
                run.reason_code = run.reason_code or "PUMP_DRAIN_TIMEOUT"
            await asyncio.gather(*run.pumps, return_exceptions=True)
        transport = getattr(run.proc, "_transport", None)
        if transport is not None:       # a pipe held open by an outsider would otherwise leave the transport open
            try:
                transport.close()
            except Exception:      # noqa: BLE001
                log.debug("closing the subprocess transport failed", exc_info=True)

    # --- results and Job handles ---------------------------------------------------------------------------------------
    def _outcome(self, run: _Run) -> CliOutcome:
        if run.abort is not None:
            return run.abort.outcome
        mapping = {"CANCELLED": CliOutcome.CANCELLED, "TIMEOUT": CliOutcome.TIMEOUT, "IDLE": CliOutcome.IDLE,
                   "ERROR": CliOutcome.ERROR}
        if run.decision is not None:
            return mapping[run.decision]
        return CliOutcome.SUCCESS if run.proc.returncode == 0 else CliOutcome.ERROR

    _UNSET = object()

    def _result(self, run: _Run, lifecycle: ProcessLifecycle, tree: TreeProof, termination: str | None, *,
                survivors: bool = False, spawn_failed: bool = False, reason: str | None = None,
                exit_code=_UNSET) -> CommandResult:
        stderr = run.err.decode("utf-8", errors="replace")
        if spawn_failed and run.abort is not None:
            stderr = f"spawn failed: {run.abort.reason}" + (f": {run.abort.cause}" if run.abort.cause else "")
        return CommandResult(
            argv=run.argv, outcome=self._outcome(run),
            exit_code=run.proc.returncode if exit_code is self._UNSET else exit_code,
            stdout=run.out.decode("utf-8", errors="replace"), stderr=stderr,
            duration_seconds=time.monotonic() - run.started, identity=run.identity, termination=termination,
            truncated=run.truncated, lifecycle=lifecycle, tree=tree, spawn_failed=spawn_failed, survivors=survivors,
            reason=reason or run.reason_code)

    def _spawn_failed(self, run: _Run, reason: str, message: str) -> CommandResult:
        """No process exists: nothing ran."""
        self._close_job(run.job)
        result = CommandResult(run.argv, CliOutcome.ERROR, None, "", f"spawn failed: {message}",
                               time.monotonic() - run.started, lifecycle=ProcessLifecycle.SPAWN_FAILED,
                               tree=TreeProof.EMPTY, spawn_failed=True, reason=reason)
        if run.tracked is not None:
            run.tracked.result = result
        return result

    def _release_job(self, run: _Run, result: CommandResult) -> None:
        """5.1 step 11: closed after the Job is proven empty; RETAINED when it is not (a later check or the process exit
        still reaches the tree)."""
        job = run.job
        if job is None:
            return
        if result.lifecycle is ProcessLifecycle.TERMINATION_UNCONFIRMED:
            key = run.tracked.execution_id if run.tracked is not None else id(run)
            self._unconfirmed[key] = job
            return
        self._close_job(job)

    @staticmethod
    def _close_job(job: TreeJob | None) -> None:
        if job is None:
            return
        try:
            job.close()
        except Exception:      # noqa: BLE001 - "a handle that cannot be closed is logged"
            log.warning("a Job handle could not be closed", exc_info=True)
