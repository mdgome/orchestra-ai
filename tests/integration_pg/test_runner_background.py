"""The REAL Runner with its background units against PostgreSQL (design 9.1, SUP-19 to SUP-32, R31-4, R31-11, D35-1 to D35-5):
the start order and the capability publication, the success boundary, the failed-start settlement and its priority, the audit
records of the failed units, the startup mutation gate, stop deadlines and STOP_INCOMPLETE, the dispatch guard and the advisory
close failure. The bodies of the sweeper and the risk worker are the phase-5 seams, so these tests drive the lifecycle, not a scan."""
import asyncio
import dataclasses
import uuid

import pytest
from sqlalchemy import select, text

from app.infra.process_control import SelfTestResult, TrackingState
from app.models.tables import ExecutionRequest, RuntimeCapability
from app.repositories.approval_repository import ExecutionRequestRepository
from app.repositories.runtime_capability_repository import RuntimeCapabilityRepository
from app.services.background_supervisor import (
    CODE_EXCEPTION, CODE_EXTERNAL, CODE_READY_TIMEOUT, SUPERVISOR_ID, UnitState)
from app.services.runner import BackgroundReadyTimeout, Runner, RunnerAlreadyActive, RunnerLifecycle, RunnerStopIncomplete
from tests.harness import Harness
from tests.integration_pg.test_runner_lifecycle import Plan, ProxyDb

KEY = 987_654_322
L = RunnerLifecycle


