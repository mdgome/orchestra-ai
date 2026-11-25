"""B2 phase 6 (design 14.2, 14.3, SUP-17, table SUP-DS, D35-1): the runtime snapshot built from the REAL background supervisor.

Case A (A1: a component task is done and the live supervisor has not processed its exit; A2: a waiter-less BACKOFF unit with a
live supervisor), case A2 and case B (the supervisor DEGRADED and a derived item) are built from the real registry and read
through `snapshot_of`; the derived values exist only in the lifecycle RUNNING; a lookup changes nothing; the Runner's
background revision grows only when a listed value really changes."""
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.schemas import api as api_schemas
from app.schemas import reports as report_schemas
from app.services import sweeper_stall as ss
from app.services.background_supervisor import (
    CODE_BUDGET, CODE_SUPERVISOR_EXITED, DERIVED_BACKOFF_ORPHANED, DERIVED_EXITED_UNSUPERVISED, DERIVED_WAITER_DONE, Origin,
    UnitState,
)
from app.services.runner import RunnerLifecycle as L
from app.services.runtime_status import (
    BackgroundComponentStatus, RunnerBlocker, RunnerRuntimeState, StartupOutcome, runtime_snapshot, snapshot_of,
)
from tests.unit.test_background_supervisor import cleanup, drain, dying, make, settle_loop
from tests.unit.test_runner_sweeper import make_runner

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


class FakeRunner:
    """Duck-typed Runner over a REAL BackgroundSupervisor (what `snapshot_of` reads)."""

    def __init__(self, sup, host, stall=None, config=None, revision=0):
        self.sup, self.host = sup, host
        self.sweeper_stall, self.sweeper_config_invalid, self.background_revision = stall, config, revision
        self.recovery_unsettled = False

    @property
    def lifecycle(self):
        return L(self.host.lifecycle)

    def background_status(self):
        return self.sup.status()


def snap(runner):
    return snapshot_of(runner, StartupOutcome.STARTED)


def retrieve(sup):
    """Marks the exceptions of done tasks as retrieved (no state of the supervisor changes: the status read uses done())."""
    for unit in sup.units.values():
        for task in (unit.task, unit.waiter):
            if task is not None and task.done() and not task.cancelled():
                task.exception()


async def case_a1():
    sup, host, *_ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await settle_loop()                                           # the task is done, its exit event is NOT processed
    retrieve(sup)
    return sup, host


def sweeper_item(snapshot):
    return next(c for c in snapshot.background_components if c.component == "sweeper")


# --- case A: derived only, the supervisor alive --------------------------------------------------------------------------------
async def test_case_a1_a_done_task_with_an_unprocessed_exit_is_a_derived_item_and_nothing_else():
    sup, host = await case_a1()
    s = snap(FakeRunner(sup, host))
    assert s.background_components == (BackgroundComponentStatus("sweeper", None, None, False, DERIVED_EXITED_UNSUPERVISED),)
    assert s.background_degraded is False and s.warnings == () and s.blockers == () and s.ready and s.runner_active
    assert s.derived_states == (("sweeper", DERIVED_EXITED_UNSUPERVISED),)
    assert s.as_dict()["warnings"] == [] and s.as_dict()["runner"]["background_degraded"] is False
    await cleanup(sup)


async def test_case_a2_a_waiterless_backoff_unit_with_a_live_supervisor_has_the_same_shape():
    sup, host, *_ = make()
    sup.units["sweeper"].state = UnitState.BACKOFF                # only fault injection builds it (final decision Q1-1)
    s = snap(FakeRunner(sup, host))
    assert s.background_components == (BackgroundComponentStatus("sweeper", None, None, False, DERIVED_BACKOFF_ORPHANED),)
    assert s.background_degraded is False and s.warnings == () and s.blockers == () and s.ready


# --- case B: the DEGRADED supervisor and a derived item -----------------------------------------------------------------------
async def test_case_b_the_degraded_supervisor_and_a_waiter_done_item():
    sup, host, *_ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await drain(sup)                                              # BACKOFF with a registered waiter
    sup.units["sweeper"].waiter.cancel()
    await settle_loop()                                           # the waiter is done, its event is not processed
    retrieve(sup)
    sup.supervisor_degraded, sup.supervisor_degraded_at = True, NOW
    s = snap(FakeRunner(sup, host))
    by = {c.component: c for c in s.background_components}
    assert by["supervisor"] == BackgroundComponentStatus("supervisor", CODE_SUPERVISOR_EXITED, NOW, False, "DEGRADED")
    assert by["sweeper"] == BackgroundComponentStatus("sweeper", None, None, False, DERIVED_WAITER_DONE)
    assert s.background_degraded is True and s.warnings == ("BACKGROUND_COMPONENT_DEGRADED",)
    assert s.blockers == (RunnerBlocker.SUPERVISOR_DEGRADED.value,) and s.ready is False and s.runner_active is True
    assert s.as_dict()["warnings"] == ["BACKGROUND_COMPONENT_DEGRADED"]
    assert s.derived_states == (("sweeper", DERIVED_WAITER_DONE),)


