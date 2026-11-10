"""Bounded request recovery: the lifecycle lock is not held during recovery DB I/O, the operation wait and the cancel
settlement are bounded, the DB outcome and the connection (resource) outcome are tracked separately, an UNCERTAIN commit
is reconciled read-only, and every recovery write is the token-fenced conditional UPDATE.

Control is by Events and wrapped private methods / a session subclass; there is no production hook. Timeouts are small
but nothing waits for a sleep to elapse: Events decide where each operation is. Operations that ignore a cancellation
are always released in `finally` and reaped."""
import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.runner as runner_module
from app.models.tables import ExecutionRequest
from app.repositories.approval_repository import ExecutionRequestRepository
from app.services.recovery_transaction import DbOutcome, RecoveryEvidence, ResourceOutcome
from app.services.runner import (
    CancelReason, RecoveryError, RecoveryKind, RecoveryTimeout, RecoveryUnsettled, Runner, RunnerLifecycle,
    RunnerStopIncomplete)
from tests.harness import Harness

KEY = 987_654_323
L = RunnerLifecycle
SHUTDOWN_WAIT = 0.3
# Two timeout profiles. NORMAL: no test using it waits for the deadline, so the statement limit is generous enough for
# every ordinary SELECT / UPDATE / COMMIT even on a slow machine. DEADLINE: for the tests that wait for the operation
# deadline itself (the operation must be short, the statement limit stays below it but is still far above a normal SQL).
# Only the test of the server-side statement timeout uses a short statement limit, and only for its own statement.
STATEMENT, OPERATION = 5.0, 8.0
DEADLINE_STATEMENT, DEADLINE_OPERATION = 0.9, 1.0
BOUND = 10


# --- helpers -----------------------------------------------------------------------------------------------------------
def make_runner(h: Harness, deadline=False, poll=3600.0, **kw) -> Runner:
    kw.setdefault("recovery_statement_timeout_seconds", DEADLINE_STATEMENT if deadline else STATEMENT)
    kw.setdefault("recovery_timeout_seconds", DEADLINE_OPERATION if deadline else OPERATION)
    kw.setdefault("recovery_shutdown_wait_seconds", SHUTDOWN_WAIT)
    return Runner(h.db, h.leases, h.workflow, h.gates, [h.claude, h.codex], advisory_key=KEY, poll_seconds=poll,
                  claim_timeout_seconds=300, shutdown_grace_seconds=0.1, **kw)


async def claimed_request(h: Harness, *, stale=False):
    task = await h.new_task()
    await h.tasks.request_execution(task.id)
    async with h.db.transaction() as s:
        await ExecutionRequestRepository(s).claim_next("dead-runner")
        if stale:
            await s.execute(text("update execution_requests set claimed_at = now() - interval '1 hour'"))
    return task


async def rows(h: Harness):
    async with h.db.transaction() as s:
        found = (await s.execute(select(ExecutionRequest).order_by(ExecutionRequest.created_at))).scalars().all()
        return [(r.id, r.status, r.claim_token, r.note) for r in found]


