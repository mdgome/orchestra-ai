"""Runner-side sweeper bookkeeping without a database: the pass internals (SUP-28 three cases, rows 22 to 30), RV-3
(`_recheck_sweeper_configuration`, SUP-30), the startup wait (both paths of SUP-31 / R31-4X), the common set condition of
`startup_mutation_done` (D35-3), the dispatch predicate (R31-2), `_cancel_cli`, the checkpoint key of `_park_uncertain` and the
runtime status. A Runner is constructed with dummies: nothing here touches the engine."""
import asyncio
import dataclasses
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.repositories.task_repository import TrackedRow
from app.services.background_supervisor import UnitState
from app.services.runner import ConfigWakeResult, PassOutcome, Runner, RunnerLifecycle, StartContext
from app.services.runtime_status import RunnerBlocker, StartupOutcome, runtime_snapshot, snapshot_of
from app.services.sweeper_stall import SWEEP_CONFIG_INVALID, SWEEPER_CONFIG_INVALID_REASONS, SweeperFallbackInvalid
from app.validation import policies as P
from app.validation.validators import build_sweeper_pass_policy

L = RunnerLifecycle


def make_runner(**kw) -> Runner:
    return Runner(SimpleNamespace(engine=None), None, None, None, [], advisory_key=1, capability_enabled=False, **kw)


def ctx_for(r: Runner, generation=1) -> StartContext:
    ctx = StartContext(generation, asyncio.get_running_loop().create_future(), asyncio.Event())
    r._ctx, r._generation = ctx, generation
    return ctx


# --- SUP-28: three cases of the fallback and the entry-time snapshot -----------------------------------------------------------
async def test_a_valid_pass_clears_nothing_and_returns_the_interval(monkeypatch):
    r = make_runner()
    assert await r._sweeper_pass() == P.ORPHAN_SWEEP_INTERVAL_SECONDS
    assert r.sweeper_config_invalid.config_currently_valid is True and not r.sweeper_stall.stalled


@pytest.mark.parametrize("value,reason", [(None, "TYPE"), ("50", "TYPE"), (True, "TYPE"), (float("nan"), "TYPE"),
                                          (float("inf"), "TYPE"), (0, "OUT_OF_RANGE"), (-1, "OUT_OF_RANGE"), (10**400, "RELATION")],
                         ids=["none", "str", "bool", "nan", "inf", "zero", "negative", "huge"])
async def test_sup_28_case_1_an_invalid_snapshot_constant_is_sweep_config_invalid_and_the_body_does_not_run(monkeypatch, value, reason):
    ran = []

    async def body(start, policy):
        ran.append(1)
        return PassOutcome(wrapped=True)

    r = make_runner(sweep_pass=body)
    monkeypatch.setattr(P, "ORPHAN_SWEEP_BATCH_ROWS", value)
    wait = await r._sweeper_pass()
    assert ran == [] and wait == 86400.0                                       # nothing is read, the fallback wait
    snap, cfg = r.sweeper_stall, r.sweeper_config_invalid
    assert snap.last_failure_code == SWEEP_CONFIG_INVALID and snap.consecutive_failures == 1
    assert cfg.config_invalid_active and cfg.config_currently_valid is False
    assert cfg.config_invalid_reasons == (reason,) and reason in SWEEPER_CONFIG_INVALID_REASONS                    # fixed vocabulary
    assert "ORPHAN_SWEEP_BATCH_ROWS" not in " ".join(cfg.config_invalid_reasons)                                   # never a constant name
    monkeypatch.setattr(P, "ORPHAN_SWEEP_BATCH_ROWS", 50)                       # fixed: the next pass runs and clears (CI-5)
    assert await r._sweeper_pass() == P.ORPHAN_SWEEP_INTERVAL_SECONDS and ran == [1]
    assert not r.sweeper_config_invalid.config_invalid_active and not r.sweeper_stall.stalled


