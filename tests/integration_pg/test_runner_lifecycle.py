"""Runner lifecycle: state transitions, strict recovery gating, the settle contract of stop() / failed start() (resource
based final transition, cancellation safety, exception priority), connection ownership and the `_prune_done` /
done-callback split. Waiting is done with Events and controllable futures, never with sleep lengths."""
import asyncio
import logging
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.models.tables import ExecutionRequest
from app.repositories.approval_repository import ExecutionRequestRepository
from app.services.runner import Runner, RunnerAlreadyActive, RunnerLifecycle, RunnerStopIncomplete, _InFlight
from tests.harness import Harness

KEY = 987_654_321
L = RunnerLifecycle


# --- helpers -----------------------------------------------------------------------------------------------------------
def make_runner(h: Harness, db=None, grace=0.1, poll=3600.0) -> Runner:
    return Runner(db or h.db, h.leases, h.workflow, h.gates, [h.claude, h.codex], advisory_key=KEY, poll_seconds=poll,
                  claim_timeout_seconds=300, shutdown_grace_seconds=grace)


async def requests(h: Harness):
    async with h.db.transaction() as s:
        rows = (await s.execute(select(ExecutionRequest).order_by(ExecutionRequest.created_at))).scalars().all()
        return [(r.id, r.status, r.claim_token, r.note) for r in rows]


async def claimed_request(h: Harness):
    """A CLAIMED request whose owner is 'dead': the recovery target."""
    task = await h.new_task()
    await h.tasks.request_execution(task.id)
    async with h.db.transaction() as s:
        await ExecutionRequestRepository(s).claim_next("dead-runner")
    return task


def resources(r: Runner):
    return r._loop_task is not None, r._lock_conn is not None, len(r._inflight)