async def test_a_waiterless_backoff_with_the_degraded_supervisor_is_backoff_orphaned_and_case_b():
    sup, host, *_ = make()
    sup.units["sweeper"].state = UnitState.BACKOFF
    sup.supervisor_degraded, sup.supervisor_degraded_at = True, NOW
    s = snap(FakeRunner(sup, host))
    assert {(c.component, c.display_state) for c in s.background_components} == {
        ("supervisor", "DEGRADED"), ("sweeper", DERIVED_BACKOFF_ORPHANED)}
    assert s.background_degraded and s.blockers == ("SUPERVISOR_DEGRADED",)


# --- a really degraded component ----------------------------------------------------------------------------------------------
async def test_a_degraded_component_alone_changes_neither_ready_nor_blockers_and_sets_the_flag_and_the_warning():
    sup, host, *_ = make()
    sup._degrade(sup.units["risk-worker"], CODE_BUDGET, exhausted=True, label="T5")
    s = snap(FakeRunner(sup, host))
    item = s.background_components[0]
    assert (item.component, item.reason_code, item.restart_exhausted, item.display_state) == (
        "risk-worker", CODE_BUDGET, True, "DEGRADED") and item.since is not None
    assert s.background_degraded and s.warnings == ("BACKGROUND_COMPONENT_DEGRADED",)
    assert s.ready and s.runner_active and s.blockers == ()


# --- D35-1: derived values exist only in RUNNING ----------------------------------------------------------------------------
@pytest.mark.parametrize("lifecycle", ["STARTING", "STOPPING", "STOP_INCOMPLETE", "STOPPED"])
async def test_the_same_done_tasks_and_waiters_give_no_derived_item_and_no_warning_outside_running(lifecycle):
    sup, host = await case_a1()
    sup.units["capability"].state = UnitState.BACKOFF
    host.lifecycle = lifecycle
    s = snap(FakeRunner(sup, host))
    assert s.background_components == () and s.derived_states == () and s.warnings == () and not s.background_degraded
    await cleanup(sup)


async def test_a_synthetic_snapshot_drops_a_derived_item_outside_running_and_keeps_a_degraded_one():
    degraded = BackgroundComponentStatus("risk-worker", "X", NOW, True, "DEGRADED")
    derived = BackgroundComponentStatus("sweeper", None, None, False, DERIVED_EXITED_UNSUPERVISED)
    state = RunnerRuntimeState(background_degraded=True, background_components=(degraded, derived))
    stopping = runtime_snapshot(L.STOPPING, StartupOutcome.STARTED, False, state)
    assert stopping.background_components == (degraded,)
    running = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False, state)
    assert running.background_components == (degraded, derived) and running.derived_states == (("sweeper", derived.display_state),)


async def test_a_lookup_changes_no_state_budget_queue_or_dispatch_and_repeats_identically():
    sup, host = await case_a1()
    sup.units["capability"].state = UnitState.BACKOFF
    runner = FakeRunner(sup, host)
    before = ([(u.state, u.consecutive, len(u.window), u.waiter_identity, u.identity) for u in sup.units.values()],
              sup.queue.qsize(), sup.supervisor_degraded)
    first = snap(runner)
    for _ in range(5):
        assert snap(runner) == first
    after = ([(u.state, u.consecutive, len(u.window), u.waiter_identity, u.identity) for u in sup.units.values()],
             sup.queue.qsize(), sup.supervisor_degraded)
    assert after == before and sup.units["capability"].waiter is None
    await cleanup(sup)


# --- the sweeper status and the warnings ------------------------------------------------------------------------------------
def stalled_state(threshold=1):
    st = ss.SweeperStallState(threshold=threshold)
    st.record_failed_pass(pass_start=None, position=uuid.UUID(int=7), kind=ss.CANDIDATE, code="SWEEP_QUERY_FAILED", now=NOW,
                          retry_delay_seconds=30)
    return st


