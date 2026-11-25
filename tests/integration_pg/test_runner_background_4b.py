"""Phase 4b: the REAL Runner on PostgreSQL for the per-row supervisor cases (SUP-T T7, T10, T12, T27 to T31, table SUP-X), the
registry cleanup between starts, the bounded capability refresh, the shared DB semaphore, a real unresolved tracked row in
`_park_uncertain` / takeover (the `ids_before` RECOVERY attach), the retained-handle wiring through the container and one start
with the REAL tracking self-test (Windows only)."""
import asyncio
import dataclasses
import sys
import time
import uuid

import pytest
from sqlalchemy import select

from app.models.tables import Approval, Execution, Task
from app.repositories.approval_repository import ExecutionRequestRepository
from app.repositories.runtime_capability_repository import RuntimeCapabilityRepository
from app.services.background_supervisor import ExitEvent, UnitState
from app.services.lease_service import AcquireStatus, LeaseService, TrackedVerdict
from app.services.runner import Runner, RunnerLifecycle, RunnerStopIncomplete
from tests.harness import Harness
from tests.helpers import FakeClock
from tests.integration_pg.test_runner_background import (
    KEY, lock_free, make_runner, ok_self_test, settle_loop, stuck_body)
from tests.integration_pg.test_tracked_takeover import add_tracked

L = RunnerLifecycle


def fast_stop(r: Runner, seconds=0.3):
    r._bg = dataclasses.replace(r._bg, settle_timeout_seconds=seconds)


