"""Runner lifecycle: state transitions, strict recovery gating, the settle contract of stop() / failed start() (resource
based final transition, cancellation safety, exception priority), connection ownership and the `_prune_done` /
done-callback split. Waiting is done with Events and controllable futures, never with sleep lengths."""
import asyncio
import inspect
import logging
import sys
import uuid
from types import SimpleNamespace
from typing import NamedTuple

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


# --- best-effort cleanup for tests that end with a failing assertion ----------------------------------------------------------
class Entry(NamedTuple):
    """One Runner a test's `finally` has to clean up."""
    name: str
    runner: Runner
    stop_task: asyncio.Task | None = None           # the task that ran the original Runner.stop() call (None: awaited directly)
    expected: tuple[type, ...] = ()                 # exception types the test body verifies for that task
    observed: dict | None = None                    # shared with the body: {"stop_result": bool, "exception": exception | None}
    settle_finished: asyncio.Event | None = None    # only says that _settle_stop ran to its end; never decides the cleanup


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def lock_returned(db) -> None:
    ensure(await asyncio.wait_for(lock_free(db), 10), "the advisory lock was not returned")


def _verified(entry: Entry, exc: BaseException | None) -> bool:
    """True only when the test body finished verifying the stop task's outcome AND it is of an expected type."""
    seen = entry.observed
    return bool(seen and seen.get("stop_result") is True and exc is not None and isinstance(exc, entry.expected))


async def stop_if_needed(entry: Entry) -> None:
    """Brings one Runner to STOPPED. The stop task (if any) is dealt with first, so that a STOPPED Runner can never hide
    an unverified cancellation or error of the original stop() call. No sleeps, no polling."""
    task, runner = entry.stop_task, entry.runner
    if task is not None:
        if not task.done():
            await asyncio.wait({task}, timeout=10)                    # bounded, and it never cancels the task
            if not task.done():
                raise RuntimeError(f"the stop task of {entry.name} did not finish")
        if task.cancelled():                                          # the original CancelledError cannot be read back
            if not _verified(entry, (entry.observed or {}).get("exception")):
                error = RuntimeError("stop task was cancelled before its result was verified")
                error.add_note("the stop task was in the cancelled state")
                raise error
        else:
            exc = task.exception()
            if exc is None:
                task.result()
            elif not _verified(entry, exc):
                raise exc
    if runner.lifecycle in (L.RUNNING, L.STOP_INCOMPLETE):
        await asyncio.wait_for(runner.stop(), 10)
    elif runner.lifecycle is L.STOPPING:
        raise RuntimeError(f"{entry.name} is stuck in STOPPING although its stop task ended" if task is not None
                           else "cleanup impossible: STOPPING without its stop task")
    elif runner.lifecycle is not L.STOPPED:                           # STARTING (or anything unknown)
        raise RuntimeError(f"cannot clean up {entry.name} from {runner.lifecycle}")
    ensure(runner.lifecycle is L.STOPPED, f"{entry.name} is {runner.lifecycle} after its cleanup")