@pytest.mark.parametrize("name", ["ORPHAN_SWEEP_STALL_THRESHOLD", "ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP",
                                  "ORPHAN_SWEEP_LOG_THROTTLE_SECONDS", "ORPHAN_SWEEP_PASS_SECONDS", "CANDIDATE_PAGE_ROWS",
                                  "BACKGROUND_WAKEUP_DEBOUNCE_SECONDS"])
async def test_sup_28_the_stall_threshold_and_the_log_throttle_are_snapshot_constants_too(monkeypatch, name):
    r = make_runner()
    monkeypatch.setattr(P, name, float("nan"))
    await r._sweeper_pass()
    assert r.sweeper_stall.last_failure_code == SWEEP_CONFIG_INVALID


async def test_sup_28_case_2_a_valid_fallback_is_used_by_the_failure_path_and_is_not_a_snapshot_constant(monkeypatch):
    r = make_runner()
    monkeypatch.setattr(P, "ORPHAN_SWEEP_BATCH_ROWS", None)
    await r._sweeper_pass()
    assert (r.sweeper_stall.next_retry_at - r.sweeper_stall.last_failure_at).total_seconds() == 86400.0


@pytest.mark.parametrize("bad", [86400.5, 1e300, float("nan"), 0, True])
async def test_sup_28_case_3_a_corrupted_fallback_is_an_abnormal_exit_row_28_and_writes_nothing(monkeypatch, bad):
    r = make_runner()
    monkeypatch.setattr(P, "ORPHAN_SWEEP_BATCH_ROWS", None)                     # the snapshot fails AND the fallback is corrupted
    monkeypatch.setattr(P, "ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS", bad)
    with pytest.raises(SweeperFallbackInvalid):
        await r._sweeper_pass()
    assert r.sweeper_stall.consecutive_failures == 0 and not r.sweeper_config_invalid.config_invalid_active


async def test_a_corrupted_fallback_alone_does_not_matter_while_the_snapshot_is_valid(monkeypatch):
    r = make_runner()
    monkeypatch.setattr(P, "ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS", 86400.5)      # consumed only by the failure path
    assert await r._sweeper_pass() == P.ORPHAN_SWEEP_INTERVAL_SECONDS


# --- the pass body outcomes: rows 1 to 4, 13, 16 -----------------------------------------------------------------------------
async def test_failed_passes_accumulate_to_the_threshold_with_the_capped_backoff_and_a_stop_is_never_a_failure(monkeypatch):
    uid = uuid.UUID(int=9)

    async def body(start, policy):
        return PassOutcome("FAILED", code="SWEEP_QUERY_FAILED", position=uid, position_kind="CANDIDATE")

    r = make_runner(sweep_pass=body)
    waits = [await r._sweeper_pass() for _ in range(P.ORPHAN_SWEEP_STALL_THRESHOLD)]
    assert waits == [30.0, 60.0, 120.0, 240.0, 300.0]
    snap = r.sweeper_stall
    assert snap.stalled and snap.consecutive_failures == 5 and snap.first_failure_cursor == uid
    assert snap.last_failure_code == "SWEEP_QUERY_FAILED"


async def test_a_child_cancellation_is_sweep_cancelled_but_a_cancel_of_the_task_itself_is_never_recorded():
    async def child_cancelled(start, policy):
        raise asyncio.CancelledError()                                           # nobody cancelled THIS task: a child operation

    r = make_runner(sweep_pass=child_cancelled)
    await r._sweeper_pass()
    assert r.sweeper_stall.last_failure_code == "SWEEP_CANCELLED"

    started = asyncio.Event()

    async def hangs(start, policy):
        started.set()
        await asyncio.Event().wait()

    r2 = make_runner(sweep_pass=hangs)
    task = asyncio.ensure_future(r2._sweeper_pass())
    await started.wait()
    task.cancel()                                                                # row 14 / 15 / 27: the top-level task
    with pytest.raises(asyncio.CancelledError):
        await task
    assert r2.sweeper_stall.consecutive_failures == 0 and r2.sweeper_stall.last_failure_code is None