async def lock_free(db) -> bool:
    """True when nobody holds the Runner advisory lock. The probe lock is released again at once."""
    conn = await db.engine.connect()
    try:
        got = (await conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": KEY})).scalar()
        if got:
            assert (await conn.execute(text("select pg_advisory_unlock(:k)"), {"k": KEY})).scalar() is True
        return bool(got)
    finally:
        await conn.close()


async def wait(event: asyncio.Event):
    await asyncio.wait_for(event.wait(), 10)


def stalled(r: Runner, name: str, monkeypatch, *, only_periodic=False):
    """Replaces r.<name> with a wrapper that stops at its entry until `proceed` is set (original called once)."""
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = getattr(r, name)

    async def wrapper(*args, **kw):
        startup = bool(args and args[0] is True) or bool(kw.get("startup"))
        if not (only_periodic and startup):
            entered.set()
            await proceed.wait()
        return await original(*args, **kw)

    monkeypatch.setattr(r, name, wrapper)
    return entered, proceed


def track(r: Runner, name: str, monkeypatch):
    """Wraps a settle coroutine: `entered` / `finished` Events and a call counter (original called exactly once)."""
    entered, finished, calls = asyncio.Event(), asyncio.Event(), []
    original = getattr(r, name)

    async def wrapper(*args, **kw):
        calls.append(1)
        entered.set()
        try:
            return await original(*args, **kw)
        finally:
            finished.set()

    monkeypatch.setattr(r, name, wrapper)
    return entered, finished, calls


class SignalLock(asyncio.Lock):
    """An asyncio.Lock that reports when somebody has to wait for it (test control of the lifecycle lock)."""

    def __init__(self):
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.contended.set()
        return await super().acquire()


class Plan:
    """What the connection proxy should do. Failures are injected at a named stage of Runner.start()."""

    def __init__(self, fail_at=None, exc=None, fail_close=0, gate_close=False):
        self.fail_at, self.exc, self.fail_close = fail_at, exc, fail_close
        self.close_entered = asyncio.Event() if gate_close else None
        self.close_proceed = asyncio.Event() if gate_close else None


class ConnProxy:
    """Wraps a REAL AsyncConnection: everything is delegated, except what the plan injects. `close` can fail (a given
    number of times) or stall at a gate; proxy_close_attempts counts every call, underlying_close_calls only the
    delegations to the real connection."""

    def __init__(self, real, plan: Plan):
        self.real, self.plan = real, plan
        self.proxy_close_attempts = 0
        self.underlying_close_calls = 0
        self.emergency_closed = False

    async def execution_options(self, **kw):
        if self.plan.fail_at == "execution_options":
            raise self.plan.exc
        await self.real.execution_options(**kw)
        return self

    async def execute(self, statement, params=None):
        if self.plan.fail_at == "lock_query" and "pg_try_advisory_lock" in str(statement):
            raise self.plan.exc
        return await self.real.execute(statement, params)

    async def close(self):
        self.proxy_close_attempts += 1
        if self.plan.close_entered is not None:
            self.plan.close_entered.set()
            await self.plan.close_proceed.wait()
        if self.plan.fail_close > 0:
            self.plan.fail_close -= 1
            raise OSError("close failed")
        self.underlying_close_calls += 1
        await self.real.close()

    async def emergency_close(self):
        """Only when the Runner did not clean up: recorded separately so a failure can be diagnosed."""
        if self.underlying_close_calls == 0:
            self.emergency_closed = True
            await self.real.close()


class ProxyDb:
    """A Database whose `engine.connect()` hands out ConnProxy objects (transactions are the real ones)."""

    def __init__(self, real, plan: Plan, fail_connect: BaseException | None = None):
        self._real, self.plan, self.proxies = real, plan, []
        self.engine = SimpleNamespace(connect=self._connect)
        self._fail_connect = fail_connect

    async def _connect(self):
        if self._fail_connect is not None:
            raise self._fail_connect
        proxy = ConnProxy(await self._real.engine.connect(), self.plan)
        self.proxies.append(proxy)
        return proxy

    def transaction(self):
        return self._real.transaction()

    async def cleanup(self, db):
        """finally-block: close what the Runner left open, then prove the advisory lock is free."""
        for proxy in self.proxies:
            await asyncio.wait_for(proxy.emergency_close(), 10)
        emergency = [i for i, p in enumerate(self.proxies) if p.emergency_closed]
        assert await asyncio.wait_for(lock_free(db), 10), f"advisory lock leaked (emergency closes: {emergency})"


def counts(proxy: ConnProxy):
    return proxy.proxy_close_attempts, proxy.underlying_close_calls


# --- basic transitions -----------------------------------------------------------------------------------------------------
async def test_transitions_and_resources(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)
    await r.stop()                                                   # STOPPED: no-op
    await r.start()
    assert r.lifecycle is L.RUNNING and resources(r) == (True, True, 0)
    with pytest.raises(RuntimeError):
        await r.start()                                              # double start refused
    assert r.lifecycle is L.RUNNING
    await r.stop()
    assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)
    await r.stop()                                                   # again: still a no-op
    await r.start()                                                  # restart after a clean stop
    assert r.lifecycle is L.RUNNING
    await r.stop()


async def test_recovery_is_refused_outside_its_state_and_changes_nothing(db, tmp_path):
    h = Harness(db, tmp_path)
    await claimed_request(h)
    before = await requests(h)
    r = make_runner(h)
    for startup in (False, True):                                    # STOPPED
        with pytest.raises(RuntimeError):
            await r.recover(startup=startup)
    assert await requests(h) == before


async def test_startup_recovery_runs_inside_start(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h)
    r = make_runner(h)
    seen = {}
    real = r._recover_requests_locked

    async def spy(startup):
        summary = await real(startup)
        seen.setdefault("first", (startup, summary["requeued"]))
        return summary

    monkeypatch.setattr(r, "_recover_requests_locked", spy)
    await r.start()
    try:
        assert seen["first"] == (True, 1)                            # every CLAIMED request of a dead runner
    finally:
        await r.stop()