def config_invalid_state(*, valid_again=False):
    st = ss.SweeperStallState()
    st.record_config_invalid(pass_start=None, boundary_cursor=None, now=NOW, fallback_seconds=86400.0,
                             reasons=("RELATION",))
    if valid_again:
        st.note_config_evaluation(True, NOW + timedelta(seconds=5))
    return st


def test_a_stall_adds_the_warning_and_the_status_but_changes_neither_ready_nor_blockers():
    st = stalled_state()
    s = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False,
                         RunnerRuntimeState(sweeper_stall=st.snapshot(), sweeper_config_invalid=st.config_snapshot()))
    plain = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False)
    assert s.warnings == ("SWEEPER_STALLED",) and s.ready == plain.ready and s.blockers == plain.blockers
    assert s.runner_active == plain.runner_active
    assert s.sweeper == {"stalled": True, "consecutive_failures": 1, "last_failure_code": "SWEEP_QUERY_FAILED",
                         "last_failure_at": NOW, "next_retry_at": NOW + timedelta(seconds=30),
                         "config_invalid_active": False, "clearance_pending": False}
    assert not any(key in s.sweeper for key in ("pass_start_cursor", "first_failure_cursor", "current_failure_cursor"))


def test_a_configuration_invalid_state_alone_is_an_immediate_warning_with_the_neutral_stall_and_no_stall_blocker():
    st = config_invalid_state()
    s = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False,
                         RunnerRuntimeState(sweeper_stall=st.snapshot(), sweeper_config_invalid=st.config_snapshot()))
    assert s.warnings == ("SWEEPER_CONFIG_INVALID",) and s.ready and s.blockers == ()
    assert s.sweeper["config_invalid_active"] is True and s.sweeper["clearance_pending"] is False
    assert s.sweeper["stalled"] is False


def test_the_configuration_valid_again_but_not_cleared_is_clearance_pending_and_still_a_warning():
    st = config_invalid_state(valid_again=True)
    config = st.config_snapshot()
    assert config.config_currently_valid is True and config.clearance_pending is True
    s = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False, RunnerRuntimeState(sweeper_config_invalid=config))
    assert s.warnings == ("SWEEPER_CONFIG_INVALID",) and s.sweeper["clearance_pending"] is True


def test_all_three_warnings_are_listed_in_ascii_order():
    st = stalled_state()
    st.record_config_invalid(pass_start=None, boundary_cursor=None, now=NOW, fallback_seconds=86400.0)
    s = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False,
                         RunnerRuntimeState(sweeper_stall=st.snapshot(), sweeper_config_invalid=st.config_snapshot(),
                                            background_degraded=True))
    assert s.warnings == ("BACKGROUND_COMPONENT_DEGRADED", "SWEEPER_CONFIG_INVALID", "SWEEPER_STALLED")


def test_the_supervisor_blocker_needs_the_running_lifecycle_and_the_first_argument_stays_the_authority():
    state = RunnerRuntimeState(lifecycle="STOPPED", runner_active=False, supervisor_state="DEGRADED")
    running = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False, state)
    assert running.blockers == ("SUPERVISOR_DEGRADED",) and running.runner_active and not running.ready
    stopped = runtime_snapshot(L.STOPPED, StartupOutcome.STARTED, False, state)
    assert stopped.blockers == ("STOPPED",) and not stopped.runner_active


def test_the_three_argument_form_and_the_positional_form_keep_working():
    assert runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, recovery_unsettled=False).ready
    assert runtime_snapshot(L.STOPPED, StartupOutcome.PREFLIGHT_BLOCKED, False).blockers == ("PREFLIGHT_FAILED",)


# --- the attribute names equal the DTO field names (14.2) ---------------------------------------------------------------------
def names(model):
    return set(model.model_fields)


def test_the_frozen_dataclass_attribute_names_equal_the_dto_field_names():
    assert {f.name for f in dataclasses.fields(ss.SweeperStallSnapshot)} == names(api_schemas.SweeperStallOut)
    assert {f.name for f in dataclasses.fields(BackgroundComponentStatus)} == names(api_schemas.BackgroundComponentOut)
    config = ss.SweeperStallState().config_snapshot()
    for field in names(api_schemas.SweeperConfigInvalidOut):
        assert hasattr(config, field), field                       # aliases of the config_invalid_* fields
    assert api_schemas.SweeperConfigInvalidOut.model_validate(config).active is False


def test_the_health_sweeper_object_has_exactly_the_key_set_of_sweeper_status_out():
    health = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False).as_dict()["runner"]["sweeper"]
    assert set(health) == names(report_schemas.SweeperStatusOut)
    assert report_schemas.SweeperStatusOut.model_validate(health, strict=True) == report_schemas.SweeperStatusOut()


