"""BackgroundSupervisor: table SUP-O (creation by origin), SUP-T T1 to T37, SUP-G priorities, SUP-LF rows, SUP-DS display rows,
restart budgets, waiters, the exit-cause slot and the settlement helper. Fake clock with DYADIC values and a controllable sleep:
no test waits for a real backoff."""
import asyncio
import dataclasses
import itertools

import pytest

from app.services.background_supervisor import (
    COMPONENT_IDS, CODE_BUDGET, CODE_EXCEPTION, CODE_EXTERNAL, DERIVED_BACKOFF_ORPHANED, DERIVED_EXITED_UNSUPERVISED,
    DERIVED_WAITER_DONE, SUPERVISOR_ID, BackgroundSupervisor, HostView, Origin, TaskCancelReason, UnitState, Verdict,
    settle_tasks, waiter_id)
from app.validation.validators import build_background_policy

POLICY = build_background_policy(5.0)


class Host:
    def __init__(self, lifecycle="RUNNING", generation=1, aborting=False):
        self.lifecycle, self.generation, self.aborting = lifecycle, generation, aborting

    def __call__(self):
        return HostView(self.lifecycle, self.generation, self.aborting)


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class Sleeper:
    """A controllable sleep: records every requested delay and waits for `release()`."""
    def __init__(self):
        self.delays, self.gates = [], []

    async def __call__(self, delay):
        gate = asyncio.Event()
        self.delays.append(delay)
        self.gates.append(gate)
        await gate.wait()

    def release(self, index=-1):
        self.gates[index].set()


def make(policy=POLICY, host=None, clock=None, sleeper=None):
    host, clock, sleeper = host or Host(), clock or Clock(), sleeper or Sleeper()
    failures = []
    sup = BackgroundSupervisor(policy, host, clock=clock, sleep=sleeper)
    sup.begin_generation(host.generation, failures.append)
    return sup, host, clock, sleeper, failures


def alive(ready_now=True):
    def factory(ready):
        async def body():
            if ready_now:
                ready()
            await asyncio.Event().wait()
        return body()
    return factory


def dying(exc=None, ready_now=True):
    def factory(ready):
        async def body():
            if ready_now:
                ready()
            await asyncio.sleep(0)
            if exc is not None:
                raise exc
        return body()
    return factory


async def settle_loop(n=5):
    for _ in range(n):
        await asyncio.sleep(0)


async def release(sleeper):
    """Let the waiter reach its sleep first, then release the controllable delay."""
    await settle_loop()
    sleeper.release()


async def drain(sup):
    """Process every queued exit event (what the supervisor loop does), returning the labels."""
    await settle_loop()
    labels = []
    while not sup.queue.empty():
        labels.append(sup.process_event(sup.queue.get_nowait()))
    return labels


def component_event(sup):
    """Pop queued events until the component's own exit event; waiter events are processed on the way (normal ends)."""
    while True:
        ev = sup.queue.get_nowait()
        if not ev.is_waiter:
            return ev
        sup.process_event(ev)


async def cleanup(sup):
    await sup.settle(TaskCancelReason.STOP, 1.0)


# --- table SUP-O -----------------------------------------------------------------------------------------------------------
LIFECYCLES = ["STOPPED", "STARTING", "RUNNING", "STOPPING", "STOP_INCOMPLETE"]


@pytest.mark.parametrize("origin", list(Origin))
@pytest.mark.parametrize("lifecycle", LIFECYCLES)
@pytest.mark.parametrize("state", [UnitState.IDLE, UnitState.BACKOFF, UnitState.DEGRADED])
@pytest.mark.parametrize("aborting", [False, True])
async def test_sup_o_exactly_one_verdict_per_cell_and_created_only_where_every_condition_holds(origin, lifecycle, state, aborting):
    host = Host(lifecycle, 1, aborting)
    sup, *_ = make(host=host)
    unit = sup.units["sweeper"]
    unit.state = state
    waiter = None
    if state is UnitState.BACKOFF:
        waiter = asyncio.get_running_loop().create_future()
        unit.waiter, unit.waiter_identity = asyncio.ensure_future(asyncio.sleep(3600)), 77
        waiter = unit.waiter
    verdict = sup.ensure_component("sweeper", alive(), origin, 1, waiter_identity=77)
    allowed_lifecycle = {Origin.START: {"STARTING"}, Origin.RECOVERY: {"STARTING", "RUNNING"}, Origin.RESTART: {"RUNNING"}}[origin]
    expected_state = UnitState.BACKOFF if origin is Origin.RESTART else UnitState.IDLE
    should_create = lifecycle in allowed_lifecycle and not aborting and state is expected_state
    assert (verdict is Verdict.CREATED) == should_create, (origin, lifecycle, state, aborting, verdict)
    if should_create:
        assert unit.state is UnitState.RUNNING and unit.waiter is None            # the waiter reference is consumed (R31-1)
    else:
        assert unit.task is None                                                   # a refusal creates nothing
    if waiter is not None:
        waiter.cancel()
    await cleanup(sup)


