"""What /health may say about the Runner, computed in ONE pure place.

`status` is API-process liveness (always "ok" while the process answers). `ready` means a new execution request would
actually be picked up. Both `runner_active` and `ready` are derived from `Runner.lifecycle`; nothing assigns them by hand.

The reasons a Runner never started (preflight blocked, the advisory lock is held elsewhere, start() failed) are NOT
visible in the lifecycle (it is simply STOPPED), so the lifespan records them as `StartupOutcome` and this module maps
them. START_FAILED is reserved: a general `Runner.start` exception still aborts the API lifespan (decision D6-a), so a
running API can never report it; it is only produced by tests of this function until D6-b is decided.
"""
import enum
from dataclasses import dataclass

from app.services.runner import RunnerLifecycle


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


_STARTUP_CAUSE = {
    StartupOutcome.PREFLIGHT_BLOCKED: RunnerBlocker.PREFLIGHT_FAILED,
    StartupOutcome.ALREADY_ACTIVE: RunnerBlocker.ADVISORY_LOCK_HELD,
    StartupOutcome.START_FAILED: RunnerBlocker.START_FAILED,
}


@dataclass(frozen=True)
class RuntimeSnapshot:
    runner_active: bool
    ready: bool
    lifecycle: str
    startup_outcome: str
    blockers: tuple[str, ...]

    def as_dict(self) -> dict:
        """The /health body. `status` and `runner_active` keep their original meaning and position."""
        return {"status": "ok", "runner_active": self.runner_active, "ready": self.ready,
                "runner": {"lifecycle": self.lifecycle, "startup_outcome": self.startup_outcome,
                           "blockers": list(self.blockers)}}


def runtime_snapshot(lifecycle: RunnerLifecycle, startup_outcome: StartupOutcome,
                     recovery_unsettled: bool) -> RuntimeSnapshot:
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
    active = lifecycle is RunnerLifecycle.RUNNING
    return RuntimeSnapshot(runner_active=active, ready=active and not blockers, lifecycle=lifecycle.value,
                           startup_outcome=startup_outcome.value, blockers=tuple(b.value for b in blockers))


def snapshot_of(runner, startup_outcome: StartupOutcome) -> RuntimeSnapshot:
    """Reads the live Runner (duck-typed: `lifecycle`, optional `recovery_unsettled`) and judges it."""
    return runtime_snapshot(runner.lifecycle, startup_outcome, bool(getattr(runner, "recovery_unsettled", False)))
