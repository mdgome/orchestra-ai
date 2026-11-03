import asyncio
import os
import subprocess
import sys
import time

import psutil
import pytest

from app.infra.command_executor import CommandExecutor, CommandSpec, build_env
from app.infra.process_control import (
    CONFIRMED_GONE, ProcessIdentity, TerminationResult, capture_identity, check_alive, terminate_tree,
)
from app.infra.workspace_guard import WorkspaceGuard
from app.models.states import CliOutcome
from app.validation.exceptions import FailClosed

PY = sys.executable


@pytest.fixture
def guard(tmp_path):
    return WorkspaceGuard(tmp_path)


@pytest.fixture
def executor(guard):
    return CommandExecutor(guard)


def spec(tmp_path, code, **kw):
    return CommandSpec(argv=[PY, "-c", code], cwd=tmp_path, **kw)


async def test_success_captures_output_and_exit_code(executor, tmp_path):
    r = await executor.run(spec(tmp_path, "print('hi'); import sys; print('err', file=sys.stderr)"))
    assert r.outcome is CliOutcome.SUCCESS and r.exit_code == 0
    assert r.stdout.strip() == "hi" and "err" in r.stderr
    assert r.identity is not None


async def test_nonzero_exit_is_an_error_not_a_success(executor, tmp_path):
    r = await executor.run(spec(tmp_path, "import sys; sys.exit(3)"))
    assert r.outcome is CliOutcome.ERROR and r.exit_code == 3


async def test_missing_executable_fails_closed(executor, tmp_path):
    with pytest.raises(FailClosed):
        await executor.run(CommandSpec(argv=["definitely-not-a-real-binary-xyz"], cwd=tmp_path))


async def test_shell_string_is_rejected(executor, tmp_path):
    with pytest.raises(FailClosed):
        await executor.run(CommandSpec(argv=[], cwd=tmp_path))


async def test_timeout_kills_the_whole_process_tree(executor, tmp_path):
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open(r'{pidfile}', 'w').write(str(c.pid))\n"
        "time.sleep(120)\n"
    )
    r = await executor.run(spec(tmp_path, code, timeout_seconds=3))
    assert r.outcome is CliOutcome.TIMEOUT
    child_pid = int(pidfile.read_text())
    await asyncio.sleep(0.5)
    assert not psutil.pid_exists(child_pid), "grandchild must be cleaned up on timeout"
    assert r.identity is not None and check_alive(r.identity) in CONFIRMED_GONE


async def test_idle_output_detection(executor, tmp_path):
    r = await executor.run(spec(tmp_path, "import time; time.sleep(60)", idle_seconds=2, timeout_seconds=30))
    assert r.outcome is CliOutcome.IDLE


async def test_cancel_terminates_process(executor, tmp_path):
    task = asyncio.create_task(executor.run(spec(tmp_path, "import time; time.sleep(60)"), run_key="k1"))
    await asyncio.sleep(1.5)
    executor.cancel("k1")
    r = await asyncio.wait_for(task, timeout=15)
    assert r.outcome is CliOutcome.CANCELLED


async def test_on_started_receives_identity(executor, tmp_path):
    seen = []

    async def hook(identity):
        seen.append(identity)

    await executor.run(spec(tmp_path, "pass"), on_started=hook)
    assert len(seen) == 1 and seen[0].pid > 0 and seen[0].create_time > 0


def test_environment_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    env = build_env({"EXTRA": "1"})
    assert "SUPER_SECRET_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert env["EXTRA"] == "1" and "PATH" in {k.upper() for k in env}


async def test_child_does_not_receive_secrets(executor, tmp_path, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leak")
    r = await executor.run(spec(tmp_path, "import os; print(os.environ.get('SUPER_SECRET_TOKEN','absent'))"))
    assert r.stdout.strip() == "absent"


async def test_cwd_outside_workspace_is_rejected(executor, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    with pytest.raises(FailClosed):
        await executor.run(CommandSpec(argv=[PY, "-c", "pass"], cwd=outside))


def test_guard_rejects_dotdot_and_absolute_escape(guard, tmp_path):
    (tmp_path / "ok").mkdir()
    assert guard.resolve_inside("ok").name == "ok"
    with pytest.raises(FailClosed):
        guard.resolve_inside("../escape")
    with pytest.raises(FailClosed):
        guard.resolve_inside(os.path.abspath(os.sep))


def test_guard_rejects_junction_or_symlink_escape(guard, tmp_path, tmp_path_factory):
    target = tmp_path_factory.mktemp("target")
    link = tmp_path / "link"
    if sys.platform == "win32":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if r.returncode != 0:
            pytest.fail(f"cannot create junction for test: {r.stderr!r}")
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(FailClosed):
        guard.resolve_inside("link")
    with pytest.raises(FailClosed):
        guard.resolve_inside("link/inner")


# --- process identity -----------------------------------------------------------------
def _spawn_sleeper():
    return subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])