async def test_sup_o_a_wrong_waiter_identity_and_a_stale_generation_are_refused():
    sup, host, *_ = make()
    unit = sup.units["sweeper"]
    unit.state, unit.waiter, unit.waiter_identity = UnitState.BACKOFF, asyncio.ensure_future(asyncio.sleep(3600)), 5
    assert sup.ensure_component("sweeper", alive(), Origin.RESTART, 1, waiter_identity=6) is Verdict.REFUSED_WAITER
    assert sup.ensure_component("sweeper", alive(), Origin.RESTART, 2, waiter_identity=5) is Verdict.REFUSED_GENERATION
    unit.waiter.cancel()
    assert unit.state is UnitState.BACKOFF and unit.task is None


async def test_sup_o_a_done_task_whose_event_is_unprocessed_still_occupies_the_slot_and_the_processed_event_frees_it():
    host = Host("STARTING")
    sup, *_ = make(host=host)
    assert sup.ensure_component("sweeper", dying(), Origin.START, 1) is Verdict.CREATED
    await settle_loop()
    assert sup.units["sweeper"].task.done()
    assert sup.ensure_component("sweeper", alive(), Origin.RECOVERY, 1) is Verdict.REFUSED_SLOT
    await cleanup(sup)


async def test_sup_o_a_degraded_supervisor_refuses_every_origin():
    for origin, lifecycle in ((Origin.START, "STARTING"), (Origin.RECOVERY, "RUNNING"), (Origin.RESTART, "RUNNING")):
        sup, *_ = make(host=Host(lifecycle))
        sup.supervisor_degraded = True
        assert sup.ensure_component("sweeper", alive(), origin, 1) is Verdict.REFUSED_DEGRADED


# --- READY and per-task state (SUP-15, T1, T2, T22) -------------------------------------------------------------------------
async def test_ready_counts_only_for_the_registered_identity_and_generation_and_never_after_the_marker():
    host = Host("STARTING")
    sup, *_ = make(host=host)
    sup.ensure_component("sweeper", alive(ready_now=False), Origin.START, 1)
    unit = sup.units["sweeper"]
    assert not unit.ready_seen
    assert sup.signal_ready("sweeper", unit.identity + 99, 1) is False             # another identity
    assert sup.signal_ready("sweeper", unit.identity, 2) is False                  # another generation
    host.aborting = True
    assert sup.signal_ready("sweeper", unit.identity, 1) is False                  # SUP-G priority 0: ignored
    host.aborting = False
    assert sup.signal_ready("sweeper", unit.identity, 1) is True and unit.ready_seen and unit.stable_since == 0.0
    await cleanup(sup)


async def test_a_replaced_task_never_inherits_ready_or_stable_since():
    sup, host, clock, sleeper, _ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)    # READY at once, then dies
    clock.now = 8.0
    await drain(sup)
    await release(sleeper)
    await settle_loop()
    unit = sup.units["sweeper"]
    assert unit.state is UnitState.RUNNING and unit.stable_since == 8.0           # stamped at the NEW task's READY, not 0.0
    await cleanup(sup)


# --- T3 to T5: the budgets, the delays and stabilization ---------------------------------------------------------------------
async def crash_once(sup, clock, at, exc=RuntimeError("boom")):
    """Make the sweeper task die with `exc` at fake time `at`, then process its exit event."""
    unit = sup.units["sweeper"]
    gate = asyncio.Event()
    unit.task.cancel()
    clock.now = at
    labels = await drain(sup)
    return labels


async def test_t4_the_delays_are_5_10_20_40_60_and_the_sixth_consecutive_exit_degrades():
    sup, host, clock, sleeper, _ = make()
    labels, now = [], 0.0
    for k in range(1, 7):
        if k == 1:
            sup.ensure_component("sweeper", dying(RuntimeError("x"), ready_now=False), Origin.RECOVERY, 1)
        else:
            await release(sleeper)
            await settle_loop()
        clock.now = now = now + 1.0
        unit = sup.units["sweeper"]
        await settle_loop()
        labels += await drain(sup)
        if unit.state is UnitState.DEGRADED:
            break
    # the factory of the restart is the one registered at the first creation: a dying one, so the cycle repeats
    assert sleeper.delays == [5.0, 10.0, 20.0, 40.0, 60.0]
    assert sup.units["sweeper"].state is UnitState.DEGRADED and sup.units["sweeper"].restart_exhausted
    assert sup.units["sweeper"].degraded_reason == CODE_BUDGET and sup.units["sweeper"].consecutive == 6
    assert sup.status().background_degraded is True


async def test_t5_degraded_keeps_last_failure_registers_no_waiter_and_ignores_later_events():
    policy = dataclasses.replace(POLICY, max_restarts=1)
    sup, host, clock, sleeper, _ = make(policy=policy)
    sup.ensure_component("sweeper", dying(RuntimeError("x"), ready_now=False), Origin.RECOVERY, 1)
    await drain(sup)
    await release(sleeper)
    await settle_loop()
    await drain(sup)
    unit = sup.units["sweeper"]
    assert unit.state is UnitState.DEGRADED and unit.waiter is None and unit.last_failure.code == CODE_EXCEPTION
    for origin, lifecycle in ((Origin.START, "STARTING"), (Origin.RECOVERY, "RUNNING"), (Origin.RESTART, "RUNNING")):
        host.lifecycle = lifecycle                                                  # T21: DEGRADED refuses every origin
        assert sup.ensure_component("sweeper", alive(), origin, 1) is Verdict.REFUSED_DEGRADED