async def test_starting_state_refuses_start_stop_and_recover(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    await claimed_request(h)
    r = make_runner(h)
    original = r.recover
    entered, proceed, calls = asyncio.Event(), asyncio.Event(), []

    async def recover(*, startup=False):
        calls.append((startup, r._lock_conn is not None, r.lifecycle))
        entered.set()
        await proceed.wait()
        return await original(startup=startup)                       # the saved bound method, exactly once

    monkeypatch.setattr(r, "recover", recover)
    starting = asyncio.create_task(r.start())
    await wait(entered)
    try:
        assert calls == [(True, True, L.STARTING)]                   # start set the lock connection BEFORE recovering
        before = await requests(h)
        for call in (r.start, r.stop, lambda: original(startup=False)):
            with pytest.raises(RuntimeError):
                await call()
        assert await requests(h) == before and r.lifecycle is L.STARTING
    finally:
        proceed.set()
        await starting
        await r.stop()


# --- failed start: connection ownership and settle --------------------------------------------------------------------------
STAGES = ["execution_options", "lock_query", "already_active", "recover"]


async def run_failing_start(db, h, monkeypatch, stage, plan_kw=None):
    """Starts a Runner (on a proxy database) that fails at `stage`. Returns (runner, proxy_db, other, error)."""
    error = ValueError(stage)
    plan = Plan(fail_at=stage if stage in ("execution_options", "lock_query") else None, exc=error, **(plan_kw or {}))
    rdb = ProxyDb(db, plan)
    r = make_runner(h, db=rdb)
    other = None
    if stage == "already_active":
        other = make_runner(h)
        await other.start()
    if stage == "recover":
        async def failing_recover(*, startup=False):
            raise error

        monkeypatch.setattr(r, "recover", failing_recover)
    return r, rdb, other, error


async def test_connect_failure_leaves_nothing_behind(db, tmp_path):
    h = Harness(db, tmp_path)
    error = ValueError("connect failed")
    rdb = ProxyDb(db, Plan(), fail_connect=error)
    r = make_runner(h, db=rdb)
    with pytest.raises(ValueError) as info:
        await r.start()
    assert info.value is error and r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)
    assert await lock_free(db)


@pytest.mark.parametrize("stage", STAGES)
async def test_a_failed_start_closes_its_connection_exactly_once_and_returns_to_stopped(db, tmp_path, monkeypatch, stage):
    h = Harness(db, tmp_path)
    r, rdb, other, error = await run_failing_start(db, h, monkeypatch, stage)
    try:
        expected = RunnerAlreadyActive if stage == "already_active" else ValueError
        with pytest.raises(expected) as info:
            await r.start()
        if stage != "already_active":
            assert info.value is error
        assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)
        (proxy,) = rdb.proxies
        assert counts(proxy) == (1, 1)                               # one close, never a second one on the same conn
    finally:
        if other is not None:
            await other.stop()
        await rdb.cleanup(db)


@pytest.mark.parametrize("stage", STAGES)
async def test_an_unconfirmed_close_after_a_failed_start_keeps_the_connection_until_stop_retries(
        db, tmp_path, monkeypatch, stage):
    h = Harness(db, tmp_path)
    r, rdb, other, error = await run_failing_start(db, h, monkeypatch, stage, {"fail_close": 1})
    try:
        expected = RunnerAlreadyActive if stage == "already_active" else ValueError
        with pytest.raises(expected) as info:
            await r.start()
        assert any("cleanup after the failed start" in n for n in getattr(info.value, "__notes__", []))   # not hidden
        (proxy,) = rdb.proxies
        assert r.lifecycle is L.STOP_INCOMPLETE and r._lock_conn is proxy and counts(proxy) == (1, 0)
        before = await requests(h)
        with pytest.raises(RuntimeError):
            await r.start()
        for startup in (True, False):
            with pytest.raises(RuntimeError):
                await Runner.recover(r, startup=startup)             # the class method: `recover` may be patched here
        assert await requests(h) == before
        await r.stop()                                               # retries the SAME connection
        assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0) and counts(proxy) == (2, 1)
    finally:
        if other is not None:
            await other.stop()
        await rdb.cleanup(db)