async def test_an_exception_of_the_body_is_sweep_exception_and_no_progress_accumulates():
    async def boom(start, policy):
        raise RuntimeError("secret detail must never be stored")

    r = make_runner(sweep_pass=boom)
    await r._sweeper_pass()
    assert r.sweeper_stall.last_failure_code == "SWEEP_EXCEPTION"
    assert "secret" not in repr(r.sweeper_stall)

    async def idle(start, policy):
        return PassOutcome("NO_PROGRESS", position=uuid.UUID(int=1), position_kind="CANDIDATE")

    r2 = make_runner(sweep_pass=idle)
    await r2._sweeper_pass()
    assert r2.sweeper_stall.last_failure_code == "SWEEP_NO_PROGRESS"


async def test_without_the_phase_5_seam_a_pass_sweeps_nothing_and_wraps_around():
    r = make_runner()
    r._committed_cursor = uuid.UUID(int=4)
    await r._sweeper_pass()
    assert r._committed_cursor is None


# --- RV-3: every combination has exactly one answer ---------------------------------------------------------------------------
@pytest.mark.parametrize("lifecycle", list(L))
@pytest.mark.parametrize("same_generation", [True, False])
@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("task_state", ["live", "none", "done_unprocessed"])
@pytest.mark.parametrize("pending", [True, False])
async def test_rv_3_result_precedence(lifecycle, same_generation, valid, active, task_state, pending):
    r = make_runner()
    r._generation, r._state = 3, lifecycle
    r._stall.config_invalid_active, r._stall.wake_pending = active, pending
    unit = r._supervisor.units["sweeper"]
    if task_state == "live":
        unit.task = asyncio.ensure_future(asyncio.Event().wait())
    elif task_state == "done_unprocessed":
        unit.task = asyncio.ensure_future(asyncio.sleep(0))
        await asyncio.sleep(0.01)
    generation = 3 if same_generation else 2
    policy = (lambda: None) if valid else (lambda: (_ for _ in ()).throw(ValueError("bad")))
    got = r._recheck_sweeper_configuration(generation, policy)
    if not same_generation:
        want = ConfigWakeResult.STALE_GENERATION
    elif lifecycle is not L.RUNNING:
        want = ConfigWakeResult.NOT_RUNNING
    elif not valid:
        want = ConfigWakeResult.STILL_INVALID
    elif task_state != "live":
        want = ConfigWakeResult.NO_SWEEPER_TASK
    elif not active:
        want = ConfigWakeResult.NO_INVALID_STATE
    elif pending:
        want = ConfigWakeResult.ALREADY_PENDING
    else:
        want = ConfigWakeResult.WAKE_SCHEDULED
    assert got is want
    if got is ConfigWakeResult.WAKE_SCHEDULED:
        assert r._stall.wake_pending and r._sweeper_wake.is_set()
    if got is ConfigWakeResult.NO_SWEEPER_TASK:
        assert r._stall.wake_pending is False                                    # never set without a task
    if unit.task is not None:
        unit.task.cancel()


async def test_rv_3_a_second_request_is_coalesced_and_the_wake_ends_the_long_wait_once():
    slept = []

    async def fast(seconds):
        slept.append(seconds)

    r = make_runner(supervisor_sleep=fast)
    r._generation, r._state = 1, L.RUNNING
    r._stall.config_invalid_active = True
    r._supervisor.units["sweeper"].task = asyncio.ensure_future(asyncio.Event().wait())
    assert r._recheck_sweeper_configuration(1) is ConfigWakeResult.WAKE_SCHEDULED
    assert r._recheck_sweeper_configuration(1) is ConfigWakeResult.ALREADY_PENDING
    await asyncio.wait_for(r._sweeper_sleep(86400.0), 5)                         # the 24 hour wait ends at once
    assert r._stall.wake_pending is False and not r._sweeper_wake.is_set()
    assert slept == [P.BACKGROUND_WAKEUP_DEBOUNCE_SECONDS]                       # debounced once
    r._supervisor.units["sweeper"].task.cancel()