@pytest.mark.parametrize("offset,reset", [(299.75, False), (300.0, True), (300.25, True)])
async def test_t3_stability_boundary_is_judged_from_the_real_exit_time_with_dyadic_values(offset, reset):
    sup, host, clock, sleeper, _ = make()
    sup.ensure_component("sweeper", alive(), Origin.RECOVERY, 1)
    await settle_loop()
    unit = sup.units["sweeper"]
    unit.consecutive = 3                                                           # earlier failures of this generation
    start = unit.stable_since
    unit.task.cancel()
    await settle_loop()
    sup._reasons.clear()                                                           # an UNEXPECTED cancellation
    unit.task._exit = None
    # the wrapper stamped the exit time with the clock at the moment of the cancel: move the clock first
    clock.now = start + offset
    sup._stamps.clear()
    ev = [e for e in iter(lambda: None if sup.queue.empty() else sup.queue.get_nowait(), None)][0]
    ev = dataclasses.replace(ev, exit_time=start + offset)
    sup.process_event(ev)
    assert (unit.consecutive == 1) is reset                                        # reset before the exit is counted (T3)
    assert (unit.consecutive == 4) is (not reset)


async def test_an_exit_before_ready_is_never_stable_even_with_a_stale_stable_since():
    sup, host, clock, sleeper, _ = make()
    sup.ensure_component("sweeper", alive(ready_now=False), Origin.RECOVERY, 1)
    await settle_loop()
    unit = sup.units["sweeper"]
    unit.consecutive, unit.stable_since = 2, -1000.0                               # a predecessor's stale value
    unit.task.cancel()
    clock.now = 10_000.0
    await drain(sup)
    assert unit.consecutive == 3 and unit.state is UnitState.BACKOFF


async def test_the_rolling_window_degrades_even_when_every_run_was_stable_and_expires_by_time_only():
    policy = dataclasses.replace(POLICY, window_max=2, window_seconds=3600.0, stable_seconds=60.0, max_restarts=50)
    sup, host, clock, sleeper, _ = make(policy=policy)
    sup.ensure_component("sweeper", alive(), Origin.RECOVERY, 1)
    await settle_loop()
    unit = sup.units["sweeper"]

    async def stable_crash(at):
        unit.task.cancel()
        await settle_loop()
        sup._reasons.clear()
        ev = component_event(sup)
        sup.process_event(dataclasses.replace(ev, exit_time=at, ready_seen=True, stable_since=at - 100.0))

    await stable_crash(10.0)
    assert unit.consecutive == 1 and len(unit.window) == 1                         # the consecutive count was reset, the window not
    await release(sleeper)
    await settle_loop()
    await stable_crash(20.0)
    assert unit.state is UnitState.BACKOFF and len(unit.window) == 2
    await release(sleeper)
    await settle_loop()
    await stable_crash(30.0)
    assert unit.state is UnitState.DEGRADED                                        # W entries remain: the window budget degrades
    assert unit.restart_exhausted


async def test_window_entries_older_than_the_window_are_dropped_only_by_time():
    policy = dataclasses.replace(POLICY, window_max=2, window_seconds=100.0, max_restarts=50)
    sup, host, clock, sleeper, _ = make(policy=policy)
    sup.ensure_component("sweeper", alive(), Origin.RECOVERY, 1)
    await settle_loop()
    unit = sup.units["sweeper"]
    for at in (10.0, 20.0):
        unit.task.cancel()
        await settle_loop()
        sup._reasons.clear()
        sup.process_event(dataclasses.replace(component_event(sup), exit_time=at))
        await release(sleeper)
        await settle_loop()
    unit.task.cancel()
    await settle_loop()
    sup._reasons.clear()
    sup.process_event(dataclasses.replace(component_event(sup), exit_time=500.0))   # both entries expired by time
    assert unit.state is UnitState.BACKOFF and len(unit.window) == 1


async def test_t16_a_corrupted_restart_constant_copy_degrades_at_once_without_a_delay():
    policy = dataclasses.replace(POLICY, backoff_base_seconds="x")
    sup, *_ = make(policy=policy)
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    labels = await drain(sup)
    assert labels == ["T16"] and sup.units["sweeper"].state is UnitState.DEGRADED and sup.units["sweeper"].waiter is None


# --- SUP-2 and SUP-LF: causes and the last_failure record --------------------------------------------------------------------
async def test_a_signal_less_return_is_an_external_or_unknown_abnormal_exit_recorded_once():
    sup, *_ = make()
    sup.ensure_component("sweeper", dying(None), Origin.RECOVERY, 1)               # returns by itself, no exception
    labels = await drain(sup)
    unit = sup.units["sweeper"]
    assert labels == ["T4"] and unit.last_failure.code == CODE_EXTERNAL and unit.state is UnitState.BACKOFF
    assert sum(sup.exit_cause_reads.values()) == 1