# --- failed start and cancellation: who is the final error ------------------------------------------------------------------
async def start_with_cleanup_gate(db, h, original: BaseException, **plan_kw):
    plan = Plan(fail_at="lock_query", exc=original, gate_close=True, **plan_kw)
    rdb = ProxyDb(db, plan)
    r = make_runner(h, db=rdb)
    starting = asyncio.create_task(r.start())
    await wait(plan.close_entered)                                    # the failed start is now cleaning up
    return r, rdb, plan, starting


async def test_cancellation_during_cleanup_wins_and_keeps_the_start_error_as_cause(db, tmp_path):
    h = Harness(db, tmp_path)
    original = ValueError("start failed")
    r, rdb, plan, starting = await start_with_cleanup_gate(db, h, original)
    try:
        starting.cancel()                                             # (b) a cancellation while cleaning up
        plan.close_proceed.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await starting
        assert not isinstance(info.value, ExceptionGroup) and info.value.__cause__ is original
        assert r.lifecycle is L.STOPPED and counts(rdb.proxies[0]) == (1, 1)
    finally:
        plan.close_proceed.set()
        await rdb.cleanup(db)


async def test_an_original_start_cancellation_stays_final_and_extra_cancels_are_only_noted(db, tmp_path):
    h = Harness(db, tmp_path)
    original = asyncio.CancelledError()
    r, rdb, plan, starting = await start_with_cleanup_gate(db, h, original)
    try:
        starting.cancel()                                             # (c) a further cancellation during cleanup
        plan.close_proceed.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await starting
        assert info.value is original and r.lifecycle is L.STOPPED and counts(rdb.proxies[0]) == (1, 1)
    finally:
        plan.close_proceed.set()
        await rdb.cleanup(db)


async def test_an_original_start_cancellation_runs_the_cleanup_to_the_end(db, tmp_path):
    h = Harness(db, tmp_path)
    original = asyncio.CancelledError()
    rdb = ProxyDb(db, Plan(fail_at="lock_query", exc=original))
    r = make_runner(h, db=rdb)
    try:
        with pytest.raises(asyncio.CancelledError) as info:           # (a) the cancellation IS the start error
            await r.start()
        assert info.value is original and r.lifecycle is L.STOPPED and counts(rdb.proxies[0]) == (1, 1)
    finally:
        await rdb.cleanup(db)


async def test_start_error_and_cleanup_error_travel_as_one_cause_of_the_final_cancellation(db, tmp_path):
    h = Harness(db, tmp_path)
    original = ValueError("start failed")
    r, rdb, plan, starting = await start_with_cleanup_gate(db, h, original, fail_close=1)
    try:
        starting.cancel()                                             # (d) cleanup fails AND a cancellation arrives
        plan.close_proceed.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await starting
        cause = info.value.__cause__
        assert isinstance(cause, ExceptionGroup) and {type(e) for e in cause.exceptions} == {ValueError, OSError}
        assert cause.exceptions[0] is original
        proxy = rdb.proxies[0]
        assert r.lifecycle is L.STOP_INCOMPLETE and r._lock_conn is proxy and counts(proxy) == (1, 0)
        await r.stop()
        assert r.lifecycle is L.STOPPED and counts(proxy) == (2, 1)
    finally:
        plan.close_proceed.set()
        await rdb.cleanup(db)


