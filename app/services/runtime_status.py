"""What /health may say about the Runner, computed in ONE pure place.

`status` is API-process liveness (always "ok" while the process answers). `ready` means a new execution request would
actually be picked up. Both `runner_active` and `ready` are derived from `Runner.lifecycle`; nothing assigns them by hand.

The reasons a Runner never started (preflight blocked, the advisory lock is held elsewhere, start() failed) are NOT
visible in the lifecycle (it is simply STOPPED), so the lifespan records them as `StartupOutcome` and this module maps
them. START_FAILED is reserved: a general `Runner.start` exception still aborts the API lifespan (decision D6-a), so a
running API can never report it; it is only produced by tests of this function until D6-b is decided.

Background state (design 14.2): the Runner side owns the source state and this module imports no API schema. A derived
display value (table SUP-DS) is display only and exists only in the lifecycle RUNNING (D35-1): it never sets
`background_degraded`, never adds a blocker and never adds the `BACKGROUND_COMPONENT_DEGRADED` warning.
"""
import enum
from dataclasses import dataclass
from datetime import datetime

from app.services.runner import RunnerLifecycle
from app.services.sweeper_stall import SweeperConfigInvalidSnapshot, SweeperStallSnapshot


class StartupOutcome(enum.Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    STARTED = "STARTED"
    PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"
    START_FAILED = "START_FAILED"            # reserved, see the module docstring


class RunnerBlocker(enum.Enum):
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    ADVISORY_LOCK_HELD = "ADVISORY_LOCK_HELD"
    START_FAILED = "START_FAILED"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    STOP_INCOMPLETE = "STOP_INCOMPLETE"
    RECOVERY_UNSETTLED = "RECOVERY_UNSETTLED"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    SUPERVISOR_DEGRADED = "SUPERVISOR_DEGRADED"     # SUP-10: the background supervisor ended; no new work is claimed


_STARTUP_CAUSE = {
    StartupOutcome.PREFLIGHT_BLOCKED: RunnerBlocker.PREFLIGHT_FAILED,
    StartupOutcome.ALREADY_ACTIVE: RunnerBlocker.ADVISORY_LOCK_HELD,
    StartupOutcome.START_FAILED: RunnerBlocker.START_FAILED,
}

# fixed warning codes (design 14.2): the /health `warnings` list and the report runtime part carry exactly these
WARNING_SWEEPER_STALLED = "SWEEPER_STALLED"
WARNING_SWEEPER_CONFIG_INVALID = "SWEEPER_CONFIG_INVALID"
WARNING_BACKGROUND_COMPONENT_DEGRADED = "BACKGROUND_COMPONENT_DEGRADED"

DISPLAY_DEGRADED = "DEGRADED"
DERIVED_DISPLAY_STATES = frozenset({"BACKOFF_ORPHANED", "EXITED_UNSUPERVISED", "BACKOFF_WAITER_DONE_UNSUPERVISED"})
_SUPERVISOR = "supervisor"


@dataclass(frozen=True)
class BackgroundComponentStatus:
    """One item of the component detail list (14.2). `DEGRADED` is the real state; the three other display values are derived
    and display only (table SUP-DS): their `reason_code` and `since` are None and `restart_exhausted` is False."""
    component: str
    reason_code: str | None
    since: datetime | None
    restart_exhausted: bool
    display_state: str

    @property
    def is_derived(self) -> bool:
        return self.display_state in DERIVED_DISPLAY_STATES


@dataclass(frozen=True)
class RunnerRuntimeState:
    """The input bundle of `runtime_snapshot` (14.2). `lifecycle` is informational: the first argument stays the authority for
    `runner_active` and `ready`, a differing value here is ignored."""
    lifecycle: str | None = None
    runner_active: bool = False
    sweeper_stall: SweeperStallSnapshot | None = None
    sweeper_config_invalid: SweeperConfigInvalidSnapshot | None = None
    background_degraded: bool = False
    background_components: tuple[BackgroundComponentStatus, ...] = ()
    supervisor_state: str | None = None
    background_revision: int = 0


def sweeper_status_dict(stall: SweeperStallSnapshot | None, config: SweeperConfigInvalidSnapshot | None) -> dict:
    """The key set of `SweeperStatusOut` (no cursor, no position kind): the default object when nothing is recorded."""
    return {"stalled": bool(stall and stall.stalled),
            "consecutive_failures": stall.consecutive_failures if stall else 0,
            "last_failure_code": stall.last_failure_code if stall else None,
            "last_failure_at": stall.last_failure_at if stall else None,
            "next_retry_at": stall.next_retry_at if stall else None,
            "config_invalid_active": bool(config and config.config_invalid_active),
            "clearance_pending": bool(config and config.clearance_pending)}


def components_of(units, lifecycle: str) -> tuple[BackgroundComponentStatus, ...]:
    """The inclusion rule of 14.2: every DEGRADED unit, and (only in the lifecycle RUNNING, D35-1) every unit with a derived
    display value. Built from one registry snapshot; it writes nothing."""
    out = []
    for u in units:
        if u.display_state == DISPLAY_DEGRADED:
            out.append(BackgroundComponentStatus(u.unit_id, u.reason_code, u.since,
                                                 bool(u.restart_exhausted) and u.unit_id != _SUPERVISOR, DISPLAY_DEGRADED))
        elif u.display_state in DERIVED_DISPLAY_STATES and lifecycle == RunnerLifecycle.RUNNING.value:
            out.append(BackgroundComponentStatus(u.unit_id, None, None, False, u.display_state))
    return tuple(out)


@dataclass(frozen=True)
class RuntimeSnapshot:
    runner_active: bool
    ready: bool
    lifecycle: str
    startup_outcome: str
    blockers: tuple[str, ...]
    background_degraded: bool = False          # true only while a component or the supervisor is REALLY degraded
    supervisor_degraded: bool = False
    components: tuple = ()                     # the raw unit statuses of the supervisor (phase 4)
    sweeper_stall: SweeperStallSnapshot | None = None
    sweeper_config_invalid: SweeperConfigInvalidSnapshot | None = None
    warnings: tuple[str, ...] = ()
    background_components: tuple[BackgroundComponentStatus, ...] = ()
    background_revision: int = 0               # part of the diagnostics cache key; NOT in as_dict()

    @property
    def sweeper(self) -> dict:
        return sweeper_status_dict(self.sweeper_stall, self.sweeper_config_invalid)

    @property
    def derived_states(self) -> tuple[tuple[str, str], ...]:
        """The sorted derived-state set of the cache key (14.3), from the same registry snapshot as everything else here."""
        return tuple(sorted((c.component, c.display_state) for c in self.background_components if c.is_derived))

    def as_dict(self) -> dict:
        """The /health body. `status` and `runner_active` keep their original meaning and position."""
        return {"status": "ok", "runner_active": self.runner_active, "ready": self.ready, "warnings": list(self.warnings),
                "runner": {"lifecycle": self.lifecycle, "startup_outcome": self.startup_outcome,
                           "blockers": list(self.blockers), "background_degraded": self.background_degraded,
                           "sweeper": self.sweeper}}


def _warnings(stall, config, background_degraded: bool) -> tuple[str, ...]:
    codes = []
    if stall is not None and stall.stalled:
        codes.append(WARNING_SWEEPER_STALLED)
    if config is not None and config.config_invalid_active:
        codes.append(WARNING_SWEEPER_CONFIG_INVALID)
    if background_degraded:
        codes.append(WARNING_BACKGROUND_COMPONENT_DEGRADED)
    return tuple(sorted(codes))


def runtime_snapshot(lifecycle: RunnerLifecycle, startup_outcome: StartupOutcome,
                     recovery_unsettled: bool, state: RunnerRuntimeState | None = None, *,
                     background_degraded: bool = False, supervisor_degraded: bool = False,
                     components: tuple = ()) -> RuntimeSnapshot:
    """`state` is the background input bundle (None = no background state). The keyword arguments are the older input of the
    phase-4 tests (raw unit statuses); they are combined with `state` when both are given."""
    active = lifecycle is RunnerLifecycle.RUNNING
    if state is not None:
        supervisor_degraded = supervisor_degraded or state.supervisor_state == DISPLAY_DEGRADED
        background_degraded = background_degraded or state.background_degraded
        items = state.background_components
    else:
        items = components_of(components, lifecycle.value)
    blockers: list[RunnerBlocker] = []
    if lifecycle is RunnerLifecycle.STOPPED:
        blockers.append(_STARTUP_CAUSE.get(startup_outcome, RunnerBlocker.STOPPED))
    elif lifecycle is RunnerLifecycle.STARTING:
        blockers.append(RunnerBlocker.STARTING)
    elif lifecycle is RunnerLifecycle.STOPPING:
        blockers.append(RunnerBlocker.SHUTTING_DOWN)
    elif lifecycle is RunnerLifecycle.STOP_INCOMPLETE:
        blockers.append(RunnerBlocker.STOP_INCOMPLETE)
    if recovery_unsettled:
        blockers.append(RunnerBlocker.RECOVERY_UNSETTLED)
    if supervisor_degraded and active:
        blockers.append(RunnerBlocker.SUPERVISOR_DEGRADED)
    degraded = background_degraded or supervisor_degraded
    stall = state.sweeper_stall if state else None
    config = state.sweeper_config_invalid if state else None
    return RuntimeSnapshot(runner_active=active, ready=active and not blockers, lifecycle=lifecycle.value,
                           startup_outcome=startup_outcome.value, blockers=tuple(b.value for b in blockers),
                           background_degraded=degraded, supervisor_degraded=supervisor_degraded,
                           components=tuple(components), sweeper_stall=stall, sweeper_config_invalid=config,
                           warnings=_warnings(stall, config, degraded),
                           background_components=tuple(i for i in items if active or not i.is_derived),
                           background_revision=state.background_revision if state else 0)


def snapshot_of(runner, startup_outcome: StartupOutcome) -> RuntimeSnapshot:
    """Reads the live Runner (duck-typed; a Runner without a property is read with a default, as `recovery_unsettled` is)."""
    status = runner.background_status() if hasattr(runner, "background_status") else None
    lifecycle = runner.lifecycle
    units = status.units if status else ()
    supervisor = next((u for u in units if u.unit_id == _SUPERVISOR), None)
    state = RunnerRuntimeState(
        lifecycle=lifecycle.value, runner_active=lifecycle is RunnerLifecycle.RUNNING,
        sweeper_stall=getattr(runner, "sweeper_stall", None),
        sweeper_config_invalid=getattr(runner, "sweeper_config_invalid", None),
        background_degraded=bool(status and status.background_degraded),
        background_components=components_of(units, lifecycle.value),
        supervisor_state=supervisor.state if supervisor else None,
        background_revision=int(getattr(runner, "background_revision", 0) or 0))
    return runtime_snapshot(lifecycle, startup_outcome, bool(getattr(runner, "recovery_unsettled", False)), state,
                            components=units)