async def lock_free(db) -> bool:
    """True when nobody holds THIS file's Runner advisory lock (the probe is released again at once)."""
    conn = await db.engine.connect()
    try:
        got = (await conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": KEY})).scalar()
        if got:
            await conn.execute(text("select pg_advisory_unlock(:k)"), {"k": KEY})
        return bool(got)
    finally:
        await conn.close()


async def real_recovery_free(*, startup=False):
    """A startup recovery double for tests that run on the connection proxy (it has no real engine for recovery sessions)."""
    return {"outcome": "COMMITTED"}


def ok_self_test() -> SelfTestResult:
    return SelfTestResult(TrackingState.SUPPORTED)


def make_runner(h: Harness, db=None, self_test=ok_self_test, **kw) -> Runner:
    return Runner(db or h.db, h.leases, h.workflow, h.gates, [h.claude, h.codex], advisory_key=KEY, poll_seconds=3600.0,
                  claim_timeout_seconds=300, shutdown_grace_seconds=0.1, capability_self_test=self_test,
                  capability_enabled=True, **kw)


async def capability_row(db) -> dict | None:
    async with db.transaction() as s:
        row = (await s.execute(select(RuntimeCapability).where(RuntimeCapability.key == "process_tracking"))).scalar_one_or_none()
        return dict(row.value) if row is not None else None


async def settle_loop(n=10):
    for _ in range(n):
        await asyncio.sleep(0)


def stuck_body(release: asyncio.Event):
    async def body(self_ready):
        self_ready()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
    return body


async def stop_quietly(r: Runner):
    try:
        await r.stop()
    except Exception:  # noqa: BLE001 - cleanup of a test that already asserted what it wanted
        pass


# --- start order, capability publication (R31-11, 6.2) ------------------------------------------------------------------------
async def test_start_publishes_the_capability_in_order_and_creates_the_components_only_after(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    log = []
    real_upsert = RuntimeCapabilityRepository.upsert

    async def spy(self, key, value, **kw):
        log.append(("upsert", value["state"], kw.get("expected_incarnation_id") is not None))
        return await real_upsert(self, key, value, **kw)

    monkeypatch.setattr(RuntimeCapabilityRepository, "upsert", spy)

    def self_test():
        log.append(("self_test",))
        return ok_self_test()

    r = make_runner(h, self_test=self_test)
    real_create = r._create_startup_units

    async def create(ctx):
        log.append(("create_units", r._tracking_state))
        return await real_create(ctx)

    monkeypatch.setattr(r, "_create_startup_units", create)
    await r.start()
    try:
        assert log == [("upsert", "UNKNOWN", False), ("self_test",), ("upsert", "SUPPORTED", True), ("create_units", "SUPPORTED")]
        row = await capability_row(db)
        assert row["state"] == "SUPPORTED" and row["incarnation_id"] == r._incarnation_id
        backend = (await r._lock_conn.execute(text("select pg_backend_pid()"))).scalar()
        assert row["lock_backend_pid"] == backend                                # read on _lock_conn itself (R31-11)
        assert r.process_tracking_state == "SUPPORTED"
    finally:
        await r.stop()


async def test_a_failing_self_test_publishes_unavailable_and_does_not_fail_the_start(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h, self_test=lambda: SelfTestResult(TrackingState.UNAVAILABLE, "PROCESS_TRACKING_UNAVAILABLE", "assign"))
    await r.start()
    try:
        row = await capability_row(db)
        assert (row["state"], row["reason_code"]) == ("UNAVAILABLE", "PROCESS_TRACKING_UNAVAILABLE")
        assert r.process_tracking_state == "UNAVAILABLE" and r.lifecycle is L.RUNNING
    finally:
        await r.stop()


async def test_a_crashing_self_test_is_an_unavailable_environment_not_a_failed_start(db, tmp_path):
    h = Harness(db, tmp_path)

    def crash():
        raise OSError("no job objects here")

    r = make_runner(h, self_test=crash)
    await r.start()
    try:
        assert (await capability_row(db))["state"] == "UNAVAILABLE"
    finally:
        await r.stop()


async def test_a_failing_unknown_upsert_fails_the_start_closed_and_leaves_nothing_behind(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)

    async def boom(self, key, value, **kw):
        raise RuntimeError("database refused the UNKNOWN row")

    monkeypatch.setattr(RuntimeCapabilityRepository, "upsert", boom)
    r = make_runner(h)
    with pytest.raises(RuntimeError, match="UNKNOWN row"):
        await r.start()
    assert r.lifecycle is L.STOPPED and r._supervisor.registry_empty and r._lock_conn is None and not r._ctx.registry
    assert await lock_free(db)


async def test_a_failed_result_upsert_leaves_unknown_and_the_refresh_task_retries_it(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    real = RuntimeCapabilityRepository.upsert
    calls = {"n": 0}

    async def flaky(self, key, value, **kw):
        calls["n"] += 1
        if kw.get("expected_incarnation_id") and calls["n"] == 2:                 # the first RESULT write
            raise RuntimeError("lost write")
        return await real(self, key, value, **kw)

    monkeypatch.setattr(RuntimeCapabilityRepository, "upsert", flaky)
    ticks = asyncio.Queue()

    async def sleeper(delay):
        await ticks.get()

    r = make_runner(h, supervisor_sleep=sleeper)
    await r.start()
    try:
        assert (await capability_row(db))["state"] == "UNKNOWN"                   # the start still succeeded
        assert r.process_tracking_state == "SUPPORTED"                            # this incarnation's own memory
        ticks.put_nowait(1)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if (await capability_row(db))["state"] == "SUPPORTED":
                break
        assert (await capability_row(db))["state"] == "SUPPORTED"
    finally:
        await r.stop()


async def test_a_late_result_of_an_older_incarnation_never_overwrites_a_newer_one(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    try:
        async with db.transaction() as s:
            await RuntimeCapabilityRepository(s).upsert("process_tracking", {"state": "SUPPORTED", "incarnation_id": "newer"})
        assert await r._write_capability() is False                               # this incarnation is no longer the owner
        assert (await capability_row(db))["incarnation_id"] == "newer"
    finally:
        await r.stop()


async def test_a_posix_style_runner_publishes_no_row_and_runs_no_self_test(db, tmp_path):
    h = Harness(db, tmp_path)
    called = []
    r = Runner(h.db, h.leases, h.workflow, h.gates, [h.claude, h.codex], advisory_key=KEY, poll_seconds=3600.0,
               shutdown_grace_seconds=0.1, capability_self_test=lambda: called.append(1), capability_enabled=False)
    await r.start()
    try:
        assert await capability_row(db) is None and called == [] and r.process_tracking_state == "UNSUPPORTED_PLATFORM"
    finally:
        await r.stop()


async def test_a_start_that_ends_in_runner_already_active_leaves_no_task_behind(db, tmp_path):
    h = Harness(db, tmp_path)
    holder = await db.engine.connect()
    try:
        assert (await holder.execute(text("select pg_try_advisory_lock(:k)"), {"k": KEY})).scalar()
        r = make_runner(h)
        with pytest.raises(RunnerAlreadyActive):
            await r.start()
        assert r.lifecycle is L.STOPPED and r._supervisor.registry_empty and not r._ctx.registry
        assert r._supervisor.supervisor_task is None and await capability_row(db) is None
    finally:
        await holder.close()


# --- the success boundary and the registries ----------------------------------------------------------------------------------
async def test_a_successful_start_leaves_the_components_in_the_component_registry_and_the_startup_registry_empty(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    try:
        status = r.background_status()
        assert r.lifecycle is L.RUNNING and r._ctx.registry == {}
        assert [u.display_state for u in status.units] == ["RUNNING"] * 4 and status.derived == ()
        assert status.background_degraded is False and r.last_failures() == {}
        assert all(r._supervisor.units[u].ready_seen for u in ("sweeper", "risk-worker", "capability"))
        assert not r._ctx.start_failed.done() and r._ctx.mutation_done.is_set()   # discarded unused; the real recovery set the gate (setter 1)
    finally:
        await r.stop()
    assert r._supervisor.registry_empty and r.lifecycle is L.STOPPED


async def test_stop_then_start_of_the_same_object_is_a_new_generation_with_fresh_budgets_and_a_kept_stall_record(db, tmp_path):
    h = Harness(db, tmp_path)
    gate = asyncio.Event()

    async def gated(start, policy):
        await gate.wait()                                                           # the first pass is not allowed to finish
        from app.services.runner import PassOutcome
        return PassOutcome(wrapped=True)

    r = make_runner(h, sweep_pass=gated)
    await r.start()
    first = r._generation
    r._stall.record_config_invalid(pass_start=None, boundary_cursor=None, now=r._now(), fallback_seconds=86400.0)
    r._supervisor.units["sweeper"].consecutive = 3
    await r.stop()
    await r.start()
    try:
        assert r._generation > first and r._supervisor.units["sweeper"].consecutive == 0
        for _ in range(50):                                                         # the new task is held at the gate: the state is kept
            await asyncio.sleep(0.01)
        assert r.sweeper_config_invalid.config_invalid_active and r.sweeper_stall.consecutive_failures == 1   # row 31: kept
        gate.set()                                                                  # the first pass of the new task clears it (CI-5)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not r.sweeper_config_invalid.config_invalid_active:
                break
        assert not r.sweeper_config_invalid.config_invalid_active
    finally:
        gate.set()
        await r.stop()


# --- failed start: priority, the original exception, audit records ------------------------------------------------------------
async def test_a_failing_startup_recovery_is_reraised_as_the_same_object_and_everything_is_reaped(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    error = RuntimeError("recovery failed")

    async def recover(*, startup=False):
        raise error

    r.recover = recover
    with pytest.raises(RuntimeError) as info:
        await r.start()
    assert info.value is error
    assert r.lifecycle is L.STOPPED and r._supervisor.registry_empty and not r._ctx.registry
    assert r._ctx.mutation_done.is_set() is False                                   # R31-4X (i): the guard was not passed
    assert await lock_free(db)


async def test_a_component_that_dies_before_the_boundary_fails_the_start_with_its_own_exception_and_one_record(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    boom = RuntimeError("the capability updater died")
    gate = asyncio.Event()

    async def dying_capability(ready):
        ready()
        await gate.wait()
        raise boom

    async def recover(*, startup=False):
        gate.set()                                                                  # the component dies while the start waits
        await asyncio.sleep(0.2)
        return {}

    r._capability_body = dying_capability
    r.recover = recover
    with pytest.raises(RuntimeError) as info:
        await r.start()
    assert info.value is boom
    rec = r.last_failures()["capability"]
    assert (rec.code, rec.generation) == (CODE_EXCEPTION, 1) and set(r.last_failures()) == {"capability"}
    assert r.lifecycle is L.STOPPED and r._supervisor.registry_empty
    assert sum(r._supervisor.exit_cause_reads.values()) == 1                         # the cause was read once per exit
    assert r._supervisor.units["capability"].consecutive == 0                        # no budget in STARTING


async def test_two_units_that_die_are_both_recorded_by_the_hand_off_and_the_representative_follows_the_tie_order(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    errors = {"risk-worker": RuntimeError("risk"), "capability": RuntimeError("cap")}
    gate = asyncio.Event()

    def dying(name):
        async def body(ready):
            ready()
            await gate.wait()
            raise errors[name]
        return body

    async def recover(*, startup=False):
        gate.set()
        await asyncio.sleep(0.3)
        return {}

    r._risk_body, r._capability_body, r.recover = dying("risk-worker"), dying("capability"), recover
    with pytest.raises(RuntimeError) as info:
        await r.start()
    assert info.value in errors.values()
    records = r.last_failures()
    assert set(records) == {"risk-worker", "capability"}                             # EACH failed unit has its record
    assert {rec.generation for rec in records.values()} == {1}
    # SUP-21 tie order: the supervisor first, then BACKGROUND_COMPONENT_IDS (risk-worker before capability)
    assert info.value is errors["risk-worker"]


async def test_a_caller_cancellation_keeps_the_audit_record_of_a_unit_that_died_separately(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    gate, hold = asyncio.Event(), asyncio.Event()

    async def dying_capability(ready):
        ready()
        await gate.wait()
        raise RuntimeError("capability died before the cancel")

    async def recover(*, startup=False):
        await asyncio.sleep(0.3)                                                    # READY is complete: start() waits on this task only
        gate.set()
        await hold.wait()                                                           # the start waits here, then is cancelled

    r._capability_body, r.recover = dying_capability, recover
    r._supervisor.process_event = lambda ev: "NOOP"                                 # the supervisor never processes the exit event
    task = asyncio.ensure_future(r.start())
    for _ in range(500):
        await asyncio.sleep(0.01)
        unit = r._supervisor.units["capability"]
        if unit.task is not None and unit.task.done():
            break
    assert unit.task.done() and not task.done()                                     # the death is visible only through done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rec = r.last_failures().get("capability")
    assert rec is not None and rec.code == CODE_EXCEPTION and rec.generation == 1   # SUP-LF rows 4 and 9 (Q1-2)
    assert r.lifecycle is L.STOPPED and r._supervisor.registry_empty


async def test_a_signal_less_return_is_recorded_once_as_external_or_unknown(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    gate = asyncio.Event()

    async def returning_risk(ready):
        ready()
        await gate.wait()                                                           # then returns by itself: no exception

    async def recover(*, startup=False):
        gate.set()
        await asyncio.sleep(0.2)
        return {}

    r._risk_body, r.recover = returning_risk, recover
    with pytest.raises(RuntimeError, match="ended without a stop signal"):
        await r.start()
    rec = r.last_failures()["risk-worker"]
    assert rec.code == CODE_EXTERNAL and rec.generation == 1 and len(r.last_failures()) == 1
    assert sum(r._supervisor.exit_cause_reads.values()) == 1


@pytest.mark.parametrize("value", [None, 0, False, object()], ids=repr)
async def test_d35_row10_real_runner_a_signal_less_return_in_starting_is_one_record_whatever_the_value(db, tmp_path, value):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    gate = asyncio.Event()

    async def returning_risk(ready):
        ready()
        await gate.wait()
        return value

    async def recover(*, startup=False):
        gate.set()
        await asyncio.sleep(0.2)
        return {}

    r._risk_body, r.recover = returning_risk, recover
    with pytest.raises(RuntimeError, match="ended without a stop signal"):
        await r.start()
    rec = r.last_failures()["risk-worker"]
    assert rec.code == CODE_EXTERNAL and len(r.last_failures()) == 1
    assert sum(r._supervisor.exit_cause_reads.values()) == 1


@pytest.mark.parametrize("value", [None, 0, False, object()], ids=repr)
async def test_d35_row10_real_runner_a_signal_less_return_while_running_is_recorded_once_and_a_normal_stop_adds_nothing(
        db, tmp_path, value):
    h = Harness(db, tmp_path)
    gate = asyncio.Event()
    released = asyncio.Event()                                                       # the supervisor's restart delay never elapses
    calls = []

    async def no_sleep(delay):
        calls.append(delay)
        await released.wait()

    r = make_runner(h, supervisor_sleep=no_sleep)

    async def returning_sweeper(ready):
        ready()
        await gate.wait()
        return value

    r._sweeper_body = returning_sweeper
    await r.start()
    assert r.last_failures() == {}
    gate.set()                                                                       # no stop signal, no exception: it returns
    for _ in range(200):
        await asyncio.sleep(0.01)
        if r.last_failures():
            break
    recs = r.last_failures()
    assert set(recs) == {"sweeper"} and recs["sweeper"].code == CODE_EXTERNAL and calls.count(5.0) == 1
    assert sum(r._supervisor.exit_cause_reads.values()) == 1
    await r.stop()                                                                   # a normal stop (the waiter is cancelled)
    assert set(r.last_failures()) == {"sweeper"} and sum(r._supervisor.exit_cause_reads.values()) == 1
    assert r.lifecycle is L.STOPPED


async def test_d35_row10_real_runner_a_normal_stop_that_ends_a_component_with_a_return_value_records_nothing(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)

    async def swallowing_sweeper(ready):
        ready()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return 0                                                                 # swallows the settlement cancel and returns

    r._sweeper_body = swallowing_sweeper
    await r.start()
    await r.stop()
    assert r.last_failures() == {} and r.lifecycle is L.STOPPED
    assert r._supervisor.units["sweeper"].consecutive == 0 and len(r._supervisor.units["sweeper"].window) == 0   # no budget


async def test_a_ready_timeout_is_recorded_for_that_unit_only_and_fails_the_start(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    r._bg = dataclasses.replace(r._bg, ready_timeout_seconds=0.3)

    async def silent_sweeper(ready):
        await asyncio.Event().wait()                                                # never signals READY

    r._sweeper_body = silent_sweeper
    with pytest.raises(BackgroundReadyTimeout, match=CODE_READY_TIMEOUT):
        await r.start()
    assert set(r.last_failures()) == {"sweeper"} and r.last_failures()["sweeper"].code == CODE_READY_TIMEOUT
    assert r.lifecycle is L.STOPPED and r._supervisor.registry_empty


async def test_records_of_an_earlier_generation_survive_a_failed_start_and_are_cleared_by_the_next_success(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    r._bg = dataclasses.replace(r._bg, ready_timeout_seconds=0.3)
    original = r._sweeper_body

    async def silent(ready):
        await asyncio.Event().wait()

    r._sweeper_body = silent
    with pytest.raises(BackgroundReadyTimeout):
        await r.start()
    first = r.last_failures()["sweeper"]
    error = RuntimeError("generation 2 recovery failed")

    async def recover(*, startup=False):
        raise error

    r._sweeper_body, real_recover = original, r.recover
    r.recover = recover
    with pytest.raises(RuntimeError) as info:
        await r.start()
    assert info.value is error and r.last_failures()["sweeper"] is first and first.generation == 1   # example (a)
    r.recover = real_recover
    await r.start()
    try:
        assert r.last_failures() == {}                                               # example (d): cleared at the boundary
    finally:
        await r.stop()


async def test_a_caller_cancellation_during_start_does_not_orphan_the_startup_tasks(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    hold = asyncio.Event()

    async def recover(*, startup=False):
        await hold.wait()

    r.recover = recover
    task = asyncio.ensure_future(r.start())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if r._ctx is not None and r._ctx.recovery_task is not None and not r._ctx.recovery_task.done():
            break
    created = list(r._supervisor.settle_targets().values()) + list(r._ctx.registry.values())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(t.done() for t in created) and r.lifecycle is L.STOPPED and r._supervisor.registry_empty
    assert await lock_free(db)


async def test_a_second_cancellation_during_the_settlement_never_leaves_the_lifecycle_in_starting(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    release = asyncio.Event()

    async def stubborn(ready):
        ready()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    r._sweeper_body = stubborn
    r._bg = dataclasses.replace(r._bg, settle_timeout_seconds=0.4)
    hold = asyncio.Event()

    async def recover(*, startup=False):
        await hold.wait()

    r.recover = recover
    task = asyncio.ensure_future(r.start())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if r._ctx is not None and r._ctx.recovery_task is not None and not r._ctx.recovery_task.done():
            break
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()                                                                   # the second cancellation, mid-settlement
    with pytest.raises(asyncio.CancelledError):
        await task
    assert r.lifecycle is L.STOP_INCOMPLETE                                          # never STARTING
    assert "sweeper" in r._supervisor.unsettled_ids() and r._lock_conn is not None   # the lock is kept (D29-2)
    release.set()
    await r.stop()                                                                   # only a repeated stop heals
    assert r.lifecycle is L.STOPPED and await lock_free(db)


# --- startup mutation gate (R31-4, table R31-4X) ------------------------------------------------------------------------------
async def test_the_sweeper_first_pass_is_held_until_the_startup_mutation_is_done(db, tmp_path):
    h = Harness(db, tmp_path)
    passes = []

    async def sweep(start, policy):
        passes.append(1)
        from app.services.runner import PassOutcome
        return PassOutcome(wrapped=True)

    r = make_runner(h, sweep_pass=sweep)
    gate = asyncio.Event()

    async def recover(*, startup=False):
        await gate.wait()
        return {}

    r.recover = recover
    task = asyncio.ensure_future(r.start())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if r._supervisor.units["sweeper"].ready_seen:
            break
    await asyncio.sleep(0.1)
    assert passes == [] and not r._ctx.mutation_done.is_set()                         # READY is the entry point, the pass waits
    gate.set()
    await task
    try:
        assert r._ctx.mutation_done.is_set()                                          # case (iii): the wrapper set it (setter 2)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if passes:
                break
        assert passes
    finally:
        await r.stop()


async def test_case_ii_the_set_point_was_passed_then_recover_rest_fails_the_event_stays_set(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    error = RuntimeError("rest failed")

    async def rest(summary, generation=None, startup=False):
        raise error

    r._recover_rest = rest
    with pytest.raises(RuntimeError) as info:
        await r.start()
    assert info.value is error and r._ctx.mutation_done.is_set()                      # set before _recover_rest, never reset
    assert r.lifecycle is L.STOPPED and r._supervisor.registry_empty                  # the released sweeper was settled


async def test_case_iii_a_double_that_returns_early_and_normally_sets_the_event_and_the_start_succeeds(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)

    async def recover(*, startup=False):
        return {"outcome": "COMMITTED"}

    r.recover = recover
    await r.start()
    try:
        assert r._ctx.mutation_done.is_set() and r.lifecycle is L.RUNNING             # the event alone never decides success
    finally:
        await r.stop()


async def test_case_i_with_a_real_unsettled_recovery_the_event_is_not_set_and_the_start_fails(db, tmp_path, monkeypatch):
    from app.services.runner import RecoveryUnsettled
    h = Harness(db, tmp_path)
    r = make_runner(h)

    async def recover(*, startup=False):
        raise RecoveryUnsettled(None, "the connection release is FAILED")

    r.recover = recover
    with pytest.raises(RecoveryUnsettled):
        await r.start()
    assert r._ctx.mutation_done.is_set() is False


# --- stop deadlines, STOP_INCOMPLETE, the advisory close --------------------------------------------------------------------------
async def test_stop_with_a_stubborn_component_is_stop_incomplete_with_its_id_and_a_repeated_stop_heals(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    release = asyncio.Event()
    r._capability_body = stuck_body(release)
    await r.start()
    r._bg = dataclasses.replace(r._bg, settle_timeout_seconds=0.3)
    started = asyncio.get_running_loop().time()
    with pytest.raises(RunnerStopIncomplete) as info:
        await r.stop()
    assert asyncio.get_running_loop().time() - started < 5.0                          # one common deadline, never unbounded
    assert info.value.components == ("capability",) and info.value.run_ids == ()
    assert "capability" in str(info.value) and "task(s)" not in str(info.value)
    assert r.lifecycle is L.STOP_INCOMPLETE and r._lock_conn is not None and not await lock_free(db)
    release.set()
    await r.stop()
    assert r.lifecycle is L.STOPPED and await lock_free(db) and r._supervisor.registry_empty


async def test_the_common_deadline_bounds_all_components_together_not_one_wait_each(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    release = asyncio.Event()
    r._capability_body, r._risk_body = stuck_body(release), stuck_body(release)
    await r.start()
    r._bg = dataclasses.replace(r._bg, settle_timeout_seconds=0.5)
    started = asyncio.get_running_loop().time()
    with pytest.raises(RunnerStopIncomplete) as info:
        await r.stop()
    assert asyncio.get_running_loop().time() - started < 0.5 * 2                      # not 2 x 0.5
    assert set(info.value.components) == {"capability", "risk-worker"}
    release.set()
    await r.stop()


async def test_a_loop_task_that_ignores_cancellation_is_listed_as_runner_loop_under_its_own_deadline(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    release = asyncio.Event()

    async def stubborn_loop():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    real_loop = r._loop_task
    real_loop.cancel()
    await asyncio.gather(real_loop, return_exceptions=True)
    r._loop_task = asyncio.ensure_future(stubborn_loop())
    r._bg = dataclasses.replace(r._bg, recovery_shutdown_wait_seconds=0.1, recovery_settle_margin_seconds=0.2)
    with pytest.raises(RunnerStopIncomplete) as info:
        await r.stop()
    assert info.value.components == ("runner-loop",) and r._loop_task is not None     # the reference stays
    release.set()
    await r.stop()
    assert r.lifecycle is L.STOPPED


async def test_stop_records_the_stop_reason_before_it_cancels_and_a_stop_is_never_a_sweeper_failure(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    seen = {}
    real = r._supervisor.record_settling

    def spy(reason):
        targets = real(reason)
        seen["reason"] = reason.value
        seen["cancelled_before"] = [t.cancelled() for t in targets.values()]
        return targets

    r._supervisor.record_settling = spy
    await r.stop()
    assert seen["reason"] == "STOP" and not any(seen["cancelled_before"])
    assert r.sweeper_stall.consecutive_failures == 0 and r.last_failures() == {}


async def test_the_advisory_close_failing_alone_reraises_the_original_exception_keeps_the_connection_and_a_retry_heals(db, tmp_path):
    h = Harness(db, tmp_path)
    plan = Plan(fail_close=1)
    proxy_db = ProxyDb(db, plan)
    r = make_runner(h, db=proxy_db)
    r.recover = real_recovery_free
    try:
        await r.start()
        with pytest.raises(OSError, match="close failed") as info:
            await r.stop()
        assert not isinstance(info.value, RunnerStopIncomplete)                        # no incomplete error for an empty set
        assert r.lifecycle is L.STOP_INCOMPLETE and r._lock_conn is not None           # the reference is kept
        assert r._supervisor.registry_empty                                            # the components did settle
        await r.stop()                                                                 # the retry closes the SAME connection
        assert r.lifecycle is L.STOPPED and r._lock_conn is None
        assert len(proxy_db.proxies) == 1 and proxy_db.proxies[0].underlying_close_calls == 1
    finally:
        await proxy_db.cleanup(db)


async def test_an_unsettled_component_and_a_failing_close_make_one_incomplete_error_with_the_close_error_attached(db, tmp_path):
    h = Harness(db, tmp_path)
    plan = Plan(fail_close=1)
    proxy_db = ProxyDb(db, plan)
    r = make_runner(h, db=proxy_db)
    r.recover = real_recovery_free
    release = asyncio.Event()
    r._capability_body = stuck_body(release)
    try:
        await r.start()
        r._bg = dataclasses.replace(r._bg, settle_timeout_seconds=0.2)
        with pytest.raises(RunnerStopIncomplete) as info:
            await r.stop()
        assert info.value.components == ("capability",)
        release.set()
        with pytest.raises(OSError):
            await r.stop()                                                             # now the close is the only failure
        await r.stop()
    finally:
        await proxy_db.cleanup(db)


# --- dispatch guard (R31-2) ---------------------------------------------------------------------------------------------------
async def pending_requests(h, n):
    for _ in range(n):
        task = await h.new_task()
        await h.tasks.request_execution(task.id)


async def test_dispatch_guard_point_1_a_set_flag_claims_nothing(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await pending_requests(h, 3)
    r = make_runner(h)
    r._state = L.RUNNING
    claims = []
    real = ExecutionRequestRepository.claim_next

    async def spy(self, *a, **kw):
        claims.append(1)
        return await real(self, *a, **kw)

    monkeypatch.setattr(ExecutionRequestRepository, "claim_next", spy)
    r._supervisor.supervisor_degraded = True
    assert await r._dispatch() == 0 and claims == []


async def test_dispatch_guard_point_2_a_flip_before_claim_next_leaves_the_transaction_without_a_claim(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await pending_requests(h, 2)
    r = make_runner(h)
    r._state = L.RUNNING
    real_tx = r.db.transaction

    class FlipOnEnter:
        def __init__(self, cm):
            self.cm = cm

        async def __aenter__(self):
            session = await self.cm.__aenter__()
            r._supervisor.supervisor_degraded = True                                   # the flag flips inside the window
            return session

        async def __aexit__(self, *exc):
            return await self.cm.__aexit__(*exc)

    monkeypatch.setattr(r.db, "transaction", lambda: FlipOnEnter(real_tx()), raising=False) if hasattr(r.db, "__dict__") else None
    launched = []
    r._launch = lambda *a: launched.append(a)
    assert await r._dispatch() == 0 and launched == []
    async with db.transaction() as s:
        statuses = [x.status for x in (await s.execute(select(ExecutionRequest))).scalars()]
    assert statuses.count("CLAIMED") == 0


async def test_dispatch_guard_point_3_a_flip_after_the_commit_launches_nothing_and_leaves_one_claimed_row(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await pending_requests(h, 3)
    r = make_runner(h)
    r._state = L.RUNNING
    real = ExecutionRequestRepository.claim_next

    async def claim_then_flip(self, *a, **kw):
        request = await real(self, *a, **kw)
        r._supervisor.supervisor_degraded = True                                       # between the claim and _launch
        return request

    monkeypatch.setattr(ExecutionRequestRepository, "claim_next", claim_then_flip)
    launched = []
    r._launch = lambda *a: launched.append(a)
    assert await r._dispatch() == 0 and launched == []                                 # `break`, never `continue`
    async with db.transaction() as s:
        statuses = [x.status for x in (await s.execute(select(ExecutionRequest))).scalars()]
    assert statuses.count("CLAIMED") == 1 and statuses.count("PENDING") == 2           # exactly one stays CLAIMED (R31-3)


async def test_a_degraded_sweeper_risk_worker_or_capability_never_blocks_dispatch(db, tmp_path):
    h = Harness(db, tmp_path)
    await pending_requests(h, 1)
    r = make_runner(h)
    r._state = L.RUNNING
    for unit in ("sweeper", "risk-worker", "capability"):
        r._supervisor.units[unit].state = UnitState.DEGRADED
    launched = []
    r._launch = lambda request, project: launched.append(request)
    assert await r._dispatch() == 1 and len(launched) == 1
    assert r.background_degraded is True and r.supervisor_degraded is False


async def test_a_supervisor_that_dies_while_running_blocks_new_work_and_stop_still_settles_everything(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    try:
        r._supervisor.supervisor_task.cancel()                                          # an unexpected end of the supervisor
        await settle_loop(20)
        assert r.supervisor_degraded and r.lifecycle is L.RUNNING                       # not STOP_INCOMPLETE
        assert r._dispatch_blocked()
        status = r.background_status()
        assert status.background_degraded and status.supervisor_degraded
        assert [u for u in status.units if u.unit_id == SUPERVISOR_ID][0].display_state == "DEGRADED"
    finally:
        await r.stop()
    assert r.lifecycle is L.STOPPED and r._supervisor.registry_empty


async def test_a_component_that_dies_while_running_is_restarted_through_the_waiter_with_the_bounded_delay(db, tmp_path):
    h = Harness(db, tmp_path)
    delays, gate = [], asyncio.Queue()

    async def sleeper(delay):
        delays.append(delay)
        await gate.get()

    r = make_runner(h, supervisor_sleep=sleeper)
    await r.start()
    try:
        capability = r._supervisor.units["capability"]
        old_identity = capability.identity
        capability.task.cancel()                                                        # an unexpected top-level cancellation
        for _ in range(100):
            await asyncio.sleep(0.01)
            if capability.state is UnitState.BACKOFF and delays:
                break
        assert capability.state is UnitState.BACKOFF and capability.consecutive == 1 and 5.0 in delays
        assert capability.last_failure.code == CODE_EXTERNAL == "EXTERNAL_OR_UNKNOWN"
        assert len(capability.window) == 1 and delays.count(5.0) == 1                  # exactly one budget step / one restart delay
        assert sum(r._supervisor.exit_cause_reads.values()) == 1 and set(r.last_failures()) == {"capability"}
        gate.put_nowait(1)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if capability.state is UnitState.RUNNING:
                break
        assert capability.state is UnitState.RUNNING and capability.identity != old_identity
        assert r.lifecycle is L.RUNNING and not r.background_degraded
    finally:
        await r.stop()


async def test_a_waiter_in_backoff_is_cancelled_by_the_stop_and_creates_nothing(db, tmp_path):
    h = Harness(db, tmp_path)

    async def forever(delay):
        await asyncio.Event().wait()

    r = make_runner(h, supervisor_sleep=forever)
    await r.start()
    capability = r._supervisor.units["capability"]
    capability.task.cancel()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if capability.state is UnitState.BACKOFF:
            break
    assert capability.waiter is not None
    await r.stop()
    assert capability.task is None and capability.waiter is None and r._supervisor.registry_empty
    assert r.lifecycle is L.STOPPED


# --- the container wiring -----------------------------------------------------------------------------------------------------
async def test_the_container_wires_the_retained_settler_the_capability_state_and_the_executor(db, tmp_path):
    from app.config import Settings
    from app.container import build_container
    settings = Settings(database_url="postgresql+asyncpg://unused/x", workspace_root=str(tmp_path / "ws"),
                        project_clone_root=str(tmp_path / "clones"))
    container = build_container(settings, db=db)
    runner = container.runner
    steps, leases = runner.workflow.steps, runner.leases
    assert runner._executor is container.executor
    assert leases._retained_settler == container.executor.resettle_retained
    assert steps._tracking_state() == runner.process_tracking_state == ("UNKNOWN" if runner._capability_enabled
                                                                          else "UNSUPPORTED_PLATFORM")