# --- next_recheck_at: the instant the sweeper SCHEDULED for its next evaluation (design 14.2) -----------------------------------
class StepClock:
    def __init__(self):
        self.now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def recheck_of(r: Runner):
    return r._stall.config_snapshot().next_recheck_at


async def test_next_recheck_at_is_the_scheduled_end_of_the_pass_wait_and_null_otherwise():
    clock = StepClock()
    r = make_runner(clock=clock)
    r._generation, r._state = 1, L.RUNNING
    assert recheck_of(r) is None                                                  # nothing scheduled yet: unknown, never a guess
    waiting = asyncio.ensure_future(r._sweeper_sleep(30.0))
    await asyncio.sleep(0)
    assert recheck_of(r) == clock.now + timedelta(seconds=30)                     # the end of the pass wait
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    assert recheck_of(r) is None                                                  # a stop (cancel) leaves no scheduled recheck


async def test_next_recheck_at_follows_the_debounce_while_a_wake_up_is_pending_and_is_cleared_after_it():
    clock, release = StepClock(), asyncio.Event()

    async def debounce_sleep(seconds):
        await release.wait()

    r = make_runner(clock=clock, supervisor_sleep=debounce_sleep)
    r._generation, r._state = 1, L.RUNNING
    r._stall.config_invalid_active = True
    r._supervisor.units["sweeper"].task = asyncio.ensure_future(asyncio.Event().wait())
    waiting = asyncio.ensure_future(r._sweeper_sleep(86400.0))
    await asyncio.sleep(0)
    assert recheck_of(r) == clock.now + timedelta(seconds=86400)
    assert r._recheck_sweeper_configuration(1) is ConfigWakeResult.WAKE_SCHEDULED
    await asyncio.sleep(0.05)
    assert recheck_of(r) == clock.now + timedelta(seconds=P.BACKGROUND_WAKEUP_DEBOUNCE_SECONDS)   # the end of the debounce
    release.set()
    await asyncio.wait_for(waiting, 5)
    assert recheck_of(r) is None
    r._supervisor.units["sweeper"].task.cancel()


async def test_next_recheck_at_is_not_next_retry_at_and_never_moves_the_revision():
    clock = StepClock()
    r = make_runner(clock=clock)
    r._generation, r._state = 1, L.RUNNING
    r._stall.record_failed_pass(pass_start=None, position=None, kind=None, code="SWEEP_EXCEPTION", now=clock.now,
                                retry_delay_seconds=90.0)
    retry_at = r._stall.next_retry_at
    assert retry_at == clock.now + timedelta(seconds=90)
    before = r.background_revision
    waiting = asyncio.ensure_future(r._sweeper_sleep(30.0))
    await asyncio.sleep(0)
    snap = r._stall.config_snapshot()
    assert snap.next_recheck_at == clock.now + timedelta(seconds=30) and snap.next_recheck_at != retry_at
    assert snap.next_retry_at is None                                              # the audit value of the config state: not set here
    assert r._stall.snapshot().next_retry_at == retry_at                           # the ordinary retry schedule is untouched
    assert r.background_revision == before                                         # a scheduled time is not a public state change
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)


async def test_next_recheck_at_is_null_while_a_pass_runs_after_a_reset_and_when_the_sweeper_task_ends():
    clock = StepClock()
    r = make_runner(clock=clock)
    ctx = ctx_for(r)
    r._generation, r._state = 1, L.RUNNING
    ctx.mutation_done.set()
    passes = asyncio.Event()

    async def pass_body(start, policy):
        passes.set()
        assert recheck_of(r) is None                                               # no wait is scheduled while a pass runs
        return PassOutcome("OK", committed=None, advanced=False, wrapped=True)

    r._sweep_pass_fn = pass_body
    task = asyncio.ensure_future(r._sweeper_body(lambda: None))
    await asyncio.wait_for(passes.wait(), 5)
    for _ in range(20):
        await asyncio.sleep(0)
    assert recheck_of(r) == clock.now + timedelta(seconds=build_sweeper_pass_policy().interval_seconds)   # the pass wait
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert recheck_of(r) is None                                                   # the task ended: nothing is scheduled
    r._stall.next_recheck_at = clock.now
    r._stall.reset()
    assert recheck_of(r) is None


