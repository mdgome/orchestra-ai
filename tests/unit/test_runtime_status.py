"""The /health judgement: one pure function over lifecycle, startup outcome and recovery state."""
import pytest

from app.services.runner import RunnerLifecycle as L
from app.services.runtime_status import StartupOutcome as S
from app.services.runtime_status import runtime_snapshot, snapshot_of


def test_a_running_runner_with_a_clean_recovery_is_ready():
    snap = runtime_snapshot(L.RUNNING, S.STARTED, recovery_unsettled=False)
    assert snap.runner_active and snap.ready and snap.blockers == ()
    # exact key set kept: the schema is an EXTENSION (design 14.2): top-level `warnings`, `runner.background_degraded` and
    # `runner.sweeper` (the default SweeperStatusOut object) are added, nothing is removed or renamed
    assert snap.as_dict() == {"status": "ok", "runner_active": True, "ready": True, "warnings": [],
                              "runner": {"lifecycle": "RUNNING", "startup_outcome": "STARTED", "blockers": [],
                                         "background_degraded": False,
                                         "sweeper": {"stalled": False, "consecutive_failures": 0, "last_failure_code": None, "last_failure_at": None,
                                          "next_retry_at": None, "config_invalid_active": False, "clearance_pending": False}}}


@pytest.mark.parametrize("outcome,blocker", [
    (S.PREFLIGHT_BLOCKED, "PREFLIGHT_FAILED"), (S.ALREADY_ACTIVE, "ADVISORY_LOCK_HELD"),
    (S.START_FAILED, "START_FAILED"), (S.NOT_ATTEMPTED, "STOPPED"), (S.STARTED, "STOPPED")])
def test_a_stopped_runner_is_explained_by_the_recorded_startup_outcome(outcome, blocker):
    snap = runtime_snapshot(L.STOPPED, outcome, recovery_unsettled=False)
    assert not snap.runner_active and not snap.ready and snap.blockers == (blocker,)


def test_start_failed_is_only_reachable_through_the_pure_function():
    """D6-a keeps a general Runner.start exception fatal for the lifespan, so a live API never reports it; the mapping
    exists (and is tested here) for when D6-b is decided."""
    assert runtime_snapshot(L.STOPPED, S.START_FAILED, False).blockers == ("START_FAILED",)


@pytest.mark.parametrize("lifecycle,blocker", [
    (L.STARTING, "STARTING"), (L.STOPPING, "SHUTTING_DOWN"), (L.STOP_INCOMPLETE, "STOP_INCOMPLETE")])
def test_the_lifecycle_alone_explains_transient_and_stuck_states(lifecycle, blocker):
    # the startup outcome is irrelevant here: a startup cause is never inferred from, nor mixed into, these states
    snap = runtime_snapshot(lifecycle, S.PREFLIGHT_BLOCKED, False)
    assert snap.blockers == (blocker,) and not snap.ready and not snap.runner_active


def test_blockers_can_be_several_and_an_unsettled_recovery_blocks_even_a_running_runner():
    running = runtime_snapshot(L.RUNNING, S.STARTED, recovery_unsettled=True)
    assert running.runner_active and not running.ready and running.blockers == ("RECOVERY_UNSETTLED",)
    stuck = runtime_snapshot(L.STOP_INCOMPLETE, S.STARTED, recovery_unsettled=True)
    assert stuck.blockers == ("STOP_INCOMPLETE", "RECOVERY_UNSETTLED")


def test_runner_active_follows_the_lifecycle_after_a_stop():
    class Runner:
        lifecycle = L.RUNNING
        recovery_unsettled = False

    runner = Runner()
    assert snapshot_of(runner, S.STARTED).runner_active is True
    runner.lifecycle = L.STOPPED
    after = snapshot_of(runner, S.STARTED)
    assert after.runner_active is False and after.ready is False and after.blockers == ("STOPPED",)


def test_a_runner_without_a_recovery_view_counts_as_settled():
    class Runner:
        lifecycle = L.RUNNING

    assert snapshot_of(Runner(), S.STARTED).ready is True
