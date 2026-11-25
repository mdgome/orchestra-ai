"""B2 phase 2: the tracked-execution protocol and settlement state machine of CommandExecutor, with a fake Job.

The fake owns no Windows API: `terminate` kills the real root through psutil and `active_processes` follows it, so the
state machine (design 4, 4.1, 5.1, 9) is exercised on any platform. Real Job Object behaviour is in test_windows_job.py."""
import asyncio
import sys
import time

import psutil
import pytest

from app.infra import command_executor as ce
from app.infra.command_executor import CommandExecutor, CommandSpec, TrackedRun
from app.infra.process_control import (
    IdentityFenced, JobError, SelfTestResult, TrackingState, TreeJob, run_tracking_self_test,
)
from app.infra.workspace_guard import WorkspaceGuard
from app.models.states import CliOutcome, ProcessLifecycle, TreeProof
from app.validation import policies as P
from app.validation.exceptions import FailClosed

PY = sys.executable
SLEEPER = "import time; time.sleep(120)"
WIN = sys.platform == "win32"


class FakeJob(TreeJob):
    """A TreeJob that records every call. Default behaviour mimics a Job over the ONE root process it was given."""

    def __init__(self, *, assign_error=None, resume_error=None, terminate_error=None, query_error=None,
                 unkillable=False, counts=None):
        self.calls: list[str] = []
        self.pid = None
        self.terminated = False
        self.closed = False
        self._assign_error, self._resume_error = assign_error, resume_error
        self._terminate_error, self._query_error = terminate_error, query_error
        self._unkillable, self._counts = unkillable, counts

    # TreeJob
    def assign(self, pid):
        self.calls.append("assign")
        if self._assign_error:
            raise self._assign_error
        self.pid = pid

    def resume(self, pid):
        self.calls.append("resume")
        if self._resume_error:
            raise self._resume_error
        if WIN:
            from app.infra.windows_job import resume_primary_thread
            resume_primary_thread(pid)

    def terminate(self, exit_code):
        self.calls.append("terminate")
        if self._terminate_error:
            raise self._terminate_error
        if self._unkillable:
            return
        self.terminated = True
        self._kill_real()

    def active_processes(self):
        self.calls.append("query")
        if self._query_error:
            raise self._query_error
        if self._counts is not None:
            return self._counts(self)
        if self.terminated or self.pid is None:
            return 0
        return 1 if self._root_alive() else 0

    def close(self):
        self.calls.append("close")
        self.closed = True

    # helpers
    def _root_alive(self):
        try:
            p = psutil.Process(self.pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def _kill_real(self):
        if self.pid is None:          # psutil.Process(None) is the CURRENT process: never kill that
            return
        try:
            root = psutil.Process(self.pid)
            for p in [*root.children(recursive=True), root]:
                try:
                    p.kill()
                except psutil.Error:
                    pass
        except psutil.Error:
            pass

    def cleanup(self):
        self._kill_real()


def make(tmp_path, job, **kw):
    return CommandExecutor(WorkspaceGuard(tmp_path), job_factory=lambda: job, **kw)


def spec(tmp_path, code, **kw):
    kw.setdefault("idle_seconds", None)
    return CommandSpec(argv=[PY, "-c", code], cwd=tmp_path, **kw)


@pytest.fixture(autouse=True)
def fast_constants(monkeypatch):
    monkeypatch.setattr(P, "PROCESS_TERMINATE_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(P, "TREE_SETTLE_POLL_SECONDS", 0.3)
    monkeypatch.setattr(P, "PUMP_DRAIN_SECONDS", 1.0)


# --- the protocol order (5.1) -----------------------------------------------------------------------------------------
async def test_a_natural_exit_is_exited_with_an_empty_tree_and_no_termination(tmp_path):
    job = FakeJob()
    tracked = TrackedRun("e1")
    r = await make(tmp_path, job).run(spec(tmp_path, "print('hi')"), tracked=tracked)
    assert (r.outcome, r.lifecycle, r.tree, r.termination) == (CliOutcome.SUCCESS, ProcessLifecycle.EXITED,
                                                              TreeProof.EMPTY, None)
    assert r.exit_code == 0 and r.stdout.strip() == "hi" and not r.survivors and not r.spawn_failed
    assert job.calls[:2] == ["assign", "resume"] and "terminate" not in job.calls   # nothing to kill
    assert job.closed                                                               # closed after it was proven empty
    assert tracked.spawned and tracked.result is r and tracked.degraded is None


async def test_the_identity_is_persisted_between_assignment_and_resume(tmp_path):
    job, order = FakeJob(), []

    async def on_started(identity):
        order.extend(["persist", *job.calls])
        assert identity is not None and identity.pid == job.pid

    await make(tmp_path, job).run(spec(tmp_path, "pass"), tracked=TrackedRun("e"), on_started=on_started)
    assert order == ["persist", "assign"]                    # assigned already, NOT resumed yet (I4)
    assert job.calls.index("resume") > job.calls.index("assign")


async def test_a_non_zero_root_exit_is_exited_with_outcome_error(tmp_path):
    r = await make(tmp_path, FakeJob()).run(spec(tmp_path, "import sys; sys.exit(3)"), tracked=TrackedRun("e"))
    assert (r.outcome, r.lifecycle, r.exit_code) == (CliOutcome.ERROR, ProcessLifecycle.EXITED, 3)


async def test_a_root_that_exited_is_a_natural_exit_never_a_timeout(tmp_path):
    """The returncode check precedes the timeout check of the same tick, also with timeout_seconds=0."""
    r = await make(tmp_path, FakeJob()).run(spec(tmp_path, "pass", timeout_seconds=0), tracked=TrackedRun("e"))
    # the very first tick may legitimately see a still-running root; what is forbidden is TIMEOUT for an exited root
    assert r.outcome in (CliOutcome.SUCCESS, CliOutcome.TIMEOUT)
    r = await make(tmp_path, FakeJob()).run(spec(tmp_path, "import time; time.sleep(0.5)", timeout_seconds=30),
                                            tracked=TrackedRun("e"))
    assert r.outcome is CliOutcome.SUCCESS and r.lifecycle is ProcessLifecycle.EXITED


async def test_survivors_after_a_natural_exit_are_terminated_before_the_drain(tmp_path):
    job = FakeJob(counts=lambda j: 0 if j.terminated else 2)
    r = await make(tmp_path, job).run(spec(tmp_path, "print('x')"), tracked=TrackedRun("e"))
    assert (r.lifecycle, r.survivors, r.termination, r.outcome, r.tree) == (
        ProcessLifecycle.EXITED, True, "TERMINATED", CliOutcome.SUCCESS, TreeProof.EMPTY)
    assert job.calls.count("terminate") == 1 and job.closed


async def test_a_transient_helper_is_not_a_survivor(tmp_path):
    """ActiveProcesses is re-polled for TREE_SETTLE_POLL_SECONDS: a count that drops to 0 inside the window is no survivor."""
    seen = []

    def counts(job):
        seen.append(1)
        return 1 if len(seen) < 3 else 0

    job = FakeJob(counts=counts)
    r = await make(tmp_path, job).run(spec(tmp_path, "pass"), tracked=TrackedRun("e"))
    assert r.lifecycle is ProcessLifecycle.EXITED and r.survivors is False and r.termination is None
    assert "terminate" not in job.calls


async def test_timeout_and_idle_terminate_the_job(tmp_path):
    job = FakeJob()
    r = await make(tmp_path, job).run(spec(tmp_path, SLEEPER, timeout_seconds=1), tracked=TrackedRun("e"))
    assert (r.outcome, r.lifecycle, r.termination, r.tree) == (CliOutcome.TIMEOUT, ProcessLifecycle.TERMINATED,
                                                              "TERMINATED", TreeProof.EMPTY)
    job = FakeJob()
    r = await make(tmp_path, job).run(spec(tmp_path, SLEEPER, idle_seconds=1, timeout_seconds=60), tracked=TrackedRun("e"))
    assert (r.outcome, r.lifecycle, r.termination) == (CliOutcome.IDLE, ProcessLifecycle.TERMINATED, "TERMINATED")


async def test_cancel_by_key_terminates_the_job(tmp_path):
    job, ex = FakeJob(), None
    ex = make(tmp_path, job)
    task = asyncio.create_task(ex.run(spec(tmp_path, SLEEPER), tracked=TrackedRun("e", run_key="run:tracked:s:1")))
    await asyncio.sleep(1.0)
    ex.cancel("run")                       # a prefix of the run key reaches it
    r = await asyncio.wait_for(task, 20)
    assert (r.outcome, r.lifecycle) == (CliOutcome.CANCELLED, ProcessLifecycle.TERMINATED)
    assert ex._cancel_events == {}


# --- §4 / §4.1 golden table -------------------------------------------------------------------------------------------
GOLDEN = [
    # id, job kwargs, spec code/kwargs, (lifecycle, termination)
    ("natural-empty", {}, ("pass", {}), (ProcessLifecycle.EXITED, None)),
    ("natural-survivors", {"counts": lambda j: 0 if j.terminated else 2}, ("pass", {}),
     (ProcessLifecycle.EXITED, "TERMINATED")),
    ("timeout", {}, (SLEEPER, {"timeout_seconds": 1}), (ProcessLifecycle.TERMINATED, "TERMINATED")),
    ("still-active", {"unkillable": True}, (SLEEPER, {"timeout_seconds": 1}),
     (ProcessLifecycle.TERMINATION_UNCONFIRMED, "STILL_ALIVE")),
    ("terminate-denied", {"terminate_error": JobError("JOB_TERMINATE_FAILED", access_denied=True)},
     (SLEEPER, {"timeout_seconds": 1}), (ProcessLifecycle.TERMINATION_UNCONFIRMED, "ACCESS_DENIED")),
    ("query-denied", {"query_error": JobError("JOB_QUERY_FAILED", access_denied=True)},
     (SLEEPER, {"timeout_seconds": 1}), (ProcessLifecycle.TERMINATION_UNCONFIRMED, "ACCESS_DENIED")),
    ("query-failed", {"query_error": JobError("JOB_QUERY_FAILED")}, (SLEEPER, {"timeout_seconds": 1}),
     (ProcessLifecycle.TERMINATION_UNCONFIRMED, "UNKNOWN")),
    ("handle-lost", {"query_error": JobError("JOB_HANDLE_LOST")}, ("pass", {}),
     (ProcessLifecycle.TERMINATION_UNCONFIRMED, "UNKNOWN")),
    ("spawn-failed", {"assign_error": JobError("JOB_ASSIGN_FAILED")}, ("pass", {}),
     (ProcessLifecycle.SPAWN_FAILED, None)),
]


@pytest.mark.parametrize("name,job_kw,command,expected", GOLDEN, ids=[g[0] for g in GOLDEN])
async def test_termination_string_table_of_the_design(tmp_path, name, job_kw, command, expected):
    job = FakeJob(**job_kw)
    code, kw = command
    try:
        r = await make(tmp_path, job).run(spec(tmp_path, code, **kw), tracked=TrackedRun("e"))
    finally:
        job.cleanup()
    assert (r.lifecycle, r.termination) == expected


async def test_untracked_termination_vocabulary_is_unchanged(tmp_path, monkeypatch):
    ex = CommandExecutor(WorkspaceGuard(tmp_path))
    r = await ex.run(spec(tmp_path, SLEEPER, timeout_seconds=1))
    assert (r.outcome, r.termination, r.tree) == (CliOutcome.TIMEOUT, "TERMINATED", TreeProof.UNKNOWN)
    monkeypatch.setattr(ce, "capture_identity", lambda pid: None)                       # no identity: the Popen fallback
    r = await ex.run(spec(tmp_path, SLEEPER, timeout_seconds=1))
    assert (r.outcome, r.termination) == (CliOutcome.TIMEOUT, "KILLED_BY_HANDLE")
    monkeypatch.undo()

    async def late_timeout(self_run, spec_, cancel_event):                              # the root is gone before the kill
        while self_run.proc.returncode is None:
            await asyncio.sleep(0.05)
        self_run.decide("TIMEOUT")

    monkeypatch.setattr(ex, "_wait_phase", late_timeout)
    r = await ex.run(spec(tmp_path, "pass"))
    assert (r.outcome, r.termination) == (CliOutcome.TIMEOUT, "ALREADY_EXITED")


# --- TERMINATION_UNCONFIRMED, retained handle, slot --------------------------------------------------------------------
async def test_a_job_that_stays_active_is_unconfirmed_retained_and_releases_the_slot(tmp_path):
    job = FakeJob(unkillable=True)
    ex = make(tmp_path, job, max_concurrent=1)
    try:
        r = await ex.run(spec(tmp_path, SLEEPER, timeout_seconds=1), tracked=TrackedRun("exec-7"))
        assert (r.outcome, r.lifecycle, r.termination, r.tree) == (
            CliOutcome.TIMEOUT, ProcessLifecycle.TERMINATION_UNCONFIRMED, "STILL_ALIVE", TreeProof.NOT_EMPTY)
        assert not job.closed and ex.unconfirmed_job_count() == 1 and ex.retained_job("exec-7") is job
        assert ex._slots._value == 1                                    # the permit is back although the run is unconfirmed
        job._unkillable = False                                          # later the OS lets us kill it
        assert await ex.resettle_retained("exec-7") is True
        assert job.closed and ex.unconfirmed_job_count() == 0
        assert await ex.resettle_retained("exec-7") is False
    finally:
        job.cleanup()


async def test_a_cleanup_error_becomes_unconfirmed_never_swallowed(tmp_path, monkeypatch):
    job = FakeJob()
    ex = make(tmp_path, job)

    async def boom(_run):
        raise RuntimeError("cleanup bug")

    monkeypatch.setattr(ex, "_settle_tracked", boom)
    tracked = TrackedRun("e")
    try:
        r = await ex.run(spec(tmp_path, "pass"), tracked=tracked)
        assert (r.lifecycle, r.termination, r.reason) == (ProcessLifecycle.TERMINATION_UNCONFIRMED, "UNKNOWN",
                                                          "CLEANUP_ERROR")
        assert tracked.result is r and ex.unconfirmed_job_count() == 1
    finally:
        job.cleanup()


# --- spawn-phase failures: the command never ran ------------------------------------------------------------------------
@pytest.mark.parametrize("name,kw,reason", [
    ("assign", {"assign_error": JobError("JOB_ASSIGN_FAILED", "denied", access_denied=True)}, "JOB_ASSIGN_FAILED"),
    ("resume", {"resume_error": JobError("RESUME_FAILED", "two threads")}, "RESUME_FAILED"),
])
async def test_job_assignment_or_resume_failure_is_spawn_failed(tmp_path, name, kw, reason):
    job = FakeJob(**kw)
    tracked = TrackedRun("e")
    r = await make(tmp_path, job).run(spec(tmp_path, "pass"), tracked=tracked)
    assert (r.lifecycle, r.spawn_failed, r.tree, r.termination, r.reason) == (
        ProcessLifecycle.SPAWN_FAILED, True, TreeProof.EMPTY, None, reason)
    assert r.outcome is CliOutcome.ERROR and r.exit_code is None and tracked.spawned and tracked.result is r
    assert job.closed


async def test_job_creation_failure_spawns_nothing(tmp_path):
    def factory():
        raise JobError("JOB_CREATE_FAILED", "no job")

    tracked = TrackedRun("e")
    ex = CommandExecutor(WorkspaceGuard(tmp_path), job_factory=factory)
    r = await ex.run(spec(tmp_path, "open('ran','w').write('x')"), tracked=tracked)
    assert (r.lifecycle, r.reason, r.spawn_failed) == (ProcessLifecycle.SPAWN_FAILED, "JOB_CREATE_FAILED", True)
    assert tracked.spawned is False and tracked.result is r and not (tmp_path / "ran").exists()


async def test_identity_unavailable_is_spawn_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "capture_identity", lambda pid: None)
    job = FakeJob()
    r = await make(tmp_path, job).run(spec(tmp_path, "pass"), tracked=TrackedRun("e"))
    assert (r.lifecycle, r.reason) == (ProcessLifecycle.SPAWN_FAILED, "IDENTITY_UNAVAILABLE")
    assert "resume" not in job.calls


@pytest.mark.parametrize("exc,reason", [(RuntimeError("db down"), "IDENTITY_PERSIST_FAILED"),
                                        (IdentityFenced("lost lease"), "IDENTITY_FENCED")])
async def test_on_started_failure_of_a_tracked_command_is_spawn_failed(tmp_path, exc, reason):
    job = FakeJob()

    async def on_started(identity):
        raise exc

    r = await make(tmp_path, job).run(spec(tmp_path, "pass"), tracked=TrackedRun("e"), on_started=on_started)
    assert (r.lifecycle, r.reason, r.outcome) == (ProcessLifecycle.SPAWN_FAILED, reason, CliOutcome.ERROR)
    assert "resume" not in job.calls


async def test_on_started_timeout_is_bounded_by_the_persist_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS", 0.5)
    job = FakeJob()

    async def on_started(identity):
        await asyncio.sleep(30)

    t0 = time.monotonic()
    r = await make(tmp_path, job).run(spec(tmp_path, "pass"), tracked=TrackedRun("e"), on_started=on_started)
    assert (r.lifecycle, r.reason) == (ProcessLifecycle.SPAWN_FAILED, "IDENTITY_PERSIST_FAILED")
    assert time.monotonic() - t0 < 10 and "resume" not in job.calls


async def test_a_cancel_during_the_persist_is_a_cancelled_spawn_failure(tmp_path):
    job = FakeJob()
    ex = make(tmp_path, job)
    entered = asyncio.Event()

    async def on_started(identity):
        entered.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(ex.run(spec(tmp_path, "pass"), run_key="k", tracked=TrackedRun("e"),
                                      on_started=on_started))
    await asyncio.wait_for(entered.wait(), 10)
    ex.cancel("k")
    r = await asyncio.wait_for(task, 20)
    assert (r.outcome, r.lifecycle, r.reason) == (CliOutcome.CANCELLED, ProcessLifecycle.SPAWN_FAILED,
                                                  "CANCELLED_BEFORE_RESUME")
    assert "resume" not in job.calls


async def test_a_coroutine_cancel_during_the_persist_settles_then_reraises(tmp_path):
    job = FakeJob()
    ex = make(tmp_path, job)
    entered = asyncio.Event()
    tracked = TrackedRun("e")

    async def on_started(identity):
        entered.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(ex.run(spec(tmp_path, "pass"), tracked=tracked, on_started=on_started))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tracked.result is not None and tracked.result.lifecycle is ProcessLifecycle.SPAWN_FAILED
    assert "resume" not in job.calls and job.closed


async def test_a_missing_executable_fails_closed_before_any_tracked_state(tmp_path):
    tracked = TrackedRun("e")
    with pytest.raises(FailClosed):
        await make(tmp_path, FakeJob()).run(CommandSpec(argv=["no-such-binary-xyz"], cwd=tmp_path), tracked=tracked)
    assert tracked.spawned is False and tracked.result is None        # the caller closes the row as NEVER_RAN


async def test_an_oserror_at_spawn_is_spawn_failed_for_tracked_and_untracked(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(ce.asyncio, "create_subprocess_exec", boom)
    job = FakeJob()
    tracked = TrackedRun("e")
    r = await make(tmp_path, job).run(spec(tmp_path, "pass"), tracked=tracked)
    assert (r.lifecycle, r.spawn_failed, r.tree, r.reason, r.outcome) == (
        ProcessLifecycle.SPAWN_FAILED, True, TreeProof.EMPTY, "SPAWN_OSERROR", CliOutcome.ERROR)
    assert "cannot spawn" in r.stderr and tracked.spawned is False and tracked.result is r and job.closed
    r = await CommandExecutor(WorkspaceGuard(tmp_path)).run(spec(tmp_path, "pass"))
    assert (r.lifecycle, r.spawn_failed, r.stderr.startswith("spawn failed:")) == (ProcessLifecycle.SPAWN_FAILED, True, True)


# --- cancellation: settle first, result visible, idempotent -------------------------------------------------------------
async def test_a_coroutine_cancel_settles_the_tree_publishes_the_result_then_reraises(tmp_path):
    job = FakeJob()
    ex = make(tmp_path, job)
    tracked = TrackedRun("e", run_key="r:tracked:s:1")
    task = asyncio.create_task(ex.run(spec(tmp_path, SLEEPER), tracked=tracked))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    r = tracked.result
    assert r is not None and (r.outcome, r.lifecycle) == (CliOutcome.CANCELLED, ProcessLifecycle.TERMINATED)
    assert job.terminated and job.closed and ex._cancel_events == {}


async def test_repeated_cancels_during_settlement_do_not_interrupt_it(tmp_path, monkeypatch):
    job = FakeJob()
    ex = make(tmp_path, job)
    original = ex._prove_empty
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def slow(j):
        entered.set()
        await proceed.wait()
        return await original(j)

    monkeypatch.setattr(ex, "_prove_empty", slow)
    tracked = TrackedRun("e")
    task = asyncio.create_task(ex.run(spec(tmp_path, SLEEPER), tracked=tracked))
    await asyncio.sleep(1.0)
    task.cancel()
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0.2)
    assert not task.done() and tracked.result is None                  # still settling, nothing raised yet
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert job.calls.count("terminate") == 1 and tracked.result.lifecycle is ProcessLifecycle.TERMINATED


async def test_cancel_timeout_and_stop_racing_terminate_once(tmp_path):
    job = FakeJob()
    ex = make(tmp_path, job)
    tracked = TrackedRun("e", run_key="r:1")
    task = asyncio.create_task(ex.run(spec(tmp_path, SLEEPER, timeout_seconds=1), tracked=tracked))
    await asyncio.sleep(0.8)
    ex.cancel("r")                                                      # cancel + (about to be) timeout + stop together
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert job.calls.count("terminate") == 1
    first = tracked.result
    assert first.lifecycle is ProcessLifecycle.TERMINATED and first.outcome is CliOutcome.CANCELLED   # the first reason


async def test_an_exception_of_an_untracked_on_started_settles_the_tree_and_propagates(tmp_path):
    ex = CommandExecutor(WorkspaceGuard(tmp_path))
    seen = []

    async def on_started(identity):
        seen.append(identity)
        raise RuntimeError("persist failed")

    with pytest.raises(RuntimeError, match="persist failed"):
        await ex.run(spec(tmp_path, SLEEPER), on_started=on_started)
    assert not psutil.pid_exists(seen[0].pid) or not psutil.Process(seen[0].pid).is_running() \
        or psutil.Process(seen[0].pid).create_time() != seen[0].create_time
    assert ex._cancel_events == {}


# --- POSIX degrade / refusal contract ---------------------------------------------------------------------------------
async def test_tracking_off_degrades_a_tracked_run_to_untracked(tmp_path):
    ex = CommandExecutor(WorkspaceGuard(tmp_path), tracking=False)
    assert ex.tracking_enabled is False
    tracked = TrackedRun("e")
    r = await ex.run(spec(tmp_path, "print(1)"), tracked=tracked)
    assert tracked.degraded == "UNTRACKED_PLATFORM" and tracked.spawned and tracked.result is r
    assert (r.outcome, r.tree, r.lifecycle) == (CliOutcome.SUCCESS, TreeProof.UNKNOWN, ProcessLifecycle.EXITED)


def test_a_posix_platform_offers_no_job_and_the_self_test_says_unsupported(monkeypatch):
    from app.infra import process_control as pc
    monkeypatch.setattr(pc.sys, "platform", "linux")
    assert pc.default_job_factory() is None and pc.platform_supports_tracking() is False
    assert run_tracking_self_test() == SelfTestResult(TrackingState.UNSUPPORTED_PLATFORM,
                                                      "PROCESS_TRACKING_UNSUPPORTED_PLATFORM")


# --- cancel scopes and the cancelled-prefix record (9) ------------------------------------------------------------------
async def test_a_cancel_between_two_commands_refuses_the_next_one_and_the_scope_cleans_up(tmp_path):
    ex = CommandExecutor(WorkspaceGuard(tmp_path), tracking=False)
    scope = "run1:tracked:s1"
    with ex.tracked_scope(scope):
        first = await ex.run(spec(tmp_path, "pass"), run_key=f"{scope}:1")
        assert first.outcome is CliOutcome.SUCCESS
        ex.cancel(scope)                                             # no command is running: nothing to set
        assert ex._cancel_events == {} and ex._cancelled_prefixes == {scope: {scope}}
        r = await ex.run(spec(tmp_path, "open('ran','w')"), run_key=f"{scope}:2")
        assert (r.outcome, r.lifecycle, r.exit_code) == (CliOutcome.CANCELLED, ProcessLifecycle.NOT_STARTED, None)
        assert not (tmp_path / "ran").exists()
    assert ex._cancelled_prefixes == {}
    after = await ex.run(spec(tmp_path, "pass"), run_key=f"{scope}:3")      # the record died with the scope
    assert after.outcome is CliOutcome.SUCCESS


@pytest.mark.parametrize("prefix,key,matches", [
    ("r", "r", True), ("r", "r:tracked:s:1", True), ("r", "rx", False), ("r:tracked:s", "r:tracked:s:1", True),
    ("r:tracked:s:1", "r:tracked:s:2", False), ("r:tracked:s:1", "r:tracked:s:10", False)])
def test_the_prefix_match_rule(prefix, key, matches):
    assert ce._prefix_matches(prefix, key) is matches


@pytest.mark.parametrize("scope,prefix,recorded", [
    ("r:tracked:s", "r", True), ("r:tracked:s", "r:tracked:s", True), ("r:tracked:s", "r:tracked:s:1", True),
    ("r:tracked:s", "other", False), ("r:tracked:s", "r:tracked:t", False), ("r:tracked:s", "rx", False)])
def test_a_cancel_is_recorded_only_when_it_would_match_under_the_scope(scope, prefix, recorded):
    assert ce._recorded_under_scope(scope, prefix) is recorded


async def test_a_cancel_aimed_at_one_command_does_not_refuse_its_siblings(tmp_path):
    ex = CommandExecutor(WorkspaceGuard(tmp_path), tracking=False)
    scope = "r:tracked:s"
    with ex.tracked_scope(scope):
        ex.cancel(f"{scope}:1")                                      # P = ...:1 is stored, never S
        assert ex._cancelled_prefixes[scope] == {f"{scope}:1"}
        assert (await ex.run(spec(tmp_path, "pass"), run_key=f"{scope}:2")).outcome is CliOutcome.SUCCESS
        assert (await ex.run(spec(tmp_path, "pass"), run_key=f"{scope}:1")).outcome is CliOutcome.CANCELLED


async def test_a_run_wide_cancel_refuses_every_later_command_of_the_scope(tmp_path):
    ex = CommandExecutor(WorkspaceGuard(tmp_path), tracking=False)
    with ex.tracked_scope("r:tracked:s"):
        ex.cancel("r")
        assert (await ex.run(spec(tmp_path, "pass"), run_key="r:tracked:s:9")).outcome is CliOutcome.CANCELLED


async def test_run_key_of_a_tracked_command_never_collides_with_a_step_key(tmp_path):
    ex = CommandExecutor(WorkspaceGuard(tmp_path), tracking=False)
    scope_key, step_key = "run1:tracked:s1", "run1:step9"
    command_key = f"{scope_key}:77"
    assert command_key != step_key and not ce._prefix_matches(step_key, command_key)
    assert ce._prefix_matches("run1", command_key) and ce._prefix_matches("run1", step_key)
    assert not ce._prefix_matches(scope_key, step_key)


def test_cancelled_prefixes_is_separate_from_cancel_events(tmp_path):
    ex = CommandExecutor(WorkspaceGuard(tmp_path), tracking=False)
    with ex.tracked_scope("s"):
        ex.cancel("s")
        assert ex._cancel_events == {} and ex._cancelled_prefixes == {"s": {"s"}}


async def test_the_tracked_run_key_is_used_when_no_run_key_is_given(tmp_path):
    ex = CommandExecutor(WorkspaceGuard(tmp_path), tracking=False)
    with ex.tracked_scope("r:tracked:s"):
        ex.cancel("r")
        tracked = TrackedRun("e", run_key="r:tracked:s:1")
        r = await ex.run(spec(tmp_path, "pass"), tracked=tracked)
        assert r.lifecycle is ProcessLifecycle.NOT_STARTED and tracked.result is r and tracked.spawned is False


# --- bounded pump drain (untracked, any platform) ----------------------------------------------------------------------
HOLDER = ("import subprocess, sys\n"
          "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
          "open('child.pid', 'w').write(str(c.pid))\n"
          "print('root done', flush=True)\n")


async def test_an_untracked_root_that_exits_while_a_descendant_holds_the_pipes_returns_after_the_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "PUMP_DRAIN_SECONDS", 1.5)
    ex = CommandExecutor(WorkspaceGuard(tmp_path), tracking=False)
    t0 = time.monotonic()
    try:
        r = await ex.run(CommandSpec(argv=[PY, "-c", HOLDER], cwd=tmp_path, timeout_seconds=30, idle_seconds=None))
        elapsed = time.monotonic() - t0
    finally:
        pid_file = tmp_path / "child.pid"
        if pid_file.exists():
            try:
                psutil.Process(int(pid_file.read_text())).kill()
            except psutil.Error:
                pass
    assert elapsed < 20, "no unbounded hang"
    assert (r.outcome, r.exit_code, r.termination, r.tree, r.truncated, r.lifecycle) == (
        CliOutcome.SUCCESS, 0, None, TreeProof.UNKNOWN, True, ProcessLifecycle.EXITED)
    assert r.reason == "PUMP_DRAIN_TIMEOUT" and "root done" in r.stdout