# --- startup_mutation_done: the common set condition (D35-3) -------------------------------------------------------------------
@pytest.mark.parametrize("generation_ok,lifecycle,aborting,expected", [
    (True, L.STARTING, False, True),         # the only case that sets
    (True, L.STARTING, True, False),         # (iv-b): the marker alone
    (True, L.RUNNING, False, False),
    (True, L.STOPPING, False, False),
    (True, L.STOP_INCOMPLETE, False, False),
    (True, L.STOPPED, False, False),
    (False, L.STARTING, False, False),       # (iv-a): a holder of an older generation
])
async def test_the_common_set_condition_for_both_setters(generation_ok, lifecycle, aborting, expected):
    r = make_runner()
    ctx = ctx_for(r, 4)
    r._generation = 4 if generation_ok else 5
    r._state, ctx.aborting = lifecycle, aborting
    assert r._set_mutation_done(ctx) is expected and ctx.mutation_done.is_set() is expected


async def test_an_already_set_event_stays_set_and_is_never_reset_by_a_later_failure_or_the_marker():
    r = make_runner()
    ctx = ctx_for(r, 1)
    r._state = L.STARTING
    assert r._set_mutation_done(ctx) is True
    ctx.aborting = True                                                          # (v) / (ii): a later failure
    r._state = L.STOPPED
    assert r._set_mutation_done(ctx) is True and ctx.mutation_done.is_set()


# --- the startup wait of _recover_rest: both paths (iv-b) ---------------------------------------------------------------------
async def test_the_startup_wait_expires_at_the_bounded_wait_when_nothing_cancels_it(monkeypatch):
    monkeypatch.setattr(P, "ORPHAN_SWEEP_STARTUP_WAIT_SECONDS", 0.3)
    r = make_runner()
    ctx_for(r, 1)
    r._state = L.STARTING
    sweeper = r._supervisor.units["sweeper"]
    sweeper.task = asyncio.ensure_future(asyncio.Event().wait())                 # alive, and it never finishes a pass
    started = asyncio.get_running_loop().time()
    await r._await_first_sweep(1)
    elapsed = asyncio.get_running_loop().time() - started
    assert 0.25 <= elapsed < 3.0                                                 # bounded, never unbounded
    sweeper.task.cancel()


async def test_the_startup_wait_ends_at_the_cancellation_of_the_settlement(monkeypatch):
    monkeypatch.setattr(P, "ORPHAN_SWEEP_STARTUP_WAIT_SECONDS", 30.0)
    r = make_runner()
    ctx = ctx_for(r, 1)
    r._state, ctx.aborting = L.STARTING, True                                    # (iv-b)
    sweeper = r._supervisor.units["sweeper"]
    sweeper.task = asyncio.ensure_future(asyncio.Event().wait())
    waiter = asyncio.ensure_future(r._await_first_sweep(1))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, 2)
    sweeper.task.cancel()


@pytest.mark.parametrize("how", ["lifecycle", "generation", "no_sweeper", "degraded", "first_pass"])
async def test_the_startup_wait_ends_early_when_there_is_nothing_left_to_wait_for(monkeypatch, how):
    monkeypatch.setattr(P, "ORPHAN_SWEEP_STARTUP_WAIT_SECONDS", 30.0)
    r = make_runner()
    ctx = ctx_for(r, 1)
    r._state = L.STARTING
    sweeper = r._supervisor.units["sweeper"]
    sweeper.task = asyncio.ensure_future(asyncio.Event().wait())
    waiter = asyncio.ensure_future(r._await_first_sweep(1))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    if how == "lifecycle":
        r._state = L.STOPPING
    elif how == "generation":
        r._generation = 2
    elif how == "no_sweeper":
        sweeper.task.cancel()
        await asyncio.sleep(0.01)
    elif how == "degraded":
        sweeper.state = UnitState.DEGRADED
    else:
        ctx.first_pass.set()
    await asyncio.wait_for(waiter, 3)
    sweeper.task.cancel()