def returning(value, ready_now=True):
    def factory(ready):
        async def body():
            if ready_now:
                ready()
            await asyncio.sleep(0)
            return value
        return body()
    return factory


class _Obj:
    pass


@pytest.mark.parametrize("value", [None, 0, False, "", "x", _Obj(), (1,)], ids=repr)
@pytest.mark.parametrize("start_first", [True, False], ids=["start-first", "supervisor-first"])
async def test_d35_row10_a_signal_less_return_is_one_external_or_unknown_record_whatever_the_return_value(value, start_first):
    """D35 row 10: no stop or settlement signal, no exception, ANY return value -> abnormal exit, EXTERNAL_OR_UNKNOWN once."""
    host = Host("STARTING")
    sup, *_ = make(host=host)
    sup.ensure_component("risk-worker", returning(value), Origin.START, 1)
    await settle_loop()
    unit = sup.units["risk-worker"]
    assert unit.task.done() and not unit.task.cancelled() and unit.task.exception() is None
    if start_first:
        assert sup.observe_ended_required(1) == ["risk-worker"]
        sup.process_event(sup.queue.get_nowait())
    else:
        sup.process_event(sup.queue.get_nowait())
        assert sup.observe_ended_required(1) == []
    assert unit.last_failure.code == CODE_EXTERNAL and unit.last_failure.generation == 1
    assert sum(sup.exit_cause_reads.values()) == 1
    assert [f for f in sup.last_failures()] == ["risk-worker"]


@pytest.mark.parametrize("value", [None, 0, False, _Obj()], ids=repr)
async def test_d35_row10_a_signal_less_return_while_running_is_one_record_and_one_restart_budget_step(value):
    sup, *_ = make()
    sup.ensure_component("sweeper", returning(value), Origin.RECOVERY, 1)
    labels = await drain(sup)
    unit = sup.units["sweeper"]
    assert labels == ["T4"] and unit.last_failure.code == CODE_EXTERNAL and unit.consecutive == 1
    assert unit.state is UnitState.BACKOFF and sum(sup.exit_cause_reads.values()) == 1
    assert sup.observe_ended_required(1) == []                                    # a late observer rewrites nothing


@pytest.mark.parametrize("value", [None, 0, False, _Obj()], ids=repr)
@pytest.mark.parametrize("reason", [TaskCancelReason.STOP, TaskCancelReason.FAILED_START])
async def test_d35_row10_an_expected_settlement_creates_no_record_even_when_the_task_returns_or_swallows_the_cancel(value, reason):
    """The task ends normally (expected cancellation, or it swallows the cancel and returns a value) under a recorded reason:
    no failure record, no budget, no restart, whichever order start() and the supervisor read it."""
    host = Host("STARTING" if reason is TaskCancelReason.FAILED_START else "RUNNING")
    sup, *_ = make(host=host)

    def swallowing(ready):
        async def body():
            ready()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return value
        return body()

    sup.ensure_component("capability", swallowing, Origin.START if host.lifecycle == "STARTING" else Origin.RECOVERY, 1)
    sup.ensure_component("sweeper", alive(), Origin.START if host.lifecycle == "STARTING" else Origin.RECOVERY, 1)
    await settle_loop()
    host.aborting = host.lifecycle == "STARTING"
    targets = sup.record_settling(reason)                                         # the reason is recorded BEFORE the cancel
    assert sup.observe_ended_required(1) == []
    assert await sup.settle(reason, 1.0) == [] and targets
    host.lifecycle = "STOPPING"
    while not sup.queue.empty():
        sup.process_event(sup.queue.get_nowait())
    assert sup.last_failures() == {} and all(u.consecutive == 0 and u.waiter is None and len(u.window) == 0 and u.last_failure is None
                                             for u in sup.units.values())


async def test_d35_row10_a_cancellation_that_arrives_during_the_settlement_creates_no_record():
    """A second cancel of the same task while the settlement waits for it is still the recorded STOP: normal, no record."""
    sup, host, *_ = make()
    gate = asyncio.Event()

    def slow_to_end(ready):
        async def body():
            ready()
            while True:
                try:
                    await gate.wait()
                    return "done"
                except asyncio.CancelledError:
                    await asyncio.sleep(0.01)                                  # still cleaning up when the next cancel comes
        return body()

    sup.ensure_component("sweeper", slow_to_end, Origin.RECOVERY, 1)
    await settle_loop()
    settle = asyncio.ensure_future(sup.settle(TaskCancelReason.STOP, 5.0))
    await asyncio.sleep(0.005)
    sup.units["sweeper"].task.cancel()                                           # an extra cancel during the settlement
    gate.set()
    assert await settle == []
    host.lifecycle = "STOPPING"
    while not sup.queue.empty():
        sup.process_event(sup.queue.get_nowait())
    assert sup.last_failures() == {} and sup.units["sweeper"].consecutive == 0