def test_the_executor_poll_constants_are_positive_finite():
    for value in (P.EXECUTOR_POLL_SECONDS, P.JOB_POLL_INTERVAL_SECONDS):
        assert 0 < value < float("inf") and not isinstance(value, bool)


# --- bounded pump drain, tracked ----------------------------------------------------------------------------------------
def _hanging_pump(ex, monkeypatch):
    """An extra pump that never sees EOF: stands for a pipe held open by a process outside the Job."""
    original = ex._start_pumps

    def with_hang(run):
        original(run)
        run.pumps.append(asyncio.create_task(asyncio.sleep(3600)))

    monkeypatch.setattr(ex, "_start_pumps", with_hang)


async def test_a_tracked_drain_is_bounded_and_an_empty_job_is_still_exited(tmp_path, monkeypatch):
    ex = make(tmp_path, FakeJob())
    _hanging_pump(ex, monkeypatch)
    t0 = time.monotonic()
    r = await ex.run(spec(tmp_path, "print(1)"), tracked=TrackedRun("e"))
    assert time.monotonic() - t0 < 8
    assert (r.lifecycle, r.truncated, r.reason, r.tree, r.outcome) == (
        ProcessLifecycle.EXITED, True, "PUMP_DRAIN_TIMEOUT", TreeProof.EMPTY, CliOutcome.SUCCESS)