# --- dispatch predicate (R31-2) and the cancel path ----------------------------------------------------------------------------
async def test_only_the_supervisor_flag_blocks_dispatch():
    r = make_runner()
    assert r._dispatch_blocked() is False
    for unit in ("sweeper", "risk-worker", "capability"):
        r._supervisor.units[unit].state = UnitState.DEGRADED
        assert r._dispatch_blocked() is False                                    # a degraded component never blocks
    r._supervisor.supervisor_degraded = True
    assert r._dispatch_blocked() is True and r.supervisor_degraded is True


async def test_cancel_cli_also_cancels_the_executor_by_run_id():
    calls = []
    executor = SimpleNamespace(cancel=lambda key: calls.append(key))
    provider = SimpleNamespace(cancel=lambda key: calls.append(("provider", key)))
    r = Runner(SimpleNamespace(engine=None), None, None, None, [provider], advisory_key=1, executor=executor,
               capability_enabled=False)
    run_id = uuid.uuid4()
    r._cancel_cli(run_id)
    assert calls == [("provider", str(run_id)), str(run_id)]


async def test_the_checkpoint_key_of_the_unresolved_tracked_row_is_read_from_its_detail():
    def row(detail):
        return TrackedRow(uuid.uuid4(), uuid.uuid4(), None, None, None, detail)

    class Repo:
        def __init__(self, rows):
            self.rows = rows

        async def unresolved_for_task(self, task_id):
            return self.rows

    good = row(json.dumps({"tracked": True, "checkpoint_step_key": "STEP_TEST"}))
    assert await Runner._tracked_checkpoint_key(Repo([good]), uuid.uuid4()) == "STEP_TEST"
    assert await Runner._tracked_checkpoint_key(Repo([row("not json"), good]), uuid.uuid4()) == "STEP_TEST"
    assert await Runner._tracked_checkpoint_key(Repo([row(json.dumps({"tracked": True}))]), uuid.uuid4()) is None
    assert await Runner._tracked_checkpoint_key(Repo([row(None), row(json.dumps([1]))]), uuid.uuid4()) is None


# --- the runtime status ---------------------------------------------------------------------------------------------------------
def test_runtime_snapshot_keeps_its_three_argument_form_and_adds_the_supervisor_blocker():
    base = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False)
    assert base.ready and base.blockers == () and base.background_degraded is False
    assert base.as_dict()["runner"]["background_degraded"] is False               # the public shape (phase 6, 14.2)
    degraded = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False, supervisor_degraded=True)
    assert degraded.ready is False and degraded.blockers == (RunnerBlocker.SUPERVISOR_DEGRADED.value,)
    assert degraded.runner_active is True and degraded.background_degraded is True
    only_component = runtime_snapshot(L.RUNNING, StartupOutcome.STARTED, False, background_degraded=True)
    assert only_component.ready and only_component.blockers == ()                 # ready and blockers unchanged by a component


async def test_snapshot_of_reads_the_runner_supervisor_status():
    r = make_runner()
    r._state = L.RUNNING
    assert snapshot_of(r, StartupOutcome.STARTED).ready is True
    r._supervisor.supervisor_degraded = True
    snap = snapshot_of(r, StartupOutcome.STARTED)
    assert snap.supervisor_degraded and snap.background_degraded and not snap.ready
    assert any(u.unit_id == "supervisor" and u.display_state == "DEGRADED" for u in snap.components)