async def test_d35_row10_an_unexpected_top_level_cancellation_and_an_uncaught_exception_are_abnormal_per_sup_2_c():
    """Distinguished from the signal-less return: the exception keeps COMPONENT_EXCEPTION; the cancellation (no recorded
    reason) is an abnormal exit recorded once and consumes one budget step (the code of this case is not a row-10 claim)."""
    sup, *_ = make()
    sup.ensure_component("sweeper", alive(), Origin.RECOVERY, 1)
    sup.ensure_component("capability", dying(RuntimeError("boom")), Origin.RECOVERY, 1)
    await settle_loop()
    sup.units["sweeper"].task.cancel()                                           # nobody recorded a reason
    await drain(sup)
    assert sup.units["capability"].last_failure.code == CODE_EXCEPTION
    swept = sup.units["sweeper"]
    assert swept.last_failure is not None and swept.consecutive == 1 and swept.state is UnitState.BACKOFF
    assert swept.last_failure.code == CODE_EXTERNAL == "EXTERNAL_OR_UNKNOWN" and swept.last_failure.generation == 1
    assert len(swept.window) == 1                                                # exactly ONE restart-budget step
    reads = {key[1]: n for key, n in sup.exit_cause_reads.items()}
    assert reads == {"sweeper": 1, "capability": 1}                              # one cause read per exit
    assert sum(sup.exit_cause_reads.values()) == 2                               # once per exit
    assert sup.observe_ended_required(1) == [] and swept.consecutive == 1 and len(swept.window) == 1   # no second record
    await cleanup(sup)


async def test_an_uncaught_exception_uses_the_exception_code():
    sup, *_ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("boom")), Origin.RECOVERY, 1)
    await drain(sup)
    assert sup.units["sweeper"].last_failure.code == CODE_EXCEPTION


async def test_a_recorded_stop_or_failed_start_is_never_a_failure_whatever_the_task_did():
    for reason in (TaskCancelReason.STOP, TaskCancelReason.FAILED_START):
        sup, *_ = make()
        sup.ensure_component("sweeper", alive(), Origin.RECOVERY, 1)
        await settle_loop()
        unsettled = await sup.settle(reason, 1.0)
        assert unsettled == [] and sup.units["sweeper"].last_failure is None and sup.units["sweeper"].consecutive == 0


async def test_last_failure_is_written_once_in_either_order_and_the_cause_is_read_once():
    for start_first in (True, False):
        host = Host("STARTING")
        sup, *_ = make(host=host)
        sup.ensure_component("capability", dying(RuntimeError("x")), Origin.START, 1)
        await settle_loop()
        unit = sup.units["capability"]
        if start_first:
            assert sup.observe_ended_required(1) == ["capability"]
            sup.process_event(sup.queue.get_nowait())                             # the supervisor comes second: no second record
        else:
            sup.process_event(sup.queue.get_nowait())
            assert sup.observe_ended_required(1) == []
        assert unit.last_failure.generation == 1 and unit.last_failure.code == CODE_EXCEPTION
        assert sum(sup.exit_cause_reads.values()) == 1


async def test_the_hand_off_records_every_abnormally_exited_unit_not_only_the_first_failure_sup_lf_example_e():
    host = Host("STARTING")
    sup, *_ = make(host=host)
    sup.ensure_component("sweeper", alive(), Origin.START, 1)
    sup.ensure_component("risk-worker", dying(RuntimeError("r")), Origin.START, 1)
    sup.ensure_component("capability", dying(RuntimeError("c")), Origin.START, 1)
    await settle_loop()
    assert sup.ended_required() == ["risk-worker", "capability"]                   # the tie order of SUP-21
    assert sup.observe_ended_required(1) == ["risk-worker", "capability"]
    host.aborting = True                                                           # afterwards: cleanup events (priority 0)
    sup.record_settling(TaskCancelReason.FAILED_START)
    labels = []
    while not sup.queue.empty():
        labels.append(sup.process_event(sup.queue.get_nowait()))
    assert set(labels) == {"T9/T17/T27"} and sup.units["capability"].last_failure is not None
    await sup.settle(TaskCancelReason.FAILED_START, 1.0)
    assert sup.units["capability"].last_failure is not None                         # preserved by the settlement (row 5)


async def test_last_failure_of_an_earlier_generation_is_preserved_labelled_and_cleared_only_by_the_success_boundary():
    host = Host("RUNNING", 1)
    sup, *_ = make(host=host)
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await drain(sup)
    first = sup.units["sweeper"].last_failure
    await cleanup(sup)
    host.generation, host.lifecycle = 2, "STARTING"
    sup.begin_generation(2, lambda e: None)
    kept = sup.units["sweeper"].last_failure
    assert kept is first and kept.generation == 1 and sup.units["sweeper"].state is UnitState.IDLE
    assert sup.units["sweeper"].consecutive == 0 and not sup.units["sweeper"].window     # budgets reset (SUP-11)
    # a late writer of an older generation never replaces a newer record
    assert sup.record_last_failure("sweeper", "X", 0, None) is False
    sup.clear_last_failures()
    assert sup.last_failures() == {}