# --- the Runner's background revision (14.3) ---------------------------------------------------------------------------------
def test_the_revision_bumps_once_per_real_change_and_never_on_a_read_or_an_identical_rewrite():
    r = make_runner()
    assert r.background_revision == 0
    assert [r.background_revision for _ in range(3)] == [0, 0, 0]               # a read never bumps it
    r._stall.record_failed_pass(pass_start=None, position=uuid.UUID(int=1), kind=ss.CANDIDATE, code="SWEEP_QUERY_FAILED",
                                now=NOW, retry_delay_seconds=30)
    assert r.background_revision == 1 and r.background_revision == 1            # the failure position and counters
    r._stall.record_failed_pass(pass_start=None, position=uuid.UUID(int=1), kind=ss.CANDIDATE, code="SWEEP_QUERY_FAILED",
                                now=NOW, retry_delay_seconds=30)
    assert r.background_revision == 2                                           # the count changed
    r._stall.record_config_invalid(pass_start=None, boundary_cursor=None, now=NOW, fallback_seconds=86400.0)
    assert r.background_revision == 3                                           # activation of the configuration state
    r._stall.note_config_evaluation(False, NOW + timedelta(seconds=1))
    r._stall.note_config_evaluation(False, NOW + timedelta(seconds=2))
    assert r.background_revision == 3                                           # `config_evaluated_at` never bumps it
    r._stall.note_config_evaluation(True, NOW + timedelta(seconds=3))
    assert r.background_revision == 4                                           # config_currently_valid / clearance_pending
    r._supervisor.supervisor_degraded = True
    assert r.background_revision == 5                                           # the supervisor DEGRADED state
    r._supervisor.supervisor_degraded = True
    assert r.background_revision == 5                                           # an identical rewrite
    r._stall.reset()
    assert r.background_revision == 6                                           # clearance of both records


async def test_a_derived_value_never_bumps_the_revision_only_a_real_state_change_does():
    r = make_runner()
    r._state = L.RUNNING
    assert r.background_revision == 0
    r._supervisor.units["sweeper"].state = UnitState.BACKOFF                    # orphaned: a derived display value appears
    assert r.background_status().derived == (("sweeper", DERIVED_BACKOFF_ORPHANED),)
    # the REAL state changed (IDLE -> BACKOFF) so the revision moves; the derived set itself is not in the fingerprint
    assert r.background_revision == 1
    for _ in range(3):
        r.background_status()
    assert r.background_revision == 1


def test_a_component_degraded_state_and_its_details_bump_the_revision():
    r = make_runner()
    assert r.background_revision == 0
    r._supervisor._degrade(r._supervisor.units["risk-worker"], CODE_BUDGET, exhausted=True, label="T5")
    assert r.background_revision == 1
    assert snapshot_of(r, StartupOutcome.STARTED).background_revision == 1


# --- RunnerStopIncomplete: the message composes the three parts separately (14.2, section 13 'Runtime status') --------------------
from app.services.runner import RunnerStopIncomplete  # noqa: E402


@pytest.mark.parametrize("kwargs,parts", [
    (dict(run_ids=("r1", "r2")), ["2 task(s) are still running after the shutdown grace period"]),
    (dict(recovery="the recovery is unsettled"), ["request recovery is unsettled (the recovery is unsettled)"]),
    (dict(run_ids=(), components=("supervisor", "sweeper:restart-waiter")),
     ["2 background unit(s) are unsettled (supervisor, sweeper:restart-waiter)"]),
    (dict(run_ids=("r1",), components=("capability",)), ["1 task(s) are still running after the shutdown grace period", "1 background unit(s) are unsettled (capability)"]),
    (dict(recovery="x", components=("capability",)), ["1 background unit(s) are unsettled (capability)", "request recovery is unsettled (x)"]),
], ids=["run-only", "recovery-only", "component-only", "run-and-component", "recovery-and-component"])
def test_a_stop_incomplete_message_names_only_the_parts_that_exist(kwargs, parts):
    error = RunnerStopIncomplete(**kwargs)
    assert str(error) == "; ".join(parts)
    assert error.run_ids == tuple(kwargs.get("run_ids", ())) and error.components == tuple(kwargs.get("components", ()))
    assert error.recovery == kwargs.get("recovery")


async def test_a_stop_on_a_stopped_runner_is_an_idempotent_success_and_never_builds_the_exception_for_an_empty_set():
    r = make_runner()
    await r.stop()
    await r.stop()
    assert r.lifecycle is L.STOPPED