def test_terminate_tree_requires_matching_identity():
    p = _spawn_sleeper()
    try:
        real = capture_identity(p.pid)
        forged = ProcessIdentity(p.pid, real.create_time + 1000)  # same pid, different "process"
        assert check_alive(forged) is TerminationResult.IDENTITY_MISMATCH
        assert terminate_tree(forged, 1) is TerminationResult.IDENTITY_MISMATCH
        assert p.poll() is None, "process with a mismatching identity must NOT be killed"
        assert terminate_tree(real, 5) is TerminationResult.TERMINATED
        p.wait(timeout=5)
    finally:
        if p.poll() is None:
            p.kill()


def test_terminate_already_exited_is_confirmed_gone():
    p = subprocess.Popen([PY, "-c", "pass"])
    ident = capture_identity(p.pid)
    p.wait()
    time.sleep(0.2)
    assert ident is not None
    assert terminate_tree(ident, 1) in CONFIRMED_GONE


# --- process-slot (semaphore) cancellation safety ------------------------------------------------------------------------
SLEEP = ("import sys, time; s = time.time(); time.sleep(float(sys.argv[2])); "
         "open(sys.argv[1], 'w').write(f'{s} {time.time()}')")


def timed(tmp_path, name, seconds):
    return CommandSpec(argv=[PY, "-c", SLEEP, str(tmp_path / name), str(seconds)], cwd=tmp_path, idle_seconds=None)


def max_overlap(tmp_path, names) -> int:
    """The largest number of those commands that were really running at the same time (from their own clocks)."""
    points = []
    for name in names:
        start, end = (float(x) for x in (tmp_path / name).read_text().split())
        points += [(start, 1), (end, -1)]
    best = running = 0
    for _t, delta in sorted(points):
        running += delta
        best = max(best, running)
    return best


async def spin(times=20):
    for _ in range(times):
        await asyncio.sleep(0)


async def assert_capacity(executor, tmp_path, capacity, tag="cap"):
    """Behavioural check: exactly `capacity` commands run concurrently afterwards (no permit lost, none invented)."""
    names = [f"{tag}-{i}" for i in range(capacity * 2)]
    results = await asyncio.gather(*(executor.run(timed(tmp_path, n, 0.8)) for n in names))
    assert all(r.outcome is CliOutcome.SUCCESS for r in results)
    assert max_overlap(tmp_path, names) == capacity


async def test_cancelling_a_command_that_waits_for_a_slot_loses_no_permit(guard, tmp_path):
    executor = CommandExecutor(guard, max_concurrent=2)
    holders = [asyncio.create_task(executor.run(timed(tmp_path, f"hold-{i}", 1.0))) for i in range(2)]
    await spin()                                                        # both slots are taken
    waiting = asyncio.create_task(executor.run(timed(tmp_path, "never", 0.1)))
    await spin()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert all(r.outcome is CliOutcome.SUCCESS for r in await asyncio.gather(*holders))
    assert not (tmp_path / "never").exists()                            # it never ran
    await assert_capacity(executor, tmp_path, 2)


async def test_a_second_cancel_during_cleanup_is_reraised_only_after_the_permit_is_settled(guard, tmp_path, monkeypatch):
    executor = CommandExecutor(guard, max_concurrent=1)
    original = executor._settle                                         # already bound: saved BEFORE patching
    entered, proceed, calls, seen = asyncio.Event(), asyncio.Event(), [], []

    async def wrapper(*args, **kw):                                     # instance attribute: called without `self`
        calls.append(1)
        seen.extend(args)
        entered.set()
        await proceed.wait()
        return await original(*args, **kw)                              # the real cleanup, exactly once

    holder = asyncio.create_task(executor.run(timed(tmp_path, "hold", 1.0)))
    await spin()                                                        # the holder owns the slot (its own acquire is done)
    monkeypatch.setattr(executor, "_settle", wrapper)                   # patch only now: observe the WAITER's cleanup
    waiting = asyncio.create_task(executor.run(timed(tmp_path, "never", 0.1)))
    await spin()
    waiting.cancel()                                                    # first cancellation: cleanup starts
    await asyncio.wait_for(entered.wait(), 10)
    waiting.cancel()                                                    # second cancellation: cleanup NOT finished yet
    await spin()
    assert not waiting.done()                                           # still cleaning up, nothing re-raised yet
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert len(calls) == 1 and all(t.done() for t in seen)              # both helper tasks really ended
    assert (await holder).outcome is CliOutcome.SUCCESS
    monkeypatch.undo()
    assert "_settle" not in vars(executor)                              # the patch is restored automatically
    await assert_capacity(executor, tmp_path, 1)