async def test_a_ready_timeout_record_is_written_for_that_unit_only():
    host = Host("STARTING")
    sup, *_ = make(host=host)
    sup.ensure_component("capability", alive(ready_now=False), Origin.START, 1)
    sup.record_last_failure("capability", "BACKGROUND_READY_TIMEOUT", 1, sup.units["capability"].identity)
    assert sup.units["capability"].last_failure.code == "BACKGROUND_READY_TIMEOUT"
    assert sup.units["sweeper"].last_failure is None
    await cleanup(sup)


# --- T6, T36: a STARTING exit only completes start_failed ---------------------------------------------------------------------
async def test_t6_and_t36_a_starting_exit_completes_start_failed_with_the_first_failure_and_never_settles():
    host = Host("STARTING")
    sup, _, _, _, failures = make(host=host)
    sup.ensure_component("sweeper", dying(RuntimeError("one")), Origin.START, 1)
    sup.ensure_component("capability", dying(RuntimeError("two")), Origin.START, 1)
    labels = await drain(sup)
    assert labels == ["T6", "T6"] and [str(e) for e in failures] == ["one", "two"]   # the Runner guards done(); first = failures[0]
    assert sup.units["sweeper"].state is UnitState.RUNNING and sup.units["sweeper"].waiter is None   # no restart in STARTING
    assert sup.units["sweeper"].consecutive == 0 and not sup.units["sweeper"].window                  # no budget counted
    assert sup.units["sweeper"].last_failure and sup.units["capability"].last_failure                # each unit its own record


# --- SUP-G: the priority table ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("lifecycle", LIFECYCLES)
@pytest.mark.parametrize("aborting", [False, True])
@pytest.mark.parametrize("same_generation", [True, False])
@pytest.mark.parametrize("in_record", [True, False])
async def test_sup_g_every_cell_has_exactly_one_priority(lifecycle, aborting, same_generation, in_record):
    host = Host(lifecycle, 3, aborting)
    sup, *_ = make(host=host)
    from app.services.background_supervisor import ExitEvent
    ev = ExitEvent(1, 3 if same_generation else 2, "sweeper", 41, False, 0.0, False, None, False)
    if in_record:
        sup.settling.add(41)
    got = sup.event_priority(ev)
    if lifecycle == "STARTING" and aborting and same_generation:
        want = 0
    elif in_record:
        want = 1
    elif same_generation and not aborting and lifecycle in ("STARTING", "RUNNING"):
        want = 2
    else:
        want = 3
    assert got == want


async def test_t34_an_event_of_a_previous_generation_is_audit_only_and_changes_nothing():
    host = Host("RUNNING", 2)
    sup, *_ = make(host=host)
    sup.ensure_component("sweeper", alive(), Origin.RECOVERY, 2)
    from app.services.background_supervisor import ExitEvent
    before = (sup.units["sweeper"].state, sup.units["sweeper"].consecutive, len(sup.units["sweeper"].window))
    assert sup.process_event(ExitEvent(9, 1, "sweeper", 999, False, 0.0, True, 0.0, False)) == "T34"
    assert (sup.units["sweeper"].state, sup.units["sweeper"].consecutive, len(sup.units["sweeper"].window)) == before
    assert sup.audit == [("sweeper", 1, 9)]
    await cleanup(sup)


async def test_an_unknown_unit_is_recorded_fail_closed_and_restarts_nothing():
    sup, *_ = make()
    from app.services.background_supervisor import ExitEvent
    assert sup.process_event(ExitEvent(1, 1, "mystery", 5, False, 0.0, False, None, False)) == "R3"
    assert len(sup.unknown_events) == 1


async def test_each_event_is_processed_once_a_second_delivery_is_ignored():
    sup, *_ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await settle_loop()
    ev = sup.queue.get_nowait()
    assert sup.process_event(ev) == "T4"
    assert sup.process_event(ev) == "HANDLED" and sup.units["sweeper"].consecutive == 1


# --- waiters (SUP-16, SUP-W, T13 to T15, T32) -------------------------------------------------------------------------------
async def test_t13_the_waiter_restarts_the_component_through_the_guarded_start_and_deregisters():
    sup, host, clock, sleeper, _ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await drain(sup)
    unit = sup.units["sweeper"]
    await settle_loop()
    assert unit.state is UnitState.BACKOFF and unit.waiter is not None and sleeper.delays == [5.0]
    old_identity = unit.identity
    await release(sleeper)
    await settle_loop()
    assert unit.state is UnitState.RUNNING and unit.waiter is None and unit.identity != old_identity


async def test_t14_a_refused_waiter_deregisters_never_counts_and_leaves_the_budgets_alone():
    for mutate, label in ((lambda h, s: setattr(h, "generation", 9), "generation"),
                          (lambda h, s: setattr(h, "lifecycle", "STOPPING"), "lifecycle"),
                          (lambda h, s: setattr(s, "supervisor_degraded", True), "supervisor")):
        sup, host, clock, sleeper, _ = make()
        sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
        await drain(sup)
        unit = sup.units["sweeper"]
        before = (unit.consecutive, len(unit.window))
        mutate(host, sup)
        await release(sleeper)
        await settle_loop()
        assert unit.task is None and unit.waiter is None, label
        assert (unit.consecutive, len(unit.window)) == before, label
        labels = await drain(sup) if host.generation == 1 else []
        assert "T32" not in labels, label                                            # a refusal is never an exit