async def test_a_tracked_drain_timeout_is_unconfirmed_only_when_the_job_is_not_proven_empty(tmp_path, monkeypatch):
    job = FakeJob(unkillable=True)
    ex = make(tmp_path, job)
    _hanging_pump(ex, monkeypatch)
    try:
        r = await ex.run(spec(tmp_path, SLEEPER, timeout_seconds=1), tracked=TrackedRun("e"))
        assert r.lifecycle is ProcessLifecycle.TERMINATION_UNCONFIRMED and r.truncated and r.termination == "STILL_ALIVE"
    finally:
        job.cleanup()


async def test_the_root_exit_is_read_from_returncode_never_from_proc_wait(tmp_path, monkeypatch):
    def forbidden(self):
        raise AssertionError("proc.wait() must not be the signal of a tracked run")

    monkeypatch.setattr(asyncio.subprocess.Process, "wait", forbidden)
    r = await make(tmp_path, FakeJob()).run(spec(tmp_path, "print(1)"), tracked=TrackedRun("e"))
    assert r.lifecycle is ProcessLifecycle.EXITED
    r = await make(tmp_path, FakeJob()).run(spec(tmp_path, SLEEPER, timeout_seconds=1), tracked=TrackedRun("e"))
    assert r.lifecycle is ProcessLifecycle.TERMINATED


async def test_every_spawn_failure_leaves_no_registered_cancel_event_and_returns_the_slot(tmp_path):
    job = FakeJob(assign_error=JobError("JOB_ASSIGN_FAILED"))
    ex = make(tmp_path, job, max_concurrent=1)
    r = await ex.run(spec(tmp_path, "pass"), run_key="k", tracked=TrackedRun("e"))
    assert r.spawn_failed and ex._cancel_events == {} and ex._slots._value == 1