async def cleanup_and_verify(entries: list[Entry], checks: list, body_error: BaseException | None) -> None:
    """Runs in a test's `finally`. Every Runner is cleaned up even if another cleanup fails; the checks (lock returned,
    resources gone, ...) run only when every cleanup succeeded, each on its own. Nothing is raised when all is well (the
    body's own error then keeps its traceback); otherwise the body error and all cleanup / verification errors travel
    together. `checks` are (label, callable) pairs; a callable raises to fail and may return an awaitable."""
    cleanup_errors, verification_errors, done = [], [], set()
    for entry in entries:
        if id(entry.runner) in done:                                  # a duplicate would drop its stop_task information
            cleanup_errors.append(RuntimeError(f"duplicate cleanup entry for runner '{entry.name}'"))
            continue
        done.add(id(entry.runner))
        try:
            await stop_if_needed(entry)
        except BaseException as exc:  # noqa: BLE001 - CancelledError included: one failing cleanup must not skip the rest
            exc.add_note(f"cleanup of {entry.name} failed")
            cleanup_errors.append(exc)
    if not cleanup_errors:
        for label, check in checks:
            try:
                result = check()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:  # noqa: BLE001
                exc.add_note(f"verification '{label}' failed")
                verification_errors.append(exc)
    problems = [*cleanup_errors, *verification_errors]
    if not problems:
        return
    if body_error is None and len(problems) == 1:
        raise problems[0]
    raise BaseExceptionGroup("runner cleanup / verification failed",
                             [*([body_error] if body_error is not None else []), *problems])


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
    real = r._recovery_body

    async def spy(startup, session, evidence):
        summary = await real(startup, session, evidence)
        seen.setdefault("first", (startup, summary["requeued"]))
        return summary

    monkeypatch.setattr(r, "_recovery_body", spy)
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
    # `asyncio.shield` is called in exactly one place of the Runner: `_wait_shielded`, which stop() awaits. A probe around
    # it (installed only while this test's stop runs, and acting only for the stop task) shows WHERE each cancellation is
    # consumed, so the production `_wait_shielded` is exercised unchanged and no sleep or yield is needed to order the
    # two cancellations.
    original_shield = asyncio.shield
    holder, shield_calls, seen, unexpected_calls = {}, [], [], []
    shield_started = [asyncio.Event() for _ in range(3)]              # the 1st / 2nd / 3rd wait of stop() on its settle task
    consumed = [asyncio.Event() for _ in range(2)]                    # the 1st / 2nd CancelledError reached `_wait_shielded`
    observed = {"stop_result": False, "exception": None}
    stopping = drain_proceed = finished = None

    async def probe(fut):
        mine = asyncio.current_task() is holder.get("stop")
        if mine:
            n = len(shield_calls)
            shield_calls.append(n)
            if n < len(shield_started):
                shield_started[n].set()
            else:
                unexpected_calls.append(n)                           # recorded only: the probe never raises by itself
        try:
            return await original_shield(fut)                        # the original result / exception, unchanged
        except asyncio.CancelledError as exc:
            if mine and len(seen) < len(consumed):
                seen.append(exc)
                consumed[len(seen) - 1].set()
            raise                                                    # `_wait_shielded` catches it in this same task step

    try:
        await r.start()
        _entered, finished, settle_calls = track(r, "_settle_stop", monkeypatch)
        drain_entered, drain_proceed = stalled(r, "_drain", monkeypatch)
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "shield", probe)
            try:
                stopping = asyncio.create_task(r.stop())
                holder["stop"] = stopping
                await wait(drain_entered)
                assert r.lifecycle is L.STOPPING                     # observable only while the settle task runs
                await wait(shield_started[0])
                assert shield_calls == [0]                           # stop() is waiting on its settle task
                stopping.cancel()                                    # first cancellation
                await wait(consumed[0])
                # The probe re-raised inside the stop task, whose `_wait_shielded` caught the error and started its next
                # `shield` in the same task step; the test only runs after that, so the 2nd wait must already exist.
                await wait(shield_started[1])
                assert stopping.cancelling() >= 1                    # auxiliary only
                stopping.cancel()                                    # second cancellation: sent only after that proof
                await wait(consumed[1])
                await wait(shield_started[2])                        # and stop() is still waiting for the settle task
                assert not finished.is_set() and not stopping.done()
                drain_proceed.set()
                with pytest.raises(asyncio.CancelledError) as info:
                    await stopping
                assert info.value is seen[0]                         # stop() raises the FIRST CancelledError it consumed
                observed["stop_result"], observed["exception"] = True, info.value
            finally:
                drain_proceed.set()
                if stopping is not None:
                    await asyncio.gather(stopping, return_exceptions=True)
        assert asyncio.shield is original_shield                     # the probe is gone again
        assert len(seen) == 2 and all(e.is_set() for e in consumed)
        assert settle_calls == [1] and finished.is_set()             # the settle task ran exactly once, to its end
        assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)      # nothing left behind
        assert await lock_free(db)                                   # ... so the second cancellation did not stop it
    finally:
        body_error = sys.exc_info()[1]
        if drain_proceed is not None:
            drain_proceed.set()
        await cleanup_and_verify(
            [Entry("main r", r, stop_task=stopping, expected=(asyncio.CancelledError,), observed=observed,
                   settle_finished=finished)],
            [("no runtime resources left", lambda: ensure(resources(r) == (False, False, 0), f"left: {resources(r)}")),
             ("stop task waited exactly three times", lambda: ensure(shield_calls == [0, 1, 2], f"{shield_calls}")),
             ("no unexpected shield call", lambda: ensure(not unexpected_calls, f"{unexpected_calls}")),
             ("the shield probe was removed", lambda: ensure(asyncio.shield is original_shield, "still patched")),
             ("advisory lock returned", lambda: lock_returned(db))],
            body_error)


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
    extra: list[Entry] = []                                           # every other Runner this test creates, in order
    finished = None

    async def stubborn(request, *, on_lease=None):
        started.set()
        while not release.is_set():                                  # swallows cancellation until released
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    monkeypatch.setattr(r, "process", stubborn)
    try:
        task = await h.new_task()
        await r.start()
        await h.tasks.request_execution(task.id)
        r.notify()
        await asyncio.wait_for(started.wait(), 10)
        _entered, finished, _calls = track(r, "_settle_stop", monkeypatch)
        with pytest.raises(RunnerStopIncomplete):
            await r.stop()                                            # awaited directly: its outcome is settled right here
        assert finished.is_set()                                      # the settle task itself ended ...
        assert r.lifecycle is L.STOP_INCOMPLETE and resources(r) == (False, True, 1)     # ... resources may remain
        before = await requests(h)
        contender = make_runner(h)
        extra.append(Entry("contender", contender))
        with pytest.raises(RunnerAlreadyActive):
            await contender.start()                                   # the advisory lock is still held
        with pytest.raises(RuntimeError):
            await r.start()
        for startup in (True, False):
            with pytest.raises(RuntimeError):
                await r.recover(startup=startup)
        assert await requests(h) == before                            # claim token / status untouched
        release.set()
        await asyncio.wait_for(r.stop(), 10)                          # the task ended: now it succeeds
        assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0)
        again = make_runner(h)
        extra.append(Entry("again", again))
        await again.start()                                           # the lock was returned
        await again.stop()
    finally:
        body_error = sys.exc_info()[1]
        release.set()                                                 # first: the stubborn task may end
        await cleanup_and_verify(
            [*extra, Entry("main r", r, settle_finished=finished)],   # `r` last, whatever happened to the others
            [("no runtime resources left", lambda: ensure(resources(r) == (False, False, 0), f"left: {resources(r)}")),
             ("advisory lock returned", lambda: lock_returned(db))],
            body_error)


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


async def test_stop_does_not_wait_for_a_recovery_that_is_inside_its_db_transaction(db, tmp_path, monkeypatch):
    """The lifecycle lock is not held during recovery DB I/O: stop() reaches STOPPING at once, cancels the operation,
    which rolls back and releases its connection, and the Runner ends STOPPED."""
    h = Harness(db, tmp_path)
    r = make_runner(h)
    entered, proceed = stalled(r, "_recovery_body", monkeypatch, only_periodic=True)
    await r.start()                                                   # the loop's first periodic recovery stalls
    await wait(entered)
    try:
        assert r._recovery is not None and not r._lifecycle_lock.locked()
        stopping = asyncio.create_task(r.stop())
        for _ in range(200):
            await asyncio.sleep(0)
            if r.lifecycle is L.STOPPING:
                break
        assert r.lifecycle is L.STOPPING                              # stop() took the lock although recovery is live
        await asyncio.wait_for(stopping, 10)
    finally:
        proceed.set()
    assert r.lifecycle is L.STOPPED and resources(r) == (False, False, 0) and r._recovery_clean()


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