async def test_t32_a_waiter_that_ends_by_its_own_exception_is_one_exit_of_the_same_component_budget():
    sup, host, clock, sleeper, _ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await drain(sup)
    unit = sup.units["sweeper"]
    unit.factory = lambda ready: (_ for _ in ()).throw(RuntimeError("cannot create"))      # the creation itself raises (T15)
    await release(sleeper)
    labels = await drain(sup)
    await settle_loop()
    assert labels == ["T32"] and unit.consecutive == 2 and unit.state is UnitState.BACKOFF     # counted exactly once
    assert unit.waiter is not None                                                            # a new waiter, never stuck in BACKOFF
    assert sleeper.delays == [5.0, 10.0]


async def test_a_waiter_cancelled_by_the_stop_is_a_normal_end_and_starts_no_restart():
    sup, host, clock, sleeper, _ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await drain(sup)
    unsettled = await sup.settle(TaskCancelReason.STOP, 1.0)
    assert unsettled == [] and sup.units["sweeper"].task is None and sup.units["sweeper"].waiter is None
    assert sup.units["sweeper"].state is UnitState.STOPPED and sup.units["sweeper"].consecutive == 1


async def test_a_unit_that_was_never_started_is_stopped_by_the_stop_settlement():
    sup, *_ = make()
    assert await sup.settle(TaskCancelReason.STOP, 1.0) == []
    sup.mark_stopped()
    assert all(u.state is UnitState.STOPPED for u in sup.units.values())


# --- supervisor exit (T7, T8, SUP-10) ----------------------------------------------------------------------------------------
async def test_t7_a_supervisor_that_ends_in_starting_completes_start_failed_only():
    host = Host("STARTING")
    sup, _, _, _, failures = make(host=host)
    task = sup.create_supervisor(1)
    await settle_loop()
    task.cancel()                                                                  # an unrequested cancellation
    await settle_loop()
    assert len(failures) == 1 and isinstance(failures[0], RuntimeError) and isinstance(failures[0].__cause__, asyncio.CancelledError)
    assert sup.supervisor_degraded is False                                         # never a degraded flag during STARTING


async def test_t8_a_supervisor_that_ends_in_running_degrades_the_subsystem_and_blocks_dispatch():
    host = Host("RUNNING")
    sup, *_ = make(host=host)
    task = sup.create_supervisor(1)
    await settle_loop()
    task.cancel()
    await settle_loop()
    assert sup.supervisor_degraded is True
    status = sup.status()
    assert status.background_degraded and status.supervisor_degraded
    assert [u for u in status.units if u.unit_id == SUPERVISOR_ID][0].display_state == "DEGRADED"


async def test_a_second_supervisor_for_the_same_generation_is_refused():
    sup, *_ = make()
    sup.create_supervisor(1)
    with pytest.raises(RuntimeError):
        sup.create_supervisor(1)
    await sup.settle(TaskCancelReason.STOP, 1.0)


async def test_the_supervisor_loop_processes_each_queued_event_once_and_stops_on_cancel():
    sup, host, clock, sleeper, _ = make()
    sup.create_supervisor(1)
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await settle_loop(8)
    assert [label for _, label in sup.processed] == ["T4"]
    assert sup.supervisor_ready.is_set()
    await sup.settle(TaskCancelReason.STOP, 1.0)


# --- the status read: table SUP-DS (display only) -----------------------------------------------------------------------------
def snapshot_state(sup):
    return [(u.state, u.consecutive, len(u.window), u.waiter_identity, u.identity, u.handled.copy())
            for u in sup.units.values()], len(sup.processed), sup.queue.qsize()


async def test_sup_ds_row_1_a_waiting_waiter_is_plain_backoff():
    sup, *_ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await drain(sup)
    st = [u for u in sup.status().units if u.unit_id == "sweeper"][0]
    assert st.display_state == "BACKOFF" and st.reason_code is None and sup.status().derived == ()
    await cleanup(sup)


async def test_sup_ds_row_2_backoff_without_task_or_waiter_is_orphaned_whatever_the_supervisor_state():
    for dead in (False, True):
        sup, *_ = make()
        unit = sup.units["sweeper"]
        unit.state = UnitState.BACKOFF
        sup.supervisor_degraded = dead
        st = [u for u in sup.status().units if u.unit_id == "sweeper"][0]
        assert st.display_state == DERIVED_BACKOFF_ORPHANED and st.reason_code is None and st.since is None
        assert sup.status().background_degraded is dead                                  # case A2 versus case B


async def test_sup_ds_row_3_a_done_task_with_an_unprocessed_event_is_exited_unsupervised_case_a1():
    sup, *_ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    await settle_loop()
    status = sup.status()
    assert [u for u in status.units if u.unit_id == "sweeper"][0].display_state == DERIVED_EXITED_UNSUPERVISED
    assert status.background_degraded is False and status.derived == (("sweeper", DERIVED_EXITED_UNSUPERVISED),)