async def lock_free(db) -> bool:
    conn = await db.engine.connect()
    try:
        got = (await conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": KEY})).scalar()
        if got:
            assert (await conn.execute(text("select pg_advisory_unlock(:k)"), {"k": KEY})).scalar() is True
        return bool(got)
    finally:
        await conn.close()


def checked_out(db) -> int:
    return db.engine.pool.checkedout()


async def wait(event: asyncio.Event, what: str = "an event"):
    """Bounded wait; a timeout names what never happened instead of a bare TimeoutError."""
    try:
        await asyncio.wait_for(event.wait(), BOUND)
    except TimeoutError:
        raise AssertionError(f"timed out after {BOUND}s waiting for {what}") from None


class Knobs:
    """Behaviour switches of the session used by the recovery transaction (a test-only subclass, see `use_flaky`)."""

    def __init__(self):
        self.commit = "ok"          # ok | commit_then_raise (committed, reply lost) | raise_before
        self.close = "ok"           # ok | error (once) | error_always | stall (ignores cancellation until `release`)
        self.invalidate = "ok"      # ok | error
        self.connect_error = False
        self.release = asyncio.Event()
        self.closing = asyncio.Event()
        self.close_calls = 0


def use_flaky(r: Runner, k: Knobs) -> None:
    class Session(AsyncSession):
        async def connection(self, *a, **kw):
            if k.connect_error:
                raise ConnectionError("database unreachable")
            return await super().connection(*a, **kw)

        async def commit(self):
            if k.commit == "raise_before":
                raise ConnectionError("lost before COMMIT reached the server")
            await super().commit()
            if k.commit == "commit_then_raise":
                raise ConnectionError("COMMIT reply lost")

        async def close(self):
            k.close_calls += 1
            if k.close == "stall":
                k.closing.set()
                while not k.release.is_set():
                    try:
                        await k.release.wait()
                    except asyncio.CancelledError:
                        continue                                     # a close that cannot be interrupted
                return await super().close()
            if k.close in ("error", "error_always"):
                if k.close == "error":
                    k.close = "ok"
                raise ConnectionError("close failed")
            return await super().close()

        async def invalidate(self):
            if k.invalidate == "error":
                raise ConnectionError("invalidate failed")
            return await super().invalidate()

    r._recovery_sessions = async_sessionmaker(r.db.engine, class_=Session, expire_on_commit=False)


def stall_body(r: Runner, monkeypatch, *, ignore_cancel=False, only_periodic=False):
    """The recovery body stops at its entry (inside the transaction, connection checked out) until `release` is set."""
    entered, release = asyncio.Event(), asyncio.Event()
    real = r._recovery_body

    async def body(startup, session, evidence):
        if only_periodic and startup:
            return await real(startup, session, evidence)
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                if not ignore_cancel:
                    raise
        return await real(startup, session, evidence)

    monkeypatch.setattr(r, "_recovery_body", body)
    return entered, release


def count_rest(r: Runner, monkeypatch) -> list:
    calls, real = [], r._recover_rest

    async def rest(summary):
        calls.append(summary)
        return await real(summary)

    monkeypatch.setattr(r, "_recover_rest", rest)
    return calls


def watch_loop_reap(r: Runner, monkeypatch) -> dict:
    """Install BEFORE `r.start()`. Observes the production reaping and changes nothing: the wrapper calls the original
    `_reap_recovery_locked` exactly once, returns what it returns, writes no Runner field and awaits nothing (production
    calls it under the lifecycle lock, so the reads are consistent). It remembers the first PERIODIC (non-startup)
    recovery operation that this call actually reaped (found it finished and removed it) and sets `reaped` once."""
    watch = {"reaped": asyncio.Event(), "ctx": None, "before": None, "after": None}
    original = r._reap_recovery_locked

    def observed():
        ctx = r._recovery
        result = original()
        if (ctx is not None and not ctx.startup and ctx.task.done() and r._recovery is not ctx
                and not watch["reaped"].is_set()):
            watch["ctx"], watch["before"], watch["after"] = ctx, ctx, r._recovery
            watch["reaped"].set()
        return result

    monkeypatch.setattr(r, "_reap_recovery_locked", observed)
    return watch


async def loop_idle(watch: dict, r: Runner):
    """After start(): wait until the PRODUCTION loop reaped its first periodic recovery, so that a manual `recover()` is
    the only operation (otherwise it would legitimately be SKIPPED). The test does not reap anything itself."""
    await wait(watch["reaped"], "the loop's first periodic recovery to be reaped")
    assert watch["after"] is None and r._recovery is not watch["ctx"], "the production reap left the operation registered"


async def settle(r: Runner):
    """Final tidy-up of an unstarted Runner: whatever is left of its recovery is settled and reaped."""
    try:
        await asyncio.wait_for(r._settle_recovery_for_stop(), BOUND)
    except RecoveryError:
        pass


async def tidy(r: Runner, k: "Knobs | None" = None):
    """`finally` helper: knobs back to normal, gates open, whatever is left of the recovery settled and reaped."""
    if k is not None:
        k.commit, k.close, k.invalidate, k.connect_error = "ok", "ok", "ok", False
        k.release.set()
    await settle(r)


def record_stages(r: Runner, monkeypatch, before=None) -> dict:
    """Observation through private wrappers only (no production hook); each original is called exactly once.
      stages         the kind of every registered operation, in order
      registrations  (kind, cleanup evidence, commit evidence) as `_register_locked` found them (it runs under the
                     lifecycle lock, so this is how the evidence looked when the next stage was chosen)
      bodies         how often the MUTATION body ran
    `before(kind, index)` (async) runs inside the operation task right before the original: tests use it to switch
    knobs between stages."""
    rec = {"stages": [], "registrations": [], "bodies": 0}
    real_operation, real_register, real_body = r._recovery_operation, r._register_locked, r._recovery_body

    def register(startup):
        found = (r._recovery_cleanup_unsettled, r._recovery_unsettled)
        ctx = real_register(startup)
        rec["registrations"].append((ctx.kind, *found))
        return ctx

    async def operation(ctx):
        index = len(rec["stages"])
        rec["stages"].append(ctx.kind)
        if before is not None:
            await before(ctx.kind, index)
        return await real_operation(ctx)

    async def body(startup, session, evidence):
        rec["bodies"] += 1
        return await real_body(startup, session, evidence)

    monkeypatch.setattr(r, "_register_locked", register)
    monkeypatch.setattr(r, "_recovery_operation", operation)
    monkeypatch.setattr(r, "_recovery_body", body)
    return rec


async def stop_quietly(r: Runner):
    try:
        await asyncio.wait_for(r.stop(), BOUND)
    except (RunnerStopIncomplete, RecoveryError):
        pass


# --- 1/2: the lifecycle lock is free during recovery DB I/O ------------------------------------------------------------
async def test_stop_gets_the_lifecycle_lock_and_stopping_while_recovery_is_inside_its_transaction(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    entered, release = stall_body(r, monkeypatch, only_periodic=True)
    # `_settle_stop` only starts after stop() took the lifecycle lock and switched to STOPPING, so its entry is the
    # direct evidence that the lock was available while the recovery was inside its transaction.
    settle_entered, at_entry = asyncio.Event(), {}
    real_settle = r._settle_stop

    async def settle_probe():
        at_entry.update(lifecycle=r.lifecycle, recovery_live=r._recovery is not None and not r._recovery.task.done(),
                        lock_held=r._lifecycle_lock.locked())
        settle_entered.set()
        return await real_settle()                                           # the original, exactly once

    monkeypatch.setattr(r, "_settle_stop", settle_probe)
    await r.start()
    stopping = None
    try:
        await wait(entered, "the periodic recovery to be inside its transaction")
        base = checked_out(db)
        stopping = asyncio.create_task(r.stop())
        await wait(settle_entered, "stop() to reach its settle task (is the lifecycle lock held by the recovery?)")
        assert at_entry == {"lifecycle": L.STOPPING, "recovery_live": True, "lock_held": False}, at_entry
        await asyncio.wait_for(stopping, BOUND)                              # stop() as a whole finished
        assert r.lifecycle is L.STOPPED and r._recovery_clean(), r.lifecycle
        assert checked_out(db) < base                                    # the recovery connection went back (rollback)
        assert await lock_free(db)
    finally:
        release.set()
        if stopping is None or not stopping.done():
            await stop_quietly(r)


async def test_deadline_is_a_recovery_timeout_even_though_the_cancelled_operation_rolled_back(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    before = await rows(h)
    r = make_runner(h, deadline=True)
    r._state = L.RUNNING                                                   # allow a periodic call without a loop
    _, release = stall_body(r, monkeypatch)
    base = checked_out(db)
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(RecoveryTimeout) as info:
            await r.recover()
        elapsed = asyncio.get_running_loop().time() - started
        assert not info.value.pending and info.value.result.db is DbOutcome.ROLLED_BACK
        assert info.value.result.resource is ResourceOutcome.RELEASED
        assert DEADLINE_OPERATION <= elapsed < DEADLINE_OPERATION + SHUTDOWN_WAIT + 3   # operation wait + bounded settlement
        assert r._recovery is None and r._recovery_clean()
        assert await rows(h) == before and checked_out(db) == base         # nothing changed, connection returned
    finally:
        release.set()
        r._state = L.STOPPED
        await settle(r)


async def test_a_recovery_that_ignores_cancellation_stays_live_and_is_pending(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h, deadline=True)
    r._state = L.RUNNING
    entered, release = stall_body(r, monkeypatch, ignore_cancel=True)
    rest = count_rest(r, monkeypatch)
    try:
        with pytest.raises(RecoveryTimeout) as info:
            await r.recover()
        assert info.value.pending and info.value.result is None
        live = r._recovery
        assert live is not None and not live.task.done() and live.reason is CancelReason.OPERATION_DEADLINE
        assert (await r.recover())["outcome"] == "SKIPPED"                 # the same task still owns the gate
        with pytest.raises(RuntimeError):
            await r._recover(True, checked=False)                          # startup refuses a registered operation
        assert r._recovery is live and live.cancel_calls == 1              # never cancelled twice or replaced
        release.set()
        r._state = L.STOPPED
        await settle(r)
        assert r._recovery_clean() and live.result.db is DbOutcome.COMMITTED
        assert live.result.resource is ResourceOutcome.RELEASED
        assert rest == []                                                  # the deadline suppressed the follow-up
    finally:
        release.set()
        r._state = L.STOPPED
        await settle(r)


# --- caller cancellation / reaping order ---------------------------------------------------------------------------------
async def test_caller_cancellation_keeps_the_original_exception_and_loses_no_child_task(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    before = await rows(h)
    r = make_runner(h)
    entered, release = stall_body(r, monkeypatch)
    seen = []

    async def caller():
        try:
            await r._recover(False, checked=False)
        except asyncio.CancelledError as exc:
            seen.append(exc)
            raise

    task = asyncio.create_task(caller())
    try:
        await wait(entered)
        child = r._recovery.task
        task.cancel("caller marker")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
        assert seen and seen[0].args == ("caller marker",)                 # the original CancelledError object
        assert child.done() and r._recovery is None and r._recovery_clean()   # child settled and reaped, not lost
        assert await rows(h) == before
    finally:
        release.set()
        await settle(r)


async def test_cancellation_while_the_child_ignores_it_keeps_the_same_task_and_still_reraises(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    entered, release = stall_body(r, monkeypatch, ignore_cancel=True)
    task = asyncio.create_task(r._recover(False, checked=False))
    try:
        await wait(entered)
        child = r._recovery.task
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
        assert r._recovery is not None and r._recovery.task is child and not child.done()   # kept (PENDING)
        release.set()
        await settle(r)
        assert child.done() and r._recovery_clean()
    finally:
        release.set()
        await settle(r)


async def test_a_finished_operation_is_reaped_before_a_new_recovery_starts(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h, deadline=True)
    r._state = L.RUNNING
    entered, release = stall_body(r, monkeypatch, ignore_cancel=True)
    try:
        with pytest.raises(RecoveryTimeout):
            await r.recover()
        first = r._recovery
        release.set()
        await asyncio.wait_for(asyncio.wait({first.task}), BOUND)         # finished, but nobody reaped it yet
        assert r._recovery is first and first.result is None
        monkeypatch.undo()
        out = await r.recover()                                            # registers: reaps `first` first, then runs anew
        assert out["outcome"] == "COMMITTED" and first.result is not None and first.result.db is DbOutcome.COMMITTED
        assert r._recovery is None
    finally:
        release.set()
        r._state = L.STOPPED
        await settle(r)


async def test_the_done_callback_only_consumes_and_logs_and_changes_no_shared_field(db):
    r = Runner(db, None, None, None, [], advisory_key=KEY)
    marks = (r._recovery, r._recovery_unsettled, r._recovery_cleanup_unsettled, r._generation, r._state)

    async def boom():
        raise ValueError("unexpected")

    failed = asyncio.ensure_future(boom())
    cancelled = asyncio.ensure_future(asyncio.sleep(3600))
    cancelled.cancel()
    await asyncio.wait({failed, cancelled})
    r._recovery_done.clear()
    r._on_recovery_done(cancelled)                                         # must not raise CancelledError
    r._on_recovery_done(failed)                                            # consumes the ValueError
    assert r._recovery_done.is_set() and not r._wake.is_set()              # never a loop wake-up (it would spin)
    assert marks == (r._recovery, r._recovery_unsettled, r._recovery_cleanup_unsettled, r._generation, r._state)


# --- token fencing ---------------------------------------------------------------------------------------------------
async def test_a_request_that_changed_after_it_was_read_is_fenced_and_not_overwritten(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    new_owner = uuid.uuid4()
    real = ExecutionRequestRepository.requeue

    async def requeue_after_new_claim(self, request_id, note, **kw):
        async with h.db.transaction() as s:                                # another owner re-claims it meanwhile
            await s.execute(text("update execution_requests set claim_token = :t, runner_id = 'new-owner' where id = :i"),
                            {"t": new_owner, "i": request_id})
        return await real(self, request_id, note, **kw)

    monkeypatch.setattr(ExecutionRequestRepository, "requeue", requeue_after_new_claim)
    out = await r._recover(False, checked=False)
    assert (out["requeued"], out["closed"], out["fenced"]) == (0, 0, 1)    # only applied UPDATEs are counted
    [(_, status, token, note)] = await rows(h)
    assert status == "CLAIMED" and token == new_owner and note != "requeued_after_stale_claim"   # not overwritten
    assert r._recovery_clean()


# --- DB outcome vs resource outcome ------------------------------------------------------------------------------------
async def test_commit_then_close_error_is_released_by_invalidate_and_the_db_outcome_stays_committed(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    k.close = "error"
    use_flaky(r, k)
    rest = count_rest(r, monkeypatch)
    base = checked_out(db)
    try:
        out = await r._recover(False, checked=False)
        assert out["outcome"] == "COMMITTED" and out["requeued"] == 1 and len(rest) == 1
        assert r._recovery_clean() and checked_out(db) == base             # released through invalidate: no leak
        assert [row[1] for row in await rows(h)] == ["PENDING"]
    finally:
        await tidy(r, k)


async def test_commit_then_close_and_invalidate_both_unconfirmed_keeps_ownership_and_skips_the_follow_up(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    k.close, k.invalidate = "error_always", "error"
    use_flaky(r, k)
    rest = count_rest(r, monkeypatch)
    base = checked_out(db)
    try:
        with pytest.raises(RecoveryUnsettled) as info:
            await r._recover(False, checked=False)
        assert info.value.result.db is DbOutcome.COMMITTED and info.value.result.resource is ResourceOutcome.FAILED
        assert rest == [] and r._recovery_cleanup_unsettled is not None and not r._recovery_clean()
        assert r._recovery_cleanup_unsettled.session is not None          # the ownership needed to release it again
        assert [row[1] for row in await rows(h)] == ["PENDING"]            # the commit stands
        k.close = k.invalidate = "ok"                                      # a later settlement releases the same session
        await asyncio.wait_for(r._settle_recovery_for_stop(), BOUND)
        assert r._recovery_clean() and checked_out(db) == base
    finally:
        await tidy(r, k)


async def test_commit_done_close_stalled_is_pending_keeps_the_advisory_lock_and_stop_incomplete_until_released(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h, deadline=True)
    watch = watch_loop_reap(r, monkeypatch)
    k = Knobs()
    use_flaky(r, k)
    try:
        await r.start()
        await loop_idle(watch, r)
        await claimed_request(h, stale=True)
        k.close = "stall"
        with pytest.raises(RecoveryTimeout) as info:
            await r.recover()                                              # commit lands, close hangs, deadline passes
        assert info.value.pending
        ctx = r._recovery
        assert ctx.evidence.db_outcome is DbOutcome.COMMITTED and not ctx.evidence.conn_released
        with pytest.raises(RunnerStopIncomplete) as stopped:
            await asyncio.wait_for(r.stop(), BOUND)
        assert "unsettled" in str(stopped.value) and r.lifecycle is L.STOP_INCOMPLETE
        assert r._lock_conn is not None and not await lock_free(db)        # singleton ownership preserved
        assert r._recovery is ctx and ctx.cancel_calls == 1                # the same task, cancelled once
        k.release.set()                                                    # the close can finish now
        await asyncio.wait_for(r.stop(), BOUND)
        assert r.lifecycle is L.STOPPED and r._recovery_clean() and await lock_free(db)
        assert ctx.result.db is DbOutcome.COMMITTED and ctx.result.resource is ResourceOutcome.RELEASED
    finally:
        k.release.set()
        await stop_quietly(r)


async def test_shutdown_rollback_with_a_stalled_close_is_not_a_clean_stop(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    entered, release = stall_body(r, monkeypatch, only_periodic=True)
    k = Knobs()
    use_flaky(r, k)
    await r.start()
    await wait(entered)
    k.close = "stall"                                                      # only the periodic recovery's close hangs
    try:
        with pytest.raises(RunnerStopIncomplete):
            await asyncio.wait_for(r.stop(), BOUND)                        # rollback done, close never returns
        ctx = r._recovery
        assert ctx is not None and ctx.evidence.db_outcome is DbOutcome.ROLLED_BACK
        assert r.lifecycle is L.STOP_INCOMPLETE and r._lock_conn is not None and not await lock_free(db)
        k.release.set()
        await asyncio.wait_for(r.stop(), BOUND)
        assert r.lifecycle is L.STOPPED and await lock_free(db)
    finally:
        k.release.set()
        release.set()
        await stop_quietly(r)


async def test_shutdown_cancellation_with_a_confirmed_rollback_settles_normally(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    entered, release = stall_body(r, monkeypatch, only_periodic=True)
    await r.start()
    await wait(entered)
    await claimed_request(h, stale=True)                                   # appears after the startup recovery
    before = await rows(h)
    base = checked_out(db)
    try:
        await asyncio.wait_for(r.stop(), BOUND)
        assert r.lifecycle is L.STOPPED and r._recovery_clean() and await rows(h) == before
        assert checked_out(db) < base
    finally:
        release.set()
        await stop_quietly(r)


# --- startup ---------------------------------------------------------------------------------------------------------
async def test_a_pending_startup_recovery_never_reaches_running_and_keeps_the_lock_until_stop_settles_it(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h)
    r = make_runner(h, deadline=True)
    entered, release = stall_body(r, monkeypatch, ignore_cancel=True)
    try:
        with pytest.raises(RecoveryTimeout) as info:
            await asyncio.wait_for(r.start(), BOUND)
        assert info.value.pending
        assert r.lifecycle is L.STOP_INCOMPLETE and r._loop_task is None   # never RUNNING
        assert r._lock_conn is not None and not await lock_free(db)        # the advisory lock is kept
        live = r._recovery
        assert live is not None and live.kind is RecoveryKind.MUTATION
        release.set()
        await asyncio.wait_for(r.stop(), BOUND)                            # the same task is settled by a later stop
        assert r.lifecycle is L.STOPPED and r._recovery_clean() and await lock_free(db)
        assert live.result.db is DbOutcome.COMMITTED
    finally:
        release.set()
        await stop_quietly(r)


async def test_an_uncertain_startup_recovery_refuses_to_run_and_a_retry_start_then_succeeds(db, tmp_path):
    h = Harness(db, tmp_path)
    await claimed_request(h)
    r = make_runner(h)
    k = Knobs()
    k.commit = "commit_then_raise"
    use_flaky(r, k)
    try:
        with pytest.raises(RecoveryUnsettled):
            await asyncio.wait_for(r.start(), BOUND)
        assert r.lifecycle is not L.RUNNING and r._loop_task is None
        # the failed start already reconciled the (committed) result, so the lock was released and start can be retried
        assert r.lifecycle is L.STOPPED and r._recovery_clean() and await lock_free(db)
        k.commit = "ok"
        await asyncio.wait_for(r.start(), BOUND)
        assert r.lifecycle is L.RUNNING
    finally:
        k.commit = "ok"
        await stop_quietly(r)                                              # also covers a start that failed half-way
        await tidy(r, k)


# --- UNCERTAIN commit and its read-only reconciliation -----------------------------------------------------------------
async def test_an_uncertain_commit_makes_no_follow_up_and_a_later_recover_reconciles_then_converges(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    k.commit = "commit_then_raise"                                         # it WAS committed, but the caller cannot know
    use_flaky(r, k)
    rest = count_rest(r, monkeypatch)
    base = checked_out(db)
    try:
        with pytest.raises(RecoveryUnsettled) as info:
            await r._recover(False, checked=False)
        assert info.value.result.db is DbOutcome.UNCERTAIN and rest == []  # no side effect on top of an unknown commit
        assert r._recovery_unsettled is not None and checked_out(db) == base   # connection discarded, not leaked
        original = r._recovery_unsettled
        k.commit = "ok"
        rec = record_stages(r, monkeypatch)
        out = await r._recover(False, checked=False)                       # RECONCILIATION first, then the mutation
        assert rec["stages"] == [RecoveryKind.RECONCILIATION, RecoveryKind.MUTATION] and rec["bodies"] == 1
        assert r._recovery_unsettled is None and out["outcome"] == "COMMITTED"
        assert out["requeued"] == 0                                        # converged on the DB's current state
        assert original.candidates and all(c.update_applied for c in original.candidates)
        assert [row[1] for row in await rows(h)] == ["PENDING"]
    finally:
        await tidy(r, k)


async def test_an_uncertain_commit_that_never_landed_converges_as_rolled_back(db, tmp_path):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    k.commit = "raise_before"
    use_flaky(r, k)
    try:
        with pytest.raises(RecoveryUnsettled):
            await r._recover(False, checked=False)
        assert [row[1] for row in await rows(h)] == ["CLAIMED"]
        k.commit = "ok"
        out = await r._recover(False, checked=False)
        assert out["requeued"] == 1 and r._recovery_clean()                # the retry reads the current state and acts
    finally:
        await tidy(r, k)


async def test_a_failing_reconciliation_keeps_the_original_evidence_and_a_retry_succeeds(db, tmp_path):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    k.commit = "commit_then_raise"
    use_flaky(r, k)
    try:
        with pytest.raises(RecoveryUnsettled):
            await r._recover(False, checked=False)
        original = r._recovery_unsettled
        snapshot = [(c.request_id, c.original_claim_token, c.intent, c.update_applied) for c in original.candidates]
        k.commit, k.connect_error = "ok", True                             # the reconciliation itself cannot connect
        with pytest.raises(RecoveryUnsettled):
            await r._recover(False, checked=False)
        assert r._recovery_unsettled is original                           # not replaced, not cleared
        assert snapshot == [(c.request_id, c.original_claim_token, c.intent, c.update_applied)
                            for c in original.candidates]
        assert r._recovery is None                                         # the failed task was reaped
        k.connect_error = False
        out = await r._recover(False, checked=False)                       # the same evidence is reconciled again
        assert r._recovery_unsettled is None and out["outcome"] == "COMMITTED"
    finally:
        await tidy(r, k)


async def test_stop_resolves_an_uncertain_recovery_by_reconciling_and_only_then_stops(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    watch = watch_loop_reap(r, monkeypatch)
    k = Knobs()
    use_flaky(r, k)
    try:
        await r.start()
        await loop_idle(watch, r)
        await claimed_request(h, stale=True)
        k.commit = "commit_then_raise"
        with pytest.raises(RecoveryUnsettled):
            await r.recover()
        k.commit, k.connect_error = "ok", True                             # reconciliation cannot run at first
        with pytest.raises(BaseException):
            await asyncio.wait_for(r.stop(), BOUND)
        assert r.lifecycle is L.STOP_INCOMPLETE and r._recovery_unsettled is not None
        assert r._lock_conn is not None and not await lock_free(db)
        k.connect_error = False
        await asyncio.wait_for(r.stop(), BOUND)                            # a successful reconciliation is required
        assert r.lifecycle is L.STOPPED and r._recovery_clean() and await lock_free(db)
    finally:
        k.connect_error = False
        await stop_quietly(r)


async def test_reconciliation_pending_blocks_a_new_mutation(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    k.commit = "commit_then_raise"
    use_flaky(r, k)
    with pytest.raises(RecoveryUnsettled):
        await r._recover(False, checked=False)
    k.commit = "ok"
    entered, release = asyncio.Event(), asyncio.Event()
    import app.services.runner as runner_module
    real = runner_module.reconcile_body

    async def slow_reconcile(original, session, evidence):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return await real(original, session, evidence)

    monkeypatch.setattr(runner_module, "reconcile_body", slow_reconcile)
    first = asyncio.create_task(r._recover(False, checked=False))
    try:
        await wait(entered)
        assert r._recovery.kind is RecoveryKind.RECONCILIATION
        assert (await r._recover(False, checked=False))["outcome"] == "SKIPPED"   # no second operation starts
        with pytest.raises(RuntimeError):
            await r._recover(True, checked=False)
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await settle(r)


# --- concurrency / statement timeout -----------------------------------------------------------------------------------
async def test_periodic_calls_run_one_operation_and_the_rest_are_skipped(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h, recovery_timeout_seconds=5.0, recovery_statement_timeout_seconds=1.0)
    entered, release = stall_body(r, monkeypatch)
    first = asyncio.create_task(r._recover(False, checked=False))
    try:
        await wait(entered)
        skipped = await asyncio.gather(*(r._recover(False, checked=False) for _ in range(3)))
        assert [s["outcome"] for s in skipped] == ["SKIPPED"] * 3
    finally:
        release.set()
    out = await asyncio.wait_for(first, BOUND)
    assert out["outcome"] == "COMMITTED" and out["requeued"] == 1


async def test_a_statement_timeout_rolls_back_frees_the_connection_and_the_next_recovery_succeeds(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    before = await rows(h)
    # Only THIS test uses a short statement limit, and only the pg_sleep statement is meant to hit it; after the failed
    # call the same Runner goes back to a normal limit so that the real SELECT / UPDATE / COMMIT cannot trip over it.
    r = make_runner(h, recovery_timeout_seconds=5.0, recovery_statement_timeout_seconds=0.3)
    real = r._recovery_body
    calls = []
    reached_sql = asyncio.Event()

    async def slow_then_normal(startup, session, evidence):
        calls.append(1)
        if len(calls) == 1:
            # Set right before the call: it only says the code reached the point where the statement is handed to
            # session.execute. The server-side timeout is judged by the outcome below, not by this event.
            reached_sql.set()
            await session.execute(text("select pg_sleep(5)"))
        return await real(startup, session, evidence)

    monkeypatch.setattr(r, "_recovery_body", slow_then_normal)
    base = checked_out(db)
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(Exception) as info:
            await r._recover(False, checked=False)
        elapsed = asyncio.get_running_loop().time() - started
        assert reached_sql.is_set(), "the body never reached its pg_sleep call"
        message = str(info.value).lower()
        assert not isinstance(info.value, RecoveryError), f"expected the database's own error, got {info.value!r}"
        assert "statement timeout" in message or "canceling statement" in message, message      # PostgreSQL / asyncpg
        assert elapsed < 4.0, f"returned after {elapsed:.1f}s: not before the 5s operation timeout / pg_sleep(5)"
        assert r._recovery_clean() and await rows(h) == before and checked_out(db) == base      # rolled back / released
        monkeypatch.setattr(r, "_recovery_statement_ms", 5000)             # normal limit for the follow-up recovery
        out = await r._recover(False, checked=False)
        assert out["requeued"] == 1
    finally:
        await settle(r)


async def test_an_operation_timeout_shorter_than_the_pool_timeout_still_cancels_a_blocked_checkout(db, tmp_path):
    from app.db import Database, create_engine
    engine = create_engine(db.engine.url.render_as_string(hide_password=False), pool_size=1, max_overflow=0,
                           pool_timeout=30.0)
    small = Database(engine)
    h = Harness(small, tmp_path)
    r = make_runner(h, deadline=True)
    r._state = L.RUNNING
    held = await engine.connect()                                          # the only connection is taken
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(RecoveryTimeout) as info:
            await r.recover()                                              # blocked in checkout, far below pool_timeout
        assert asyncio.get_running_loop().time() - started < 10
        assert not info.value.pending and info.value.result.db is DbOutcome.ROLLED_BACK
    finally:
        r._state = L.STOPPED
        await held.close()
        await settle(r)
        await engine.dispose()


async def test_stop_returns_within_the_bounded_waits_when_the_recovery_cannot_be_cancelled(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h, deadline=True)
    entered, release = stall_body(r, monkeypatch, ignore_cancel=True)
    try:
        # started through the loop: the first periodic recovery ignores cancellation
        task = asyncio.create_task(r.start())
        await wait(entered)
        with pytest.raises(RecoveryTimeout):
            await asyncio.wait_for(task, BOUND)
        started = asyncio.get_running_loop().time()
        with pytest.raises(RunnerStopIncomplete):
            await asyncio.wait_for(r.stop(), BOUND)
        assert asyncio.get_running_loop().time() - started < SHUTDOWN_WAIT + 3      # the settle wait, not forever
    finally:
        release.set()
        await stop_quietly(r)


# --- recovery STAGES of one call: CLEANUP -> RECONCILIATION -> CLEANUP -> MUTATION ----------------------------------------
async def make_uncertain(r: Runner, k: Knobs, *, unreleased: bool) -> RecoveryEvidence:
    """The first call commits but cannot know it (UNCERTAIN). With `unreleased` the session cannot be discarded either, so
    BOTH unresolved evidence fields hold (the same evidence object) and the next call has a CLEANUP to do first."""
    k.commit = "commit_then_raise"
    if unreleased:
        k.invalidate = "error"
    with pytest.raises(RecoveryUnsettled):
        await r._recover(False, checked=False)
    k.commit = "ok"
    evidence = r._recovery_unsettled
    assert evidence is not None
    assert (r._recovery_cleanup_unsettled is evidence) is unreleased, "unexpected cleanup state after the setup call"
    return evidence


async def set_request_status(h: Harness, status: str):
    async with h.db.transaction() as s:
        await s.execute(text("update execution_requests set status = :s"), {"s": status})


async def test_a_finished_cleanup_goes_on_to_reconciliation_and_the_mutation_in_the_same_call(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    use_flaky(r, k)
    base = checked_out(db)
    try:
        await make_uncertain(r, k, unreleased=True)                        # both unresolved states exist
        k.invalidate = "ok"
        rec = record_stages(r, monkeypatch)
        out = await r._recover(False, checked=False)
        assert rec["stages"] == [RecoveryKind.CLEANUP, RecoveryKind.RECONCILIATION, RecoveryKind.MUTATION]
        assert out["outcome"] == "COMMITTED" and rec["bodies"] == 1
        assert r._recovery_clean() and checked_out(db) == base
        assert [row[1] for row in await rows(h)] == ["PENDING"]
    finally:
        await tidy(r, k)


async def test_a_cleanup_that_cannot_release_ends_the_call_after_one_stage_and_keeps_its_evidence(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    use_flaky(r, k)
    try:
        evidence = await make_uncertain(r, k, unreleased=True)             # invalidate keeps failing
        rec = record_stages(r, monkeypatch)
        with pytest.raises(RecoveryUnsettled, match="connection release unconfirmed"):
            await r._recover(False, checked=False)
        assert rec["stages"] == [RecoveryKind.CLEANUP] and rec["bodies"] == 0   # no repetition, no later stage
        assert r._recovery_cleanup_unsettled is evidence and r._recovery_unsettled is evidence
        k.invalidate = "ok"
        out = await r._recover(False, checked=False)
        assert rec["stages"][1:] == [RecoveryKind.CLEANUP, RecoveryKind.RECONCILIATION, RecoveryKind.MUTATION]
        assert out["outcome"] == "COMMITTED" and rec["bodies"] == 1 and r._recovery_clean()
    finally:
        await tidy(r, k)


async def test_an_unresolved_reconciliation_never_starts_a_mutation_and_keeps_its_evidence(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    use_flaky(r, k)
    try:
        original = await make_uncertain(r, k, unreleased=False)
        await set_request_status(h, "DONE")                                 # neither the original nor the intended state
        rec = record_stages(r, monkeypatch)
        for call in (1, 2):                                                 # the same answer every time: no growth, no loop
            with pytest.raises(RecoveryUnsettled, match="unresolved"):
                await r._recover(False, checked=False)
            assert rec["stages"] == [RecoveryKind.RECONCILIATION] * call and rec["bodies"] == 0
            assert r._recovery_unsettled is original and r._recovery_cleanup_unsettled is None
        assert [row[1] for row in await rows(h)] == ["DONE"]
    finally:
        await set_request_status(h, "PENDING")                              # the intended state again: it can be reconciled
        await tidy(r, k)


async def test_the_longest_normal_path_uses_all_four_stages_and_each_one_really_releases_its_evidence(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    use_flaky(r, k)
    base = checked_out(db)

    async def per_stage(kind, index):
        if index == 1:                                                      # the reconciliation confirms the commit, but its
            k.close, k.invalidate = "error_always", "error"                 # OWN session cannot be released
        else:
            k.close = k.invalidate = "ok"

    try:
        ev = await make_uncertain(r, k, unreleased=True)
        rec = record_stages(r, monkeypatch, before=per_stage)
        out = await r._recover(False, checked=False)
        kinds = [kind for kind, _cleanup, _commit in rec["registrations"]]
        assert kinds == rec["stages"] == [RecoveryKind.CLEANUP, RecoveryKind.RECONCILIATION, RecoveryKind.CLEANUP,
                                          RecoveryKind.MUTATION]
        (_, c0, u0), (_, c1, u1), (_, c2, u2), (_, c3, u3) = rec["registrations"]
        assert c0 is ev and u0 is ev                                        # what the first stage found
        assert c1 is None and u1 is ev                                      # the 1st CLEANUP released the old session
        assert u2 is None and c2 is not None and c2 is not ev               # the reconciliation released the commit evidence
        assert c3 is None and u3 is None                                    # the 2nd CLEANUP released the reconciliation's
        assert len(rec["registrations"]) == runner_module._MAX_RECOVERY_STAGES   # a fifth registration never happens
        assert rec["bodies"] == 1                                           # the mutation ran once, and last
        assert out["outcome"] == "COMMITTED" and r._recovery_clean() and checked_out(db) == base
    finally:
        await tidy(r, k)


async def test_a_reconciliation_whose_own_session_stays_unreleased_goes_on_to_cleanup_and_stops_there(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    use_flaky(r, k)
    try:
        await make_uncertain(r, k, unreleased=False)
        rec = record_stages(r, monkeypatch)
        k.close, k.invalidate = "error_always", "error"
        with pytest.raises(RecoveryUnsettled, match="connection release unconfirmed"):
            await r._recover(False, checked=False)
        assert rec["stages"] == [RecoveryKind.RECONCILIATION, RecoveryKind.CLEANUP] and rec["bodies"] == 0
        assert r._recovery_unsettled is None and r._recovery_cleanup_unsettled is not None   # progress, then stuck
        k.close = k.invalidate = "ok"
        out = await r._recover(False, checked=False)
        assert rec["stages"][2:] == [RecoveryKind.CLEANUP, RecoveryKind.MUTATION]
        assert out["outcome"] == "COMMITTED" and r._recovery_clean()
    finally:
        await tidy(r, k)


async def stalled_reconciliation(monkeypatch):
    """The reconciliation operation stops at its entry until `release` is set (the original runs afterwards)."""
    entered, release = asyncio.Event(), asyncio.Event()
    real = runner_module.reconcile_body

    async def slow(original, session, evidence):
        entered.set()
        await release.wait()
        return await real(original, session, evidence)

    monkeypatch.setattr(runner_module, "reconcile_body", slow)
    return entered, release


async def test_replaced_commit_evidence_is_not_a_release_and_is_neither_deleted_nor_overwritten(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    use_flaky(r, k)
    foreign = RecoveryEvidence()
    release = None
    try:
        await make_uncertain(r, k, unreleased=False)
        rec = record_stages(r, monkeypatch)
        entered, release = await stalled_reconciliation(monkeypatch)
        call = asyncio.create_task(r._recover(False, checked=False))
        await wait(entered, "the reconciliation to be running")
        async with r._lifecycle_lock:                                        # a conflicting evidence object appears
            r._recovery_unsettled = foreign
        release.set()
        with pytest.raises(RecoveryUnsettled, match="invariant violation") as info:
            await asyncio.wait_for(call, BOUND)
        assert f"{id(foreign):#x}" in str(info.value)                       # the real object identities are reported
        assert rec["stages"] == [RecoveryKind.RECONCILIATION] and rec["bodies"] == 0
        assert r._recovery_unsettled is foreign                             # not cleared and not replaced by the result
    finally:
        if release is not None:
            release.set()
        async with r._lifecycle_lock:
            r._recovery_unsettled = None
        await tidy(r, k)


async def test_replaced_cleanup_evidence_is_not_a_release_and_is_neither_deleted_nor_overwritten(db, tmp_path, monkeypatch):
    # With the single-operation gate nothing else can register while a cleanup runs, so this is NOT a normal path but an
    # invariant violation, and the call must say so instead of treating the cleanup as successful.
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    use_flaky(r, k)
    foreign = RecoveryEvidence()

    async def swap(kind, index):
        k.invalidate = "ok"
        async with r._lifecycle_lock:
            r._recovery_cleanup_unsettled = foreign

    try:
        ev = await make_uncertain(r, k, unreleased=True)
        rec = record_stages(r, monkeypatch, before=swap)
        with pytest.raises(RecoveryUnsettled, match="invariant violation") as info:
            await r._recover(False, checked=False)
        assert f"{id(foreign):#x}" in str(info.value)
        assert rec["stages"] == [RecoveryKind.CLEANUP] and rec["bodies"] == 0
        assert r._recovery_cleanup_unsettled is foreign and r._recovery_unsettled is ev
    finally:
        async with r._lifecycle_lock:
            r._recovery_cleanup_unsettled = None
        await tidy(r, k)


async def test_a_lifecycle_change_between_stages_ends_the_call_before_the_mutation(db, tmp_path, monkeypatch):
    # A real stop() would cancel the reconciliation task itself and so change what this test checks (a reconciliation
    # that SUCCEEDED followed by a changed generation). The generation is therefore moved under the lifecycle lock.
    # NOTE: generation changed + state still RUNNING is an artificial combination. In production a stop leaves the state
    # STOPPING / STOPPED, and then `_loop` leaves quietly instead of logging this RuntimeError.
    h = Harness(db, tmp_path)
    await claimed_request(h, stale=True)
    r = make_runner(h)
    k = Knobs()
    use_flaky(r, k)
    release = None
    try:
        await make_uncertain(r, k, unreleased=False)
        rec = record_stages(r, monkeypatch)
        entered, release = await stalled_reconciliation(monkeypatch)
        call = asyncio.create_task(r._recover(False, checked=False))
        await wait(entered, "the reconciliation to be running")
        async with r._lifecycle_lock:
            r._generation = r._generation + 1
        release.set()
        with pytest.raises(RuntimeError, match="lifecycle changed"):
            await asyncio.wait_for(call, BOUND)
        assert rec["stages"] == [RecoveryKind.RECONCILIATION] and rec["bodies"] == 0
        assert r._recovery_unsettled is None                                # the reconciliation itself did succeed
    finally:
        if release is not None:
            release.set()
        await tidy(r, k)


async def test_the_loop_does_not_dispatch_while_the_commit_outcome_is_unresolved(db, tmp_path, monkeypatch):
    # Pins the current fail-closed behaviour (no change here): `_loop` logs the RecoveryError and does not dispatch.
    # It uses a real start()/stop() and a loop wake-up; it is not about the advisory lock or the start sequence.
    h = Harness(db, tmp_path)
    r = make_runner(h)                                                      # poll 3600: the loop only runs when woken
    watch = watch_loop_reap(r, monkeypatch)
    k = Knobs()
    use_flaky(r, k)
    restore = False
    try:
        await r.start()
        await loop_idle(watch, r)
        await claimed_request(h, stale=True)
        await make_uncertain(r, k, unreleased=False)
        await set_request_status(h, "DONE")                                 # reconciliation cannot decide any more
        restore = True
        waiting = await h.new_task()
        await h.tasks.request_execution(waiting.id)                         # something a dispatch WOULD claim
        loop_failed, dispatches, real_recover = asyncio.Event(), [], r.recover

        async def recover(*, startup=False):
            try:
                return await real_recover(startup=startup)
            except RecoveryUnsettled:
                loop_failed.set()
                raise

        async def dispatch():
            dispatches.append(1)
            return 0

        monkeypatch.setattr(r, "recover", recover)
        monkeypatch.setattr(r, "_dispatch", dispatch)
        r.notify()                                                          # the loop runs one more iteration
        await wait(loop_failed, "the loop's recovery to end as unsettled")
        assert dispatches == [], "the loop dispatched although the commit outcome is unresolved"
        assert [row[1] for row in await rows(h)] == ["DONE", "PENDING"]     # the waiting request was not claimed
    finally:
        if restore:
            async with h.db.transaction() as s:                             # back to the intended state: reconcilable
                await s.execute(text("update execution_requests set status = 'PENDING', claim_token = NULL "
                                     "where status = 'DONE'"))
        await tidy(r, k)
        await stop_quietly(r)
    assert r.lifecycle is L.STOPPED and r._recovery_clean()