async def wait_for(predicate, tries=300):
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# --- T12 / T31: a unit that has nothing to cancel ---------------------------------------------------------------------------
async def test_t12_a_waiter_less_backoff_unit_is_stopped_never_listed_and_never_awaited(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    unit = r._supervisor.units["capability"]
    unit.task.cancel()
    await settle_loop(30)
    unit.waiter and unit.waiter.cancel()                                 # lose the waiter: BACKOFF with neither task nor waiter
    await settle_loop(10)
    unit.state, unit.task, unit.waiter, unit.waiter_identity = UnitState.BACKOFF, None, None, None
    derived = {u.unit_id: u.display_state for u in r.background_status().units}
    assert derived["capability"] == "BACKOFF_ORPHANED"
    await r.stop()                                                       # nothing of it exists that could stay unsettled
    assert r.lifecycle is L.STOPPED and unit.state is UnitState.STOPPED and r._supervisor.registry_empty


async def test_t31_a_unit_that_never_started_in_this_generation_is_stopped_by_the_stop(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    unit = r._supervisor.units["risk-worker"]
    unit.task.cancel()
    await settle_loop(30)
    unit.task = unit.waiter = None
    unit.state = UnitState.IDLE
    await r.stop()
    assert unit.state is UnitState.STOPPED and r.lifecycle is L.STOPPED


async def test_t11_stop_on_a_stopped_runner_is_a_no_op_that_touches_nothing(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    before = (r._generation, r.lifecycle, dict(r._supervisor.units) and r._supervisor.registry_empty)
    await r.stop()
    await r.stop()
    assert (r._generation, r.lifecycle, r._supervisor.registry_empty) == before and r._ctx is None
    assert await lock_free(db)


# --- T27 to T30, T10: what a stop does with a unit that ignores the cancel ---------------------------------------------------
async def test_t28_and_t30_a_restart_waiter_that_ignores_the_cancel_is_listed_by_its_id_and_creates_nothing_when_it_wakes(db, tmp_path):
    h = Harness(db, tmp_path)
    release = asyncio.Event()

    async def stubborn_sleep(delay):
        if delay != 5.0:                                                 # the capability refresh sleep stays cancellable
            await asyncio.Event().wait()
        while not release.is_set():                                      # only the first restart delay (5.0 s) ignores the cancel
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    r = make_runner(h, supervisor_sleep=stubborn_sleep)
    await r.start()
    unit = r._supervisor.units["capability"]
    unit.task.cancel()
    assert await wait_for(lambda: unit.state is UnitState.BACKOFF and unit.waiter is not None)
    fast_stop(r)
    with pytest.raises(RunnerStopIncomplete) as info:                    # T28 in STOPPING: unsettled, listed
        await r.stop()
    assert info.value.components == ("capability:restart-waiter",) and r.lifecycle is L.STOP_INCOMPLETE
    assert not await lock_free(db)
    release.set()                                                        # the waiter wakes in STOP_INCOMPLETE (T30): refused
    assert await wait_for(lambda: unit.waiter is None or unit.waiter.done())
    await r.stop()
    assert r.lifecycle is L.STOPPED and unit.task is None and unit.consecutive == 1      # no restart, nothing counted again


async def test_t27_and_t29_a_supervisor_that_ignores_the_cancel_is_listed_and_a_repeated_stop_heals(db, tmp_path):
    h = Harness(db, tmp_path)
    release = asyncio.Event()
    r = make_runner(h)

    async def stubborn_run(queue, ready):
        ready.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    r._supervisor._run = stubborn_run
    await r.start()
    fast_stop(r)
    with pytest.raises(RunnerStopIncomplete) as info:
        await r.stop()
    assert info.value.components == ("supervisor",) and r.lifecycle is L.STOP_INCOMPLETE
    assert not r.supervisor_degraded                                     # a settled-by-stop supervisor is never a degraded flag
    release.set()
    await r.stop()
    assert r.lifecycle is L.STOPPED and await lock_free(db) and r._supervisor.registry_empty


async def test_t10_a_component_that_dies_in_stop_incomplete_starts_no_restart_and_counts_nothing(db, tmp_path):
    h = Harness(db, tmp_path)
    release = asyncio.Event()

    async def late_death(ready):
        ready()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        raise RuntimeError("dies after the stop gave up")

    r = make_runner(h)
    r._capability_body = late_death
    await r.start()
    fast_stop(r)
    with pytest.raises(RunnerStopIncomplete):
        await r.stop()
    release.set()
    unit = r._supervisor.units["capability"]
    assert await wait_for(lambda: unit.task is not None and unit.task.done())
    await settle_loop()
    assert unit.consecutive == 0 and unit.waiter is None and unit.state is not UnitState.BACKOFF
    await r.stop()
    assert r.lifecycle is L.STOPPED and r.last_failures() == {}


# --- table SUP-X through a real Runner ---------------------------------------------------------------------------------------
async def test_sup_x_branch_2_a_unit_that_dies_while_the_failed_start_is_settled_is_cleanup_only(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    r._bg = dataclasses.replace(r._bg, ready_timeout_seconds=0.3)

    async def silent(ready):
        await asyncio.Event().wait()

    async def dies_on_cancel(ready):
        ready()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("blew up while being settled")

    r._sweeper_body, r._risk_body = silent, dies_on_cancel
    with pytest.raises(RuntimeError, match="BACKGROUND_READY_TIMEOUT"):
        await r.start()
    risk = r._supervisor.units["risk-worker"]
    assert risk.consecutive == 0 and risk.waiter is None and set(r.last_failures()) == {"sweeper"}   # priority 0: no record
    assert r.lifecycle is L.STOPPED and r._supervisor.registry_empty


async def test_sup_x_branches_4_and_5_three_stop_retries_keep_the_identity_in_the_settling_record(db, tmp_path):
    h = Harness(db, tmp_path)
    release = asyncio.Event()

    async def late_death(ready):
        ready()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        raise RuntimeError("late")

    r = make_runner(h)
    r._capability_body = late_death
    await r.start()
    fast_stop(r, 0.2)
    generations = []
    for _ in range(3):                                                   # every stop() bumps `_generation`
        with pytest.raises(RunnerStopIncomplete):
            await r.stop()
        generations.append(r._generation)
    assert generations == sorted(set(generations)) and len(generations) == 3
    identity = r._supervisor.units["capability"].identity
    assert identity in r._supervisor.settling                            # retained until its termination is confirmed
    release.set()
    unit = r._supervisor.units["capability"]
    assert await wait_for(lambda: unit.task.done())
    await settle_loop()
    events = [r._supervisor.queue.get_nowait() for _ in range(r._supervisor.queue.qsize())]
    mine = [e for e in events if e.unit_id == "capability"]
    assert len(mine) == 1
    # a confirmed-ended unit (risk-worker, settled by the first stop) is audit only; the unsettled identity is still recorded
    assert [r._supervisor.event_priority(e) for e in events if e.unit_id != "capability"] == [3] * (len(events) - 1)
    assert r._supervisor.event_priority(mine[0]) == 1                    # a settling identity, not an unrelated stale one
    r._supervisor.process_event(mine[0])
    assert unit.consecutive == 0 and unit.waiter is None and r.last_failures() == {}
    await r.stop()
    assert r.lifecycle is L.STOPPED and identity not in r._supervisor.settling


async def test_sup_x_branch_3_an_event_of_an_unrelated_stale_generation_is_audit_only(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    try:
        stale = ExitEvent(99, r._generation - 1, "capability", 12345, False, 0.0, True, 0.0, False)
        before = (r._supervisor.units["capability"].state, r._supervisor.units["capability"].consecutive)
        assert r._supervisor.process_event(stale) == "T34"
        assert (r._supervisor.units["capability"].state, r._supervisor.units["capability"].consecutive) == before
        assert r._supervisor.audit[-1] == ("capability", r._generation - 1, 99)
    finally:
        await r.stop()


# --- supervisor death while STARTING (T7), through a real start() ------------------------------------------------------------
async def test_t7_a_supervisor_that_raises_during_starting_fails_the_start_and_sets_no_degraded_flag(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    boom = RuntimeError("supervisor loop crashed")
    gate = asyncio.Event()

    async def crashing_run(queue, ready):
        ready.set()
        await gate.wait()
        raise boom

    async def recover(*, startup=False):
        gate.set()
        await asyncio.sleep(0.3)
        return {}

    r._supervisor._run, r.recover = crashing_run, recover
    with pytest.raises(RuntimeError) as info:
        await r.start()
    assert info.value is boom
    assert r.lifecycle is L.STOPPED and not r.supervisor_degraded and r._supervisor.registry_empty


async def test_t7_an_unrequested_cancellation_of_the_supervisor_becomes_a_start_error_with_the_cancel_as_cause(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    hold = asyncio.Event()

    async def recover(*, startup=False):
        await hold.wait()

    r.recover = recover
    task = asyncio.ensure_future(r.start())
    assert await wait_for(lambda: r._supervisor.supervisor_task is not None and r._ctx.recovery_task is not None)
    await settle_loop(20)
    r._supervisor.supervisor_task.cancel()
    with pytest.raises(RuntimeError) as info:
        await asyncio.wait_for(task, 10)
    assert isinstance(info.value.__cause__, asyncio.CancelledError)
    assert r.lifecycle is L.STOPPED and not r.supervisor_degraded


async def test_t6_the_exit_of_a_component_is_recorded_by_the_supervisor_and_start_never_settles_from_it(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    boom = RuntimeError("risk worker died")
    gate = asyncio.Event()

    async def dying(ready):
        ready()
        await gate.wait()
        raise boom

    async def recover(*, startup=False):
        await asyncio.sleep(0.3)
        gate.set()
        await asyncio.sleep(1.0)
        return {}

    r._risk_body, r.recover = dying, recover
    with pytest.raises(RuntimeError) as info:
        await r.start()
    assert info.value is boom and r.last_failures()["risk-worker"].generation == 1
    assert r._supervisor.units["risk-worker"].waiter is None and r._supervisor.units["risk-worker"].consecutive == 0


# --- registries between two starts of the same object -----------------------------------------------------------------------
async def test_the_registries_are_empty_after_a_failed_start_and_the_same_object_starts_again(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    error = RuntimeError("first start fails")
    real = r.recover

    async def failing(*, startup=False):
        raise error

    r.recover = failing
    with pytest.raises(RuntimeError) as info:
        await r.start()
    assert info.value is error and r.lifecycle is L.STOPPED
    assert r._ctx.registry == {} and r._supervisor.registry_empty and r._loop_task is None and r._lock_conn is None
    assert r._startup_alive() == []
    r.recover = real
    await r.start()                                                      # not refused with 'STOPPED but still holds resources'
    try:
        assert r.lifecycle is L.RUNNING and r._ctx.registry == {} and r._generation == 2
    finally:
        await r.stop()


# --- the capability refresh: bounded writes, a failure is retried -----------------------------------------------------------
async def test_a_capability_write_is_bounded_by_the_write_timeout_and_the_next_tick_retries(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    ticks = asyncio.Queue()

    async def sleeper(delay):
        await ticks.get()

    r = make_runner(h, supervisor_sleep=sleeper)
    await r.start()
    try:
        r._bg = dataclasses.replace(r._bg, capability_write_timeout_seconds=0.2)
        real = RuntimeCapabilityRepository.upsert
        mode = {"hang": True}

        async def slow(self, key, value, **kw):
            if mode["hang"]:
                await asyncio.sleep(30)
            return await real(self, key, value, **kw)

        monkeypatch.setattr(RuntimeCapabilityRepository, "upsert", slow)
        started = time.monotonic()
        assert await r._write_capability() is False
        assert time.monotonic() - started < 3.0                          # bounded, never the 30 s of the hang
        mode["hang"] = False
        ticks.put_nowait(1)                                              # the refresh task wakes: the retry succeeds
        capability = r._supervisor.units["capability"]
        await asyncio.sleep(0.3)
        assert capability.state is UnitState.RUNNING and await r._write_capability() is True
    finally:
        await r.stop()


# --- BACKGROUND_DB_CONCURRENCY with a real Runner ---------------------------------------------------------------------------
async def test_the_capability_write_waits_for_a_busy_db_slot_but_a_closing_path_does_not(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    try:
        assert r._db_slots._value == 1
        async with r.background_db_section():                            # another component holds the only slot
            write = asyncio.ensure_future(r._write_capability())
            await asyncio.sleep(0.2)
            assert not write.done()                                      # the refresh waits for the slot
            async with db.transaction() as s:                            # a closing write / dispatch is never throttled
                assert (await s.execute(select(Execution.id).limit(1))).first() is None
        assert await asyncio.wait_for(write, 10) is True
    finally:
        await r.stop()


# --- a real unresolved tracked row: the takeover parks it and attaches the gate at its checkpoint ---------------------------
async def parked_gate(db, tmp_path, checkpoint):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    clock = FakeClock()
    first = (await LeaseService(db, "runner-A", ttl_seconds=30, clock=clock).acquire(task.id)).lease
    await add_tracked(db, task, first.run_id, pid=4242, create_time=1.0,
                      detail={"tracked": True, "v": 1, "checkpoint_step_key": checkpoint,
                              "runner_process": {"host": "elsewhere", "pid": 1, "create_time": 1.0}})
    clock.advance(31)
    leases = LeaseService(db, "runner-B", ttl_seconds=30, clock=clock,
                          tracked_verdict=lambda row: TrackedVerdict(False, reason="RUNNER_NOT_PROVEN_DEAD"))
    r = Runner(db, leases, h.workflow, h.gates, [h.claude, h.codex], advisory_key=KEY, capability_enabled=False)
    await h.tasks.request_execution(task.id)
    async with db.transaction() as s:
        (await s.execute(select(Task).where(Task.id == task.id))).scalar_one().status = "TESTING"   # an interrupted tester call
        request = await ExecutionRequestRepository(s).claim_next(r.runner_id)
    outcome = await r.process(request)
    async with db.transaction() as s:
        approvals = (await s.execute(select(Approval).where(Approval.task_id == task.id))).scalars().all()
    return outcome, approvals


async def test_a_real_unresolved_tracked_row_parks_the_task_at_the_checkpoint_of_the_row_ids_before(db, tmp_path):
    outcome, approvals = await parked_gate(db, tmp_path, "STEP_TEST")
    assert outcome == "UNCERTAIN"
    recovery = [a for a in approvals if a.type == "RECOVERY"]
    assert len(recovery) == 1 and recovery[0].checkpoint_step_key == "STEP_TEST"     # TESTING, not PLANNING


async def test_without_a_checkpoint_in_the_row_the_gate_falls_back_to_the_plan(db, tmp_path):
    outcome, approvals = await parked_gate(db, tmp_path, "")
    assert outcome == "UNCERTAIN"
    assert [a.checkpoint_step_key for a in approvals if a.type == "RECOVERY"] == ["PLAN"]


# --- the retained-handle registry through the REAL container wiring ----------------------------------------------------------
async def test_a_retained_job_of_the_executor_is_settled_again_by_the_takeover_of_the_container_lease_service(db, tmp_path, monkeypatch):
    from app.config import Settings
    from app.container import build_container
    settings = Settings(database_url="postgresql+asyncpg://unused/x", workspace_root=str(tmp_path / "ws"),
                        project_clone_root=str(tmp_path / "clones"))
    container = build_container(settings, db=db)
    executor, leases = container.executor, container.runner.leases
    clock = FakeClock()
    leases._now = clock
    h = Harness(db, tmp_path)
    task = await h.new_task()
    first = (await LeaseService(db, "runner-A", ttl_seconds=30, clock=clock).acquire(task.id)).lease
    row_id = await add_tracked(db, task, first.run_id, pid=4242, create_time=1.0)

    class Job:
        closed = 0

    job = Job()
    executor._unconfirmed[row_id] = job

    async def prove(self_job):
        return True, "TERMINATED"

    monkeypatch.setattr(executor, "_kill_and_prove", prove)
    monkeypatch.setattr(type(executor), "_close_job", staticmethod(lambda j: setattr(j, "closed", 1)))
    leases._tracked_verdict = lambda row: (_ for _ in ()).throw(AssertionError("the OS verdict must not run"))
    clock.advance(31)
    result = await leases.acquire(task.id)
    assert result.status is AcquireStatus.ACQUIRED
    assert executor.retained_job(row_id) is None and job.closed == 1                # the registry entry was settled and dropped
    async with db.transaction() as s:
        row = (await s.execute(select(Execution).where(Execution.id == row_id))).scalar_one()
    assert row.detail["proof"] == "JOB_EMPTY"


# --- one start with the REAL tracking self-test (this Windows host) ----------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="the tracking self-test is Windows only")
async def test_a_start_with_the_real_self_test_publishes_supported_and_reports_its_wall_clock(db, tmp_path, capsys):
    h = Harness(db, tmp_path)
    r = Runner(db, h.leases, h.workflow, h.gates, [h.claude, h.codex], advisory_key=KEY, poll_seconds=3600.0,
               shutdown_grace_seconds=0.1)                                           # the default self-test: the real protocol
    started = time.monotonic()
    await r.start()
    elapsed = time.monotonic() - started
    try:
        assert r.process_tracking_state == "SUPPORTED" and r.lifecycle is L.RUNNING
        with capsys.disabled():
            print(f"\nREAL SELF-TEST START: {elapsed:.2f} s wall clock, state {r.process_tracking_state}")
        assert elapsed < 30.0
    finally:
        await r.stop()