async def test_the_recover_failure_inside_start_with_a_proxy_installed_by_the_test_keeps_the_proxy(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    plan = Plan(fail_close=1)
    seen = {}
    original_error = ValueError("startup failed")

    async def recover(*, startup=False):
        seen["lock_conn_set"] = r._lock_conn is not None            # start stored the connection before recovering
        seen["proxy"] = ConnProxy(r._lock_conn, plan)
        r._lock_conn = seen["proxy"]                                # the field now owns a proxy of the real connection
        raise original_error

    monkeypatch.setattr(r, "recover", recover)
    try:
        with pytest.raises(ValueError) as info:
            await r.start()
        proxy = seen["proxy"]
        assert seen["lock_conn_set"] and info.value is original_error
        assert r.lifecycle is L.STOP_INCOMPLETE and r._lock_conn is proxy and counts(proxy) == (1, 0)
        await r.stop()
        assert r.lifecycle is L.STOPPED and counts(proxy) == (2, 1)
    finally:
        await proxy.emergency_close()
        assert await lock_free(db)


# --- stop: settle, cancellation, priorities ---------------------------------------------------------------------------------------
async def test_a_cancelled_stop_still_settles_to_stopped_and_then_raises_the_cancellation(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    _entered, finished, calls = track(r, "_settle_stop", monkeypatch)
    drain_entered, drain_proceed = stalled(r, "_drain", monkeypatch)
    stopping = asyncio.create_task(r.stop())
    await wait(drain_entered)
    assert r.lifecycle is L.STOPPING                                 # observable only while the settle task runs
    stopping.cancel()
    await asyncio.sleep(0)                                           # let the first cancellation be consumed
    stopping.cancel()                                                # a second one must not stop the settle either
    drain_proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert calls == [1] and finished.is_set()
    assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)      # nothing left behind
    assert await lock_free(db)


async def test_a_loop_task_that_delays_its_cancellation_is_waited_for_even_when_stop_is_cancelled(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_loop():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise

    monkeypatch.setattr(r, "_loop", slow_loop)
    await r.start()
    await wait(started)
    loop_task = r._loop_task
    stopping = asyncio.create_task(r.stop())
    await wait(cancelled)                                            # stop is waiting for the loop task to end
    stopping.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert loop_task.done() and r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)
    assert await lock_free(db)


async def test_a_cleanup_error_alone_is_raised_unchanged_and_leaves_stop_incomplete(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    boom = ValueError("boom")

    async def failing_drain():
        raise boom

    monkeypatch.setattr(r, "_drain", failing_drain)
    with pytest.raises(ValueError) as info:
        await r.stop()
    assert info.value is boom                                        # not turned into RunnerStopIncomplete
    assert r.lifecycle is L.STOP_INCOMPLETE and r._lock_conn is not None   # the connection was never released
    monkeypatch.undo()
    await r.stop()
    assert r.lifecycle is L.STOPPED and await lock_free(db)


async def test_cancellation_and_a_cleanup_error_together_raise_the_cancellation_from_the_error(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    boom = ValueError("boom")
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def failing_drain():
        entered.set()
        await proceed.wait()
        raise boom

    monkeypatch.setattr(r, "_drain", failing_drain)
    stopping = asyncio.create_task(r.stop())
    await wait(entered)
    stopping.cancel()
    proceed.set()
    with pytest.raises(asyncio.CancelledError) as info:
        await stopping
    assert not isinstance(info.value, ExceptionGroup) and info.value.__cause__ is boom
    assert r.lifecycle is L.STOP_INCOMPLETE
    monkeypatch.undo()
    await r.stop()
    assert r.lifecycle is L.STOPPED and await lock_free(db)


async def test_an_unconfirmed_close_during_stop_keeps_the_connection_and_a_later_stop_retries_it(db, tmp_path):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    proxy = ConnProxy(r._lock_conn, Plan(fail_close=1))
    r._lock_conn = proxy                                              # the lock is already held: only close is faked
    try:
        with pytest.raises(OSError):
            await r.stop()
        assert r.lifecycle is L.STOP_INCOMPLETE and r._lock_conn is proxy and counts(proxy) == (1, 0)
        assert resources(r) == (False, True, 0)                       # no loop, no tasks, connection still owned
        with pytest.raises(RuntimeError):
            await r.start()
        with pytest.raises(RuntimeError):
            await r.recover(startup=False)
        await r.stop()                                                # the real close now happens, exactly once
        assert r.lifecycle is L.STOPPED and counts(proxy) == (2, 1) and resources(r) == (False, False, 0)
        other = make_runner(h)
        await other.start()                                           # the lock was returned
        await other.stop()
    finally:
        await proxy.emergency_close()
        assert await lock_free(db)


async def test_stop_incomplete_keeps_the_lock_until_a_later_stop_succeeds(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    started, release = asyncio.Event(), asyncio.Event()

    async def stubborn(request, *, on_lease=None):
        started.set()
        while not release.is_set():                                  # swallows cancellation until released
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    monkeypatch.setattr(r, "process", stubborn)
    task = await h.new_task()
    await r.start()
    await h.tasks.request_execution(task.id)
    r.notify()
    await asyncio.wait_for(started.wait(), 10)
    _entered, finished, _calls = track(r, "_settle_stop", monkeypatch)
    with pytest.raises(RunnerStopIncomplete):
        await r.stop()
    try:
        assert finished.is_set()                                      # the settle task itself ended ...
        assert r.lifecycle is L.STOP_INCOMPLETE and resources(r) == (False, True, 1)     # ... resources may remain
        before = await requests(h)
        with pytest.raises(RunnerAlreadyActive):
            await make_runner(h).start()                              # the advisory lock is still held
        with pytest.raises(RuntimeError):
            await r.start()
        for startup in (True, False):
            with pytest.raises(RuntimeError):
                await r.recover(startup=startup)
        assert await requests(h) == before                            # claim token / status untouched
    finally:
        release.set()
    await asyncio.wait_for(r.stop(), 10)                              # the task ended: now it succeeds
    assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)
    again = make_runner(h)
    await again.start()                                               # the lock was returned
    await again.stop()


# --- the final transition waits for the lifecycle lock: a second cancellation arrives exactly there ------------------------------
async def test_a_cancel_while_stop_waits_for_its_final_transition_does_not_leave_stopping(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    r._lifecycle_lock = SignalLock()
    await r.start()
    ready, proceed = asyncio.Event(), asyncio.Event()
    original = r._shutdown_resources

    async def shutdown():
        await original()                                              # saved bound method, exactly once
        ready.set()
        await proceed.wait()

    monkeypatch.setattr(r, "_shutdown_resources", shutdown)
    _entered, finished, _calls = track(r, "_settle_stop", monkeypatch)
    stopping = asyncio.create_task(r.stop())                          # stop starts FIRST: the test holds no lock yet
    await wait(ready)
    assert r.lifecycle is L.STOPPING
    stopping.cancel()                                                 # first cancel: while the cleanup is pending
    await r._lifecycle_lock.acquire()                                 # only now does the test take the lifecycle lock
    try:
        r._lifecycle_lock.contended.clear()
        proceed.set()
        await wait(r._lifecycle_lock.contended)                       # the settle waits for the final transition
        assert not stopping.done() and not finished.is_set() and r.lifecycle is L.STOPPING
        stopping.cancel()                                             # second cancel, at the lock
    finally:
        r._lifecycle_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert finished.is_set() and r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)
    assert await lock_free(db)


async def test_a_cancel_while_a_failed_start_waits_for_its_final_transition_does_not_leave_starting(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    plan = Plan()
    rdb = ProxyDb(db, plan)
    r = make_runner(h, db=rdb)
    r._lifecycle_lock = SignalLock()
    error = ValueError("recover failed")

    async def failing_recover(*, startup=False):
        raise error

    ready, proceed = asyncio.Event(), asyncio.Event()
    original_unlock = r._unlock_best_effort

    async def unlock(conn):
        ready.set()
        await proceed.wait()
        await original_unlock(conn)

    monkeypatch.setattr(r, "recover", failing_recover)
    monkeypatch.setattr(r, "_unlock_best_effort", unlock)
    _entered, finished, _calls = track(r, "_settle_failed_start", monkeypatch)
    try:
        starting = asyncio.create_task(r.start())
        await wait(ready)
        assert r.lifecycle is L.STARTING
        starting.cancel()                                             # first cancel: during the cleanup
        await r._lifecycle_lock.acquire()
        try:
            r._lifecycle_lock.contended.clear()
            proceed.set()
            await wait(r._lifecycle_lock.contended)                   # unlock + close done; the transition waits
            assert not starting.done() and not finished.is_set() and r.lifecycle is L.STARTING
            starting.cancel()                                         # second cancel, at the lock
        finally:
            r._lifecycle_lock.release()
        with pytest.raises(asyncio.CancelledError) as info:
            await starting
        assert info.value.__cause__ is error
        assert finished.is_set() and r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)
        assert counts(rdb.proxies[0]) == (1, 1)
    finally:
        await rdb.cleanup(db)


# --- STOPPING and the lifecycle lock -----------------------------------------------------------------------------------------
async def test_stopping_state_refuses_start_stop_and_recover(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    await r.start()
    await claimed_request(h)                                          # appears after start: must stay CLAIMED
    entered, proceed = stalled(r, "_drain", monkeypatch)
    stopping = asyncio.create_task(r.stop())
    await wait(entered)
    try:
        assert r.lifecycle is L.STOPPING
        before = await requests(h)
        for call in (r.start, r.stop, lambda: r.recover(startup=False), lambda: r.recover(startup=True)):
            with pytest.raises(RuntimeError):
                await call()
        assert await requests(h) == before
    finally:
        proceed.set()
        await stopping
    assert r.lifecycle is L.STOPPED


async def test_stop_waits_for_a_recovery_that_already_holds_the_lifecycle_lock(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path)
    r = make_runner(h)
    entered, proceed = stalled(r, "_recover_requests_locked", monkeypatch, only_periodic=True)
    await r.start()                                                   # the loop's first periodic recovery stalls
    await wait(entered)
    stopping = asyncio.create_task(r.stop())
    for _ in range(20):
        await asyncio.sleep(0)                                        # let stop() run as far as it can
    assert not stopping.done() and r.lifecycle is L.RUNNING           # blocked on the lock, not yet STOPPING
    proceed.set()
    await asyncio.wait_for(stopping, 10)
    assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)


# --- _prune_done only pops; the done callback owns the exception, the log and the wake-up ------------------------------------------
class CountingEvent(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.set_calls = 0

    def set(self):
        self.set_calls += 1
        super().set()


async def finished_task(error: BaseException | None):
    async def work():
        if error is not None:
            raise error

    task = asyncio.ensure_future(work())
    await asyncio.wait({task})
    return task


async def test_prune_done_only_pops_finished_entries(db, tmp_path, monkeypatch):
    r = make_runner(Harness(db, tmp_path))
    done, running = await finished_task(None), asyncio.ensure_future(asyncio.Event().wait())
    done_id, running_id = uuid.uuid4(), uuid.uuid4()
    r._inflight[done_id], r._inflight[running_id] = _InFlight(None, task=done), _InFlight(None, task=running)

    def forbidden(*_a, **_k):
        raise AssertionError("_prune_done must not call the done callback")

    monkeypatch.setattr(r, "_on_done", forbidden)
    try:
        r._prune_done()
        assert list(r._inflight) == [running_id]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_the_done_callback_consumes_the_exception_and_logs_it_once(db, tmp_path, caplog):
    r = make_runner(Harness(db, tmp_path))
    task = await finished_task(ValueError("request failed"))
    rid = uuid.uuid4()
    r._inflight[rid] = _InFlight(None, task=task)
    with caplog.at_level(logging.ERROR, logger="app.services.runner"):
        r._on_done(rid, task)
    assert [rec for rec in caplog.records if "unexpected error" in rec.getMessage()].__len__() == 1
    assert rid not in r._inflight and task.exception() is not None    # retrieved: nothing is left "never retrieved"


async def test_a_callback_that_runs_after_the_entry_was_pruned_still_logs_once_and_wakes(db, tmp_path, caplog):
    r = make_runner(Harness(db, tmp_path))
    r._wake = CountingEvent()
    task = await finished_task(ValueError("request failed"))
    rid = uuid.uuid4()
    r._inflight[rid] = _InFlight(None, task=task)
    r._prune_done()                                                   # stop's settle got there first
    assert rid not in r._inflight and r._wake.set_calls == 0
    with caplog.at_level(logging.ERROR, logger="app.services.runner"):
        r._on_done(rid, task)                                         # the late done callback, called by the test
    assert len([rec for rec in caplog.records if "unexpected error" in rec.getMessage()]) == 1
    assert r._wake.set_calls == 1 and r._wake.is_set()
