"""The background supervisor (design 9.1, SUP-1 to SUP-32, tables SUP-C, SUP-T, SUP-O, SUP-G, SUP-LF, SUP-DS).

One instance is owned by `Runner`. It owns the component registry (the state of every unit, its task, its restart waiter, its
exit-cause slot and its `last_failure` record) and the per-generation exit-event queue; the Runner owns the lifecycle, the
startup registry and the start/stop flows. The supervisor never settles a failed start and never cancels `start()`: it only
reports the first start failure through the callback that `begin_generation` receives (SUP-13). It reads the lifecycle through
the `host` callable `() -> (lifecycle_value: str, generation: int, start_aborting: bool)`, so it imports nothing from the Runner.

Everything that decides is a synchronous method (`ensure_component`, `process_event`, `status`), so the transition tables are
tested without a running event loop where possible; the only coroutines are the supervisor loop, the restart waiter and
`settle_tasks`.
"""
import asyncio
import collections
import enum
import itertools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.validation import policies as P
from app.validation.validators import BackgroundPolicy

log = logging.getLogger(__name__)

SUPERVISOR_ID = "supervisor"
COMPONENT_IDS = P.BACKGROUND_COMPONENT_IDS
RESTART_WAITER_SUFFIX = ":restart-waiter"

# fixed codes (audit only; implementer-chosen names for the two codes the design does not name, listed in the report)
CODE_EXCEPTION = "COMPONENT_EXCEPTION"
CODE_EXTERNAL = "EXTERNAL_OR_UNKNOWN"             # SUP-2 (c): unexpected cancellation or a signal-less return
CODE_READY_TIMEOUT = "BACKGROUND_READY_TIMEOUT"
CODE_SUPERVISOR_EXITED = "SUPERVISOR_EXITED"
CODE_RESTART_CONFIG = "RESTART_CONFIG_INVALID"
CODE_BUDGET = "RESTART_BUDGET_EXHAUSTED"

DERIVED_BACKOFF_ORPHANED = "BACKOFF_ORPHANED"
DERIVED_EXITED_UNSUPERVISED = "EXITED_UNSUPERVISED"
DERIVED_WAITER_DONE = "BACKOFF_WAITER_DONE_UNSUPERVISED"


def waiter_id(unit_id: str) -> str:
    return f"{unit_id}{RESTART_WAITER_SUFFIX}"