async def test_settle_can_also_be_patched_on_the_class_with_self(guard, tmp_path, monkeypatch):
    """Same scenario through descriptor binding: the wrapper receives `self` and still calls the original once."""
    executor = CommandExecutor(guard, max_concurrent=1)
    original = CommandExecutor._settle
    calls = []

    async def wrapper(self, acquire, waiter):
        calls.append(self)
        return await original(self, acquire, waiter)

    holder = asyncio.create_task(executor.run(timed(tmp_path, "hold", 0.8)))
    await spin()
    monkeypatch.setattr(CommandExecutor, "_settle", wrapper)
    waiting = asyncio.create_task(executor.run(timed(tmp_path, "never", 0.1), run_key="w"))
    await spin()
    executor.cancel("w")
    result = await waiting
    assert result.outcome is CliOutcome.CANCELLED and calls == [executor]
    assert (await holder).outcome is CliOutcome.SUCCESS
    await assert_capacity(executor, tmp_path, 1)


async def test_a_permit_and_a_cancel_that_complete_together_never_spawn_and_return_the_permit_once(guard, tmp_path):
    executor = CommandExecutor(guard, max_concurrent=1)
    await executor._slots.acquire()                                     # setup only: occupy the single slot
    started = []

    async def on_started(_identity):
        started.append(1)

    waiting = asyncio.create_task(executor.run(timed(tmp_path, "never", 0.1), run_key="w", on_started=on_started))
    await spin()
    executor._slots.release()                                           # the permit becomes available ...
    executor.cancel("w")                                                # ... in the same instant as the cancellation
    result = await waiting
    assert result.outcome is CliOutcome.CANCELLED and result.exit_code is None and started == []
    assert not (tmp_path / "never").exists()
    await assert_capacity(executor, tmp_path, 1)                        # behaviour: exactly one permit exists


async def test_repeated_cancellation_keeps_the_full_capacity(guard, tmp_path):
    executor = CommandExecutor(guard, max_concurrent=2)
    holders = [asyncio.create_task(executor.run(timed(tmp_path, f"hold-{i}", 1.5))) for i in range(2)]
    await spin()
    for round_no in range(20):
        waiting = asyncio.create_task(executor.run(timed(tmp_path, f"never-{round_no}", 0.1), run_key=f"w{round_no}"))
        await spin()
        if round_no % 2:
            waiting.cancel()                                            # external cancellation
            with pytest.raises(asyncio.CancelledError):
                await waiting
        else:
            executor.cancel(f"w{round_no}")                             # cancel event
            assert (await waiting).outcome is CliOutcome.CANCELLED
    assert all(r.outcome is CliOutcome.SUCCESS for r in await asyncio.gather(*holders))
    assert not list(tmp_path.glob("never-*"))
    assert executor._cancel_events == {}                                # nothing is left registered
    await assert_capacity(executor, tmp_path, 2)


@pytest.mark.parametrize("max_concurrent", [None, 1])
async def test_a_cancel_that_is_already_pending_never_spawns(guard, tmp_path, monkeypatch, max_concurrent):
    executor = CommandExecutor(guard, max_concurrent=max_concurrent)

    class PreCancelled(dict):
        def __setitem__(self, key, event):
            event.set()                                                 # the cancellation is pending from the start
            super().__setitem__(key, event)

    monkeypatch.setattr(executor, "_cancel_events", PreCancelled())
    started = []

    async def on_started(_identity):
        started.append(1)

    result = await executor.run(timed(tmp_path, "never", 0.1), run_key="k", on_started=on_started)
    assert result.outcome is CliOutcome.CANCELLED and result.exit_code is None and started == []
    assert not (tmp_path / "never").exists() and executor._cancel_events == {}
    monkeypatch.undo()
    if max_concurrent:
        await assert_capacity(executor, tmp_path, max_concurrent)


async def test_the_normal_path_releases_each_permit_exactly_once(guard, tmp_path):
    executor = CommandExecutor(guard, max_concurrent=1)
    for i in range(5):
        assert (await executor.run(timed(tmp_path, f"seq-{i}", 0.05))).outcome is CliOutcome.SUCCESS
    await assert_capacity(executor, tmp_path, 1)                        # a double release would let two run at once


async def test_a_failing_command_still_returns_its_permit(guard, tmp_path):
    executor = CommandExecutor(guard, max_concurrent=1)
    bad = await executor.run(CommandSpec(argv=[PY, "-c", "import sys; sys.exit(3)"], cwd=tmp_path))
    assert bad.outcome is CliOutcome.ERROR
    await assert_capacity(executor, tmp_path, 1)