async def test_sup_ds_row_4_and_5_a_done_waiter_is_derived_only_when_the_supervisor_is_dead():
    for dead, want in ((True, DERIVED_WAITER_DONE), (False, "BACKOFF")):
        sup, host, clock, sleeper, _ = make()
        sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
        await drain(sup)
        unit = sup.units["sweeper"]
        unit.waiter.cancel()
        await settle_loop()                                                            # the waiter is done, its event is queued
        sup.supervisor_degraded = dead
        st = [u for u in sup.status().units if u.unit_id == "sweeper"][0]
        assert st.display_state == want
        if dead:
            assert sup.status().background_degraded                                    # case B
        sup.queue.get_nowait() if not sup.queue.empty() else None


@pytest.mark.parametrize("lifecycle", ["STARTING", "STOPPING", "STOP_INCOMPLETE", "STOPPED"])
async def test_sup_ds_row_6_no_derived_value_outside_running(lifecycle):
    host = Host("RUNNING")
    sup, *_ = make(host=host)
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    sup.units["capability"].state = UnitState.BACKOFF
    await settle_loop()
    host.lifecycle = lifecycle
    status = sup.status()
    assert status.derived == () and all(u.display_state == u.state for u in status.units)


async def test_a_status_lookup_writes_nothing_and_repeats_identically():
    sup, host, clock, sleeper, _ = make()
    sup.ensure_component("sweeper", dying(RuntimeError("x")), Origin.RECOVERY, 1)
    sup.units["capability"].state = UnitState.BACKOFF                                  # orphaned
    await settle_loop()
    before = snapshot_state(sup)
    first = sup.status()
    for _ in range(5):
        assert sup.status() == first
    assert snapshot_state(sup) == before
    assert sleeper.delays == [] and sup.units["capability"].waiter is None             # no waiter, no recovery, no budget


async def test_a_derived_item_has_no_reason_code_and_no_since_and_a_real_degraded_item_has_both():
    sup, *_ = make()
    sup.units["capability"].state = UnitState.BACKOFF
    unit = sup.units["sweeper"]
    sup._degrade(unit, CODE_BUDGET, exhausted=True, label="T5")
    by_id = {u.unit_id: u for u in sup.status().units}
    assert by_id["capability"].reason_code is None and by_id["capability"].since is None
    assert by_id["sweeper"].reason_code == CODE_BUDGET and by_id["sweeper"].since is not None
    assert by_id["sweeper"].restart_exhausted and sup.status().background_degraded


# --- settlement ------------------------------------------------------------------------------------------------------------------
async def test_settle_tasks_is_bounded_and_lists_the_task_that_ignores_cancellation():
    release = asyncio.Event()

    async def stubborn():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    task = asyncio.ensure_future(stubborn())
    await settle_loop()
    started = asyncio.get_running_loop().time()
    assert await settle_tasks({"sweeper": task}, 0.05) == ["sweeper"]
    assert asyncio.get_running_loop().time() - started < 2.0
    release.set()
    await task


async def test_record_settling_writes_the_reason_and_the_record_before_any_cancel():
    sup, *_ = make()
    sup.ensure_component("sweeper", alive(), Origin.RECOVERY, 1)
    sup.create_supervisor(1)
    targets = sup.record_settling(TaskCancelReason.STOP)
    assert list(targets)[0] == SUPERVISOR_ID                                           # the supervisor first: it cannot restart
    assert all(not t.cancelled() for t in targets.values())                            # nothing was cancelled yet
    assert sup.units["sweeper"].identity in sup.settling and sup._reasons[sup.units["sweeper"].identity] is TaskCancelReason.STOP
    await sup.settle(TaskCancelReason.STOP, 1.0)
    assert sup.registry_empty and sup.settling == set()


async def test_a_component_that_ignores_cancellation_stays_registered_and_is_listed_by_id():
    release = asyncio.Event()

    def stubborn(ready):
        async def body():
            ready()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
        return body()

    sup, *_ = make()
    sup.ensure_component("capability", stubborn, Origin.RECOVERY, 1)
    await settle_loop()
    assert await sup.settle(TaskCancelReason.STOP, 0.05) == ["capability"]
    assert sup.registry_empty is False and sup.units["capability"].task is not None
    release.set()
    await settle_loop()
    assert await sup.settle(TaskCancelReason.STOP, 1.0) == [] and sup.registry_empty         # a repeated stop heals


def test_canonical_ids_and_the_waiter_format():
    assert COMPONENT_IDS == ("sweeper", "risk-worker", "capability")
    assert waiter_id("sweeper") == "sweeper:restart-waiter"
    assert all(c and ":" not in c and c.isascii() for c in COMPONENT_IDS) and len(set(COMPONENT_IDS)) == 3


def test_the_module_imports_no_runner_and_no_schema():
    import app.services.background_supervisor as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "app.schemas" not in src and "services.runner" not in src