class UnitState(enum.Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    BACKOFF = "BACKOFF"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"


class TaskCancelReason(enum.Enum):
    """Why a top-level task is cancelled; recorded BEFORE the cancel (never inferred). Not the recovery `CancelReason`."""
    STOP = "STOP"
    FAILED_START = "FAILED_START"
    EXTERNAL_OR_UNKNOWN = "EXTERNAL_OR_UNKNOWN"


_NORMAL = (TaskCancelReason.STOP, TaskCancelReason.FAILED_START)


class Origin(enum.Enum):
    START = "START"
    RECOVERY = "RECOVERY"
    RESTART = "RESTART"


class Verdict(enum.Enum):
    CREATED = "CREATED"
    REFUSED_LIFECYCLE = "REFUSED_LIFECYCLE"
    REFUSED_GENERATION = "REFUSED_GENERATION"
    REFUSED_ABORTING = "REFUSED_ABORTING"
    REFUSED_DEGRADED = "REFUSED_DEGRADED"
    REFUSED_SLOT = "REFUSED_SLOT"
    REFUSED_STATE = "REFUSED_STATE"
    REFUSED_WAITER = "REFUSED_WAITER"


@dataclass(frozen=True)
class HostView:
    lifecycle: str
    generation: int
    start_aborting: bool


@dataclass(frozen=True)
class ExitEvent:
    event_id: int
    generation: int
    unit_id: str
    task_identity: int
    is_waiter: bool
    exit_time: float
    ready_seen: bool
    stable_since: float | None
    normal_settlement: bool                 # a STOP or FAILED_START reason was recorded before the cancel
    task: object = field(compare=False, default=None, repr=False)


@dataclass(frozen=True)
class ExitCause:
    kind: str                               # EXCEPTION | CANCELLED | RETURNED
    exception: BaseException | None


@dataclass(frozen=True)
class LastFailure:
    """Table SUP-LF: audit only. `task_identity` is internal (dedup key) and never exposed."""
    code: str
    at: datetime
    unit: str
    generation: int
    task_identity: int | None = field(repr=False, default=None)


@dataclass(eq=False)
class Unit:
    id: str
    state: UnitState = UnitState.IDLE
    task: asyncio.Task | None = None
    identity: int | None = None
    waiter: asyncio.Task | None = None
    waiter_identity: int | None = None
    consecutive: int = 0
    window: collections.deque = field(default_factory=collections.deque)
    ready_seen: bool = False
    stable_since: float | None = None
    ready_event: asyncio.Event | None = None
    degraded_reason: str | None = None
    degraded_at: datetime | None = None
    restart_exhausted: bool = False
    last_failure: LastFailure | None = None
    generation: int | None = None
    factory: Callable | None = None
    handled: set = field(default_factory=set)         # task identities (components and waiters) whose exit was processed


@dataclass(frozen=True)
class UnitStatus:
    unit_id: str
    state: str
    display_state: str                      # the state, or a derived value (display only, SUP-17)
    reason_code: str | None                 # None for every derived item
    since: datetime | None
    restart_exhausted: bool
    consecutive_failure_count: int
    stable: bool
    last_failure_code: str | None
    last_failure_generation: int | None


@dataclass(frozen=True)
class SupervisorStatus:
    """One registry snapshot (SUP-17). `derived` is the set of derived display values, part of the diagnostics cache key."""
    lifecycle: str
    units: tuple[UnitStatus, ...]
    background_degraded: bool
    supervisor_degraded: bool
    derived: tuple[tuple[str, str], ...]


async def settle_tasks(targets: dict[str, asyncio.Task], timeout: float) -> list[str]:
    """Cancel every task that is not done, wait for all of them under ONE `asyncio.wait` timeout, retrieve every result or
    exception once, and return the ids that are still alive (a task that ignores cancellation is never awaited forever)."""
    pending = {t for t in targets.values() if not t.done()}
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    unsettled = []
    for name, t in targets.items():
        if not t.done():
            unsettled.append(name)
        elif not t.cancelled() and t.exception() is not None:
            log.error("task %s ended with an error while it was settled: %r", name, t.exception())
    return unsettled


class BackgroundSupervisor:
    def __init__(self, policy: BackgroundPolicy, host: Callable[[], HostView], *,
                 clock: Callable[[], float] | None = None, wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 sleep: Callable | None = None):
        self.policy = policy
        self._host = host
        self._clock = clock or time.monotonic          # the default event loop's `loop.time()` is `time.monotonic()`; no loop needed
        self._wall = wall_clock
        self._sleep = sleep or asyncio.sleep
        self.units: dict[str, Unit] = {uid: Unit(uid) for uid in COMPONENT_IDS}
        self.supervisor_task: asyncio.Task | None = None
        self.supervisor_identity: int | None = None
        self.supervisor_ready: asyncio.Event | None = None
        self.supervisor_degraded = False
        self.supervisor_degraded_at: datetime | None = None
        self.generation = 0
        self.queue: asyncio.Queue | None = None
        self._ids = itertools.count(1)
        self._event_ids = itertools.count(1)
        self._reasons: dict[int, TaskCancelReason] = {}      # task identity -> the reason recorded before the cancel
        self._stamps: dict[int, float] = {}                  # task identity -> exit time stamped by the task wrapper
        self._causes: dict[tuple, ExitCause] = {}            # the exit-cause slot (generation, unit, identity)
        self.exit_cause_reads: collections.Counter = collections.Counter()   # test spy: real reads per slot key
        self.settling: set[int] = set()                      # the settling record (SUP-22): identities, not a generation
        self.audit: list[tuple] = []                         # T34 audit records (unit, generation, event id)
        self.unknown_events: list[ExitEvent] = []            # R3: unknown unit, fail closed
        self._start_failed: Callable[[BaseException], None] = lambda exc: None
        self.processed: list[tuple[int, str]] = []           # (event id, action label) for tests and logs

    # ------------------------------------------------------------------------------------------------ generation (SUP-11)
    def begin_generation(self, generation: int, on_start_failure: Callable[[BaseException], None]) -> None:
        """Reset set of SUP-11 (the Runner calls it right after it bumped `_generation`, with an empty registry). Preserved:
        the `last_failure` records, until the success boundary clears them (SUP-LF rows 6 and 7)."""
        self.generation = generation
        self.queue = asyncio.Queue()
        self.supervisor_task = self.supervisor_identity = self.supervisor_ready = None
        self.supervisor_degraded, self.supervisor_degraded_at = False, None
        self._start_failed = on_start_failure
        for u in self.units.values():
            u.state, u.task, u.identity, u.waiter, u.waiter_identity = UnitState.IDLE, None, None, None, None
            u.consecutive = 0
            u.window = collections.deque(maxlen=self.policy.window_max)
            u.ready_seen, u.stable_since, u.ready_event = False, None, None
            u.degraded_reason, u.degraded_at, u.restart_exhausted = None, None, False
            u.handled.clear()
        self._reasons.clear()
        self._stamps.clear()
        kept = {(f.generation, f.unit, f.task_identity) for f in self.last_failures().values()}
        self._causes = {k: v for k, v in self._causes.items() if k in kept}

    def last_failures(self) -> dict[str, LastFailure]:
        return {uid: u.last_failure for uid, u in self.units.items() if u.last_failure is not None}

    def clear_last_failures(self) -> None:
        """SUP-LF row 7: the successful start boundary clears the records of every unit (the slot dies with its record)."""
        for u in self.units.values():
            u.last_failure = None
        self._causes.clear()

    # --------------------------------------------------------------------------------------------------- task creation
    def _wrap(self, identity: int, coro):
        async def runner():
            try:
                return await coro
            finally:
                self._stamps[identity] = self._clock()          # the real exit time (SUP-27), also on cancellation
        return runner()

    def _spawn(self, unit_id: str, identity: int, generation: int, is_waiter: bool, coro, name: str) -> asyncio.Task:
        queue = self.queue
        task = asyncio.create_task(self._wrap(identity, coro), name=name)
        task.add_done_callback(lambda t: coro.close())          # a task cancelled before its first step never awaits `coro`
        task.add_done_callback(lambda t: self._on_done(queue, unit_id, identity, generation, is_waiter, t))
        return task

    def create_supervisor(self, generation: int) -> asyncio.Task:
        """SUP-9: one synchronous creation, a second one for the same generation is refused by the occupied slot."""
        if self.supervisor_task is not None:
            raise RuntimeError("the supervisor slot is occupied")
        identity = next(self._ids)
        self.supervisor_identity = identity
        self.supervisor_ready = ready = asyncio.Event()
        queue = self.queue
        body = self._run(queue, ready)
        self.supervisor_task = task = asyncio.create_task(self._wrap(identity, body), name=SUPERVISOR_ID)
        task.add_done_callback(lambda t: body.close())
        task.add_done_callback(lambda t: self._on_supervisor_done(generation, identity, t))
        return task

    async def _run(self, queue: asyncio.Queue, ready: asyncio.Event) -> None:
        ready.set()                                             # the entry point: before it reads its queue (SUP-20)
        while True:
            self.process_event(await queue.get())

    def signal_ready(self, unit_id: str, identity: int, generation: int) -> bool:
        """SUP-T T2 and T22: the READY signal counts only for the registered task identity and generation, and never after
        the failed-start marker (SUP-G priority 0)."""
        view = self._host()
        u = self.units.get(unit_id)
        if u is None or u.identity != identity or generation != self.generation or view.generation != generation:
            return False
        if view.start_aborting or view.lifecycle not in ("STARTING", "RUNNING"):
            return False
        u.ready_seen, u.stable_since = True, self._clock()
        if u.ready_event is not None:
            u.ready_event.set()
        return True

    def ensure_component(self, unit_id: str, factory: Callable, origin: Origin, generation: int, *,
                         waiter_identity: int | None = None) -> Verdict:
        """Table SUP-O in ONE synchronous section (no await between the reads and the registration). `factory(ready)` returns
        the coroutine of the component; for a RESTART the task object is created first (the only step that can raise), then
        the waiter reference is consumed (R31-1)."""
        view, u = self._host(), self.units[unit_id]
        allowed = {Origin.START: ("STARTING",), Origin.RECOVERY: ("STARTING", "RUNNING"), Origin.RESTART: ("RUNNING",)}[origin]
        if view.lifecycle not in allowed:
            return Verdict.REFUSED_LIFECYCLE
        if generation != view.generation or generation != self.generation:
            return Verdict.REFUSED_GENERATION
        if view.start_aborting:
            return Verdict.REFUSED_ABORTING
        if u.state is UnitState.DEGRADED or self.supervisor_degraded:
            return Verdict.REFUSED_DEGRADED
        if u.task is not None:                                  # a done() task whose event is unprocessed still counts
            return Verdict.REFUSED_SLOT
        if origin is Origin.RESTART:
            if u.state is not UnitState.BACKOFF:
                return Verdict.REFUSED_STATE
            if u.waiter is None or u.waiter_identity != waiter_identity or u.waiter.cancelled():
                return Verdict.REFUSED_WAITER
        else:
            if u.state is not UnitState.IDLE:
                return Verdict.REFUSED_STATE
            if u.waiter is not None:
                return Verdict.REFUSED_WAITER
        identity = next(self._ids)
        event = asyncio.Event()
        task = self._spawn(unit_id, identity, generation, False,
                           factory(lambda: self.signal_ready(unit_id, identity, generation)), unit_id)
        u.factory = factory
        u.task, u.identity, u.generation, u.ready_event = task, identity, generation, event
        u.ready_seen, u.stable_since = False, None
        if origin is Origin.RESTART:
            u.waiter, u.waiter_identity = None, None
        u.state = UnitState.RUNNING
        return Verdict.CREATED

    # ----------------------------------------------------------------------------------------------- done-callbacks
    def _on_done(self, queue, unit_id, identity, generation, is_waiter, task) -> None:
        """Does nothing but queue a typed event (SUP-9); it reads no cause."""
        u = self.units.get(unit_id)
        current = u is not None and (u.waiter_identity if is_waiter else u.identity) == identity
        event = ExitEvent(
            next(self._event_ids), generation, unit_id, identity, is_waiter, self._stamps.pop(identity, self._clock()),
            bool(current and not is_waiter and u.ready_seen), u.stable_since if current and not is_waiter else None,
            self._reasons.get(identity) in _NORMAL, task)
        if queue is not None:
            queue.put_nowait(event)

    def _on_supervisor_done(self, generation: int, identity: int, task: asyncio.Task) -> None:
        """SUP-13 / SUP-10: the one done-callback that reads its cause (cancelled() first, exception() exactly once)."""
        exc = None if task.cancelled() else task.exception()
        if self._reasons.get(identity) in _NORMAL:
            return
        view = self._host()
        if view.generation != generation:
            return
        if view.lifecycle == "STARTING" and not view.start_aborting:
            if exc is None:
                exc = RuntimeError("the background supervisor ended without a stop signal")
                if task.cancelled():
                    exc.__cause__ = asyncio.CancelledError()
            self._start_failed(exc)                              # T7: only `start_failed`, never a degraded flag
        elif view.lifecycle == "RUNNING" and not view.start_aborting:
            self.supervisor_degraded, self.supervisor_degraded_at = True, self._wall()     # T8
            log.critical("the background supervisor ended unexpectedly: new work is blocked")

    # ---------------------------------------------------------------------------------------- exit cause (once per exit)
    def read_exit_cause(self, unit_id: str, identity: int, generation: int, task: asyncio.Task) -> ExitCause:
        key = (generation, unit_id, identity)
        cause = self._causes.get(key)
        if cause is None:
            self.exit_cause_reads[key] += 1
            if task.cancelled():
                cause = ExitCause("CANCELLED", None)
            else:
                exc = task.exception()
                cause = ExitCause("EXCEPTION", exc) if exc is not None else ExitCause("RETURNED", None)
            self._causes[key] = cause
        return cause

    def _verdict(self, cause: ExitCause, identity: int) -> tuple[bool, str | None]:
        """(abnormal, code). A recorded STOP or FAILED_START is a normal end whatever the task did (SUP-2 (a))."""
        if self._reasons.get(identity) in _NORMAL:
            return False, None
        return True, CODE_EXCEPTION if cause.kind == "EXCEPTION" else CODE_EXTERNAL

    def _start_error(self, unit_id: str, cause: ExitCause) -> BaseException:
        if cause.exception is not None:
            return cause.exception
        err = RuntimeError(f"background component {unit_id} ended without a stop signal")
        if cause.kind == "CANCELLED":
            err.__cause__ = asyncio.CancelledError()
        return err

    # ------------------------------------------------------------------------------------------------ last_failure (SUP-LF)
    def record_last_failure(self, unit_id: str, code: str, generation: int, identity: int | None) -> bool:
        """Rows 1, 2, 3, 8 and 9: one record per generation + unit + task identity; whoever comes second writes nothing; a late
        writer never replaces the record of a newer generation."""
        u = self.units[unit_id]
        cur = u.last_failure
        if cur is not None:
            if identity is not None and cur.task_identity == identity and cur.generation == generation:
                return False
            if cur.generation > generation:
                return False
        u.last_failure = LastFailure(code, self._wall(), unit_id, generation, identity)
        log.critical("background component %s failed: %s (generation %s)", unit_id, code, generation)
        return True

    def observe_ended_required(self, generation: int) -> list[str]:
        """Table SUP-LF rows 8 and 9, run by `start()` (the hand-off or a boundary check) before the supervisor processed the
        exit event: every required component task that is `done()` and ended ABNORMALLY gets its record, once. Returns the
        units written, in the tie order of SUP-21 (the supervisor has no record)."""
        written = []
        for uid in COMPONENT_IDS:
            u = self.units[uid]
            if u.task is None or not u.task.done() or u.identity is None or u.generation != generation:
                continue
            cause = self.read_exit_cause(uid, u.identity, generation, u.task)
            abnormal, code = self._verdict(cause, u.identity)
            if abnormal and self.record_last_failure(uid, code, generation, u.identity):
                written.append(uid)
        return written

    def ended_required(self) -> list[str]:
        """B4 and B5: the required tasks that are done(), supervisor first, then BACKGROUND_COMPONENT_IDS (SUP-21 tie order)."""
        out = []
        if self.supervisor_task is not None and self.supervisor_task.done():
            out.append(SUPERVISOR_ID)
        out += [uid for uid in COMPONENT_IDS if self.units[uid].task is not None and self.units[uid].task.done()]
        return out

    def first_exit_error(self, uid: str) -> BaseException:
        """The start error of a task that ended before the supervisor reported it (cause read once)."""
        if uid == SUPERVISOR_ID:
            task = self.supervisor_task
            exc = None if task.cancelled() else task.exception()
            return exc or RuntimeError("the background supervisor ended without a stop signal")
        u = self.units[uid]
        return self._start_error(uid, self.read_exit_cause(uid, u.identity, u.generation, u.task))

    # -------------------------------------------------------------------------------------------------- event priority
    def event_priority(self, ev: ExitEvent) -> int:
        """Table SUP-G: 0 failed-start cleanup, 1 settling identity, 2 current active generation, 3 unrelated stale."""
        view = self._host()
        if view.lifecycle == "STARTING" and view.start_aborting and ev.generation == view.generation:
            return 0
        if ev.task_identity in self.settling:
            return 1
        if ev.generation == view.generation and not view.start_aborting and view.lifecycle in ("STARTING", "RUNNING"):
            return 2
        return 3

    # -------------------------------------------------------------------------------------------------- event processing
    def process_event(self, ev: ExitEvent) -> str:
        """Each event once (SUP-9); returns the label of the table row that applied. Never raises on a normal path."""
        label = self._process(ev)
        self.processed.append((ev.event_id, label))
        return label

    def _process(self, ev: ExitEvent) -> str:
        prio = self.event_priority(ev)
        u = self.units.get(ev.unit_id)
        if prio == 3:
            self.audit.append((ev.unit_id, ev.generation, ev.event_id))
            log.info("exit event %s of unit %s (generation %s) is stale: audit record only", ev.event_id, ev.unit_id,
                     ev.generation)
            return "T34"
        if prio in (0, 1):
            self._retrieve(ev)                                   # no restart, no budget, no start_failed, no degraded flag
            return "T9/T17/T27/T28" if prio == 1 else "T9/T17/T27"
        if u is None:                                            # R3: fail closed, restarts nothing
            self.unknown_events.append(ev)
            log.critical("exit event of an unknown unit %r", ev.unit_id)
            return "R3"
        if (ev.task_identity in u.handled):
            return "HANDLED"
        u.handled.add(ev.task_identity)                          # before any budget is touched (no double processing)
        cause = self.read_exit_cause(ev.unit_id, ev.task_identity, ev.generation, ev.task)
        abnormal, code = self._verdict(cause, ev.task_identity)
        if ev.is_waiter:
            if u.waiter_identity == ev.task_identity:
                u.waiter, u.waiter_identity = None, None         # the reference is removed atomically with its event
            if not abnormal or cause.kind != "EXCEPTION":
                return "T28" if not abnormal else "WAITER_END"
            ev_code, label = CODE_EXCEPTION, "T32"
            return self._component_abnormal(u, ev, ev_code, cause, label)
        if u.identity == ev.task_identity:
            u.task = None                                        # the slot is freed by the processed event
        if not abnormal:
            if u.state is not UnitState.DEGRADED:
                u.state = UnitState.STOPPED
            return "T17"
        return self._component_abnormal(u, ev, code, cause, "T3-5")

    def _retrieve(self, ev: ExitEvent) -> None:
        if ev.task is not None and ev.task.done():
            self.read_exit_cause(ev.unit_id, ev.task_identity, ev.generation, ev.task)

    def _delay(self, k: int) -> float:
        p = self.policy
        return float(min(p.backoff_cap_seconds, p.backoff_base_seconds * 2 ** min(k - 1, p.backoff_exponent_cap)))

    def _component_abnormal(self, u: Unit, ev: ExitEvent, code: str, cause: ExitCause, label: str) -> str:
        view = self._host()
        if view.lifecycle == "STARTING":                         # T6 / T36: only `start()` settles
            self.record_last_failure(u.id, code, ev.generation, ev.task_identity)
            self._start_failed(self._start_error(u.id, cause))
            return "T6" if label != "T32" else "T36"
        if view.lifecycle != "RUNNING":
            return "T9/T10"
        # T3: stabilization is judged from the REAL exit time of this task and resets only the consecutive count
        p = self.policy
        if ev.ready_seen and ev.stable_since is not None and ev.exit_time - ev.stable_since >= p.stable_seconds:
            u.consecutive = 0
        u.consecutive += 1
        self.record_last_failure(u.id, code, ev.generation, ev.task_identity)
        try:
            delay = self._delay(u.consecutive)
        except (ArithmeticError, TypeError, ValueError):         # T16: only through a corrupted copy
            return self._degrade(u, CODE_RESTART_CONFIG, exhausted=False, label="T16")
        while u.window and ev.exit_time - u.window[0] > p.window_seconds:
            u.window.popleft()                                   # expiry by time only
        if u.consecutive > p.max_restarts or len(u.window) >= p.window_max:
            return self._degrade(u, CODE_BUDGET, exhausted=True, label="T5")
        u.window.append(ev.exit_time)
        self._schedule_restart(u, delay, ev.generation)
        return "T4" if label != "T32" else "T32"

    def _degrade(self, u: Unit, reason: str, *, exhausted: bool, label: str) -> str:
        u.state, u.degraded_reason, u.degraded_at = UnitState.DEGRADED, reason, self._wall()
        u.restart_exhausted = exhausted
        log.critical("background component %s is DEGRADED (%s)", u.id, reason)
        return label

    def _schedule_restart(self, u: Unit, delay: float, generation: int) -> None:
        identity = next(self._ids)
        u.state = UnitState.BACKOFF
        u.waiter_identity = identity
        u.waiter = self._spawn(u.id, identity, generation, True, self._waiter(u, identity, generation, delay),
                               waiter_id(u.id))

    async def _waiter(self, u: Unit, identity: int, generation: int, delay: float) -> Verdict:
        await self._sleep(delay)
        verdict = self.ensure_component(u.id, u.factory, Origin.RESTART, generation, waiter_identity=identity)
        if verdict is not Verdict.CREATED and u.waiter_identity == identity:
            u.waiter, u.waiter_identity = None, None             # the refused waiter deregisters (SUP-3, T14); no budget
        return verdict

    # ------------------------------------------------------------------------------------------------ settlement support
    def settle_targets(self) -> dict[str, asyncio.Task]:
        """The component set, supervisor first so that it cannot create a restart (SUP-23)."""
        out: dict[str, asyncio.Task] = {}
        if self.supervisor_task is not None:
            out[SUPERVISOR_ID] = self.supervisor_task
        for uid, u in self.units.items():
            if u.task is not None:
                out[uid] = u.task
            if u.waiter is not None:
                out[waiter_id(uid)] = u.waiter
        return out

    def _identity_of(self, name: str) -> int | None:
        if name == SUPERVISOR_ID:
            return self.supervisor_identity
        if name.endswith(RESTART_WAITER_SUFFIX):
            return self.units[name[:-len(RESTART_WAITER_SUFFIX)]].waiter_identity
        return self.units[name].identity

    def record_settling(self, reason: TaskCancelReason) -> dict[str, asyncio.Task]:
        """ONE synchronous step: the cancel reason of every identity and the settling record, BEFORE any cancel (SUP-22)."""
        targets = self.settle_targets()
        for name in targets:
            identity = self._identity_of(name)
            if identity is not None:
                self._reasons[identity] = reason
                self.settling.add(identity)
        return targets

    async def settle(self, reason: TaskCancelReason, timeout: float) -> list[str]:
        """Cancel and await the component set under one common deadline; return the unsettled canonical ids."""
        targets = self.record_settling(reason)
        unsettled = await settle_tasks(targets, timeout)
        self.release_confirmed()
        return unsettled

    def release_confirmed(self) -> None:
        """A reference is removed, and its identity leaves the settling record, only once its task is confirmed ended."""
        for uid, u in self.units.items():
            if u.task is not None and u.task.done():
                self.settling.discard(u.identity)
                self._reasons.pop(u.identity, None)
                u.task = None
                u.state = UnitState.STOPPED if u.state is not UnitState.DEGRADED else u.state
            if u.waiter is not None and u.waiter.done():
                self.settling.discard(u.waiter_identity)
                self._reasons.pop(u.waiter_identity, None)
                u.waiter, u.waiter_identity = None, None
            if u.task is None and u.waiter is None and u.state in (UnitState.BACKOFF, UnitState.RUNNING):
                u.state = UnitState.STOPPED                    # T12 / T31: nothing of it exists that could stay unsettled
        if self.supervisor_task is not None and self.supervisor_task.done():
            self.settling.discard(self.supervisor_identity)
            self._reasons.pop(self.supervisor_identity, None)
            self.supervisor_task = None

    def mark_stopped(self) -> None:
        for u in self.units.values():
            if u.state is not UnitState.DEGRADED:
                u.state = UnitState.STOPPED

    def clear_flags(self) -> None:
        """SUP-25: a failed-start settlement that reaches STOPPED leaves no stale degraded flag."""
        self.supervisor_degraded, self.supervisor_degraded_at = False, None

    @property
    def registry_empty(self) -> bool:
        return self.supervisor_task is None and all(u.task is None and u.waiter is None for u in self.units.values())

    def unsettled_ids(self) -> list[str]:
        return list(self.settle_targets())

    # ---------------------------------------------------------------------------------------------- status read (SUP-17)
    def status(self) -> SupervisorStatus:
        """One registry snapshot; derives the display values of table SUP-DS. Display only: it writes nothing, counts no
        failure, consumes no budget, creates no waiter and starts no recovery. Derived values exist only in `RUNNING`."""
        view = self._host()
        running = view.lifecycle == "RUNNING"
        sup_dead = self.supervisor_degraded
        units, derived = [], []
        p = self.policy
        for uid, u in self.units.items():
            display, reason, since = u.state.value, None, None
            if u.state is UnitState.DEGRADED:
                reason, since = u.degraded_reason, u.degraded_at
            elif running:
                if u.task is not None and u.task.done() and u.identity not in u.handled:
                    display = DERIVED_EXITED_UNSUPERVISED                                   # row 3
                elif u.state is UnitState.BACKOFF and u.task is None:
                    if u.waiter is None:
                        display = DERIVED_BACKOFF_ORPHANED                                  # row 2
                    elif u.waiter.done() and u.waiter_identity not in u.handled and sup_dead:
                        display = DERIVED_WAITER_DONE                                       # row 4
            if display in (DERIVED_BACKOFF_ORPHANED, DERIVED_EXITED_UNSUPERVISED, DERIVED_WAITER_DONE):
                derived.append((uid, display))
            lf = u.last_failure
            stable = bool(u.ready_seen and u.stable_since is not None
                          and self._clock() - u.stable_since >= p.stable_seconds)
            units.append(UnitStatus(uid, u.state.value, display, reason, since, u.restart_exhausted, u.consecutive,
                                    stable, lf.code if lf else None, lf.generation if lf else None))
        sup_state = "DEGRADED" if sup_dead else ("RUNNING" if self.supervisor_task is not None else "STOPPED")
        units.append(UnitStatus(SUPERVISOR_ID, sup_state, sup_state, CODE_SUPERVISOR_EXITED if sup_dead else None,
                                self.supervisor_degraded_at, False, 0, False, None, None))
        degraded = sup_dead or any(u.state is UnitState.DEGRADED for u in self.units.values())
        return SupervisorStatus(view.lifecycle, tuple(units), degraded, sup_dead, tuple(derived))
