"""B2 phase 2: REAL Windows Job Object tests (design 5.1, 13). Real children, grandchildren and a real Job; skipped off Windows
(POSIX has no process tracking, 5.2, so there is nothing to run there)."""
import asyncio
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from app.infra import command_executor as ce
from app.infra import process_control as pc
from app.infra.command_executor import CommandExecutor, CommandSpec, TrackedRun
from app.infra.process_control import IdentityFenced, JobError, TrackingState
from app.infra.workspace_guard import WorkspaceGuard
from app.models.states import CliOutcome, ProcessLifecycle, TreeProof
from app.validation import policies as P

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")
from app.infra import windows_job            # importable everywhere: every kernel32 call fails off Windows
from app.infra.windows_job import CREATE_SUSPENDED, WindowsJob, handle_is_inheritable, resume_primary_thread

PY = sys.executable
REPO = Path(__file__).resolve().parents[2]

# root -> child -> grandchild, each sleeping; the pids are written to files so the tests can look at every member.
TREE = textwrap.dedent("""
    import subprocess, sys, time
    child_code = (
        "import subprocess, sys, time\\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\\n"
        "open('grandchild.pid', 'w').write(str(g.pid))\\n"
        "time.sleep(120)\\n")
    c = subprocess.Popen([sys.executable, '-c', child_code])
    open('child.pid', 'w').write(str(c.pid))
    time.sleep(120)
""")
HOLDER = ("import subprocess, sys\n"
          "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
          "open('child.pid', 'w').write(str(c.pid))\n"
          "print('root done', flush=True)\n")


def gone(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
        return not p.is_running() or p.status() == psutil.STATUS_ZOMBIE
    except psutil.Error:
        return True


def pid_of(tmp_path, name) -> int:
    return int((tmp_path / name).read_text())


async def wait_for_file(path: Path, seconds=15.0):
    deadline = time.monotonic() + seconds
    while not path.exists() or not path.read_text():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        await asyncio.sleep(0.05)


def eventually(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def kill_pid_file(tmp_path, *names):
    for name in names:
        f = tmp_path / name
        if f.exists() and f.read_text():
            try:
                psutil.Process(int(f.read_text())).kill()
            except psutil.Error:
                pass


@pytest.fixture
def tmp_cwd(tmp_path):
    yield tmp_path
    kill_pid_file(tmp_path, "grandchild.pid", "child.pid")


def spec(tmp_path, code, **kw):
    kw.setdefault("idle_seconds", None)
    return CommandSpec(argv=[PY, "-c", code], cwd=tmp_path, **kw)


def real(tmp_path, **kw):
    return CommandExecutor(WorkspaceGuard(tmp_path), **kw)


class CapturingJob(WindowsJob):
    """A real Job that remembers the pid it was given (so a test can prove that process is gone)."""
    def __init__(self):
        super().__init__()
        self.pid = None

    def assign(self, pid):
        self.pid = pid
        super().assign(pid)


class AssignFailsJob(CapturingJob):
    def assign(self, pid):
        self.pid = pid
        raise JobError("JOB_ASSIGN_FAILED", "injected", access_denied=True)


class ResumeFailsJob(CapturingJob):
    def resume(self, pid):
        raise JobError("RESUME_FAILED", "injected")


class UnkillableJob(CapturingJob):
    def terminate(self, exit_code):
        self.calls_to_terminate = getattr(self, "calls_to_terminate", 0) + 1


class QueryFailsJob(CapturingJob):
    def active_processes(self):
        raise JobError("JOB_QUERY_FAILED", "injected")


def job_class_executor(tmp_path, cls, holder, **kw):
    def factory():
        job = cls()
        holder.append(job)
        return job
    return CommandExecutor(WorkspaceGuard(tmp_path), job_factory=factory, **kw)


# --- the Job primitives -------------------------------------------------------------------------------------------------
def test_the_job_handle_is_not_inheritable_and_carries_kill_on_close():
    job = WindowsJob()
    try:
        assert handle_is_inheritable(job._handle) is False       # a grandchild cannot keep the Job open (5.1 step 1)
    finally:
        job.close()
    assert job.closed
    job.close()                                                   # idempotent
    with pytest.raises(JobError):
        job.active_processes()


def test_spawn_suspended_assign_resume_terminate_and_prove_empty(tmp_path):
    job = WindowsJob()
    child = subprocess.Popen([PY, "-c", TREE], cwd=tmp_path, stdin=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED)
    try:
        assert len(psutil.Process(child.pid).threads()) == 1      # exactly one thread while suspended
        job.assign(child.pid)
        time.sleep(0.5)
        assert not (tmp_path / "child.pid").exists()               # still suspended: no user code yet
        job.resume(child.pid)
        assert eventually(lambda: (tmp_path / "grandchild.pid").exists() and (tmp_path / "grandchild.pid").read_text())
        assert job.active_processes() >= 3                         # root, child and grandchild (>= : the venv shim re-executes)
        job.terminate(P.JOB_TERMINATE_EXIT_CODE)
        assert eventually(lambda: job.active_processes() == 0, 5)
        assert child.wait(timeout=10) == P.JOB_TERMINATE_EXIT_CODE   # a job-killed root reports the fixed exit code
        assert eventually(lambda: gone(pid_of(tmp_path, "grandchild.pid")) and gone(pid_of(tmp_path, "child.pid")))
    finally:
        child.kill()
        job.close()
        kill_pid_file(tmp_path, "grandchild.pid", "child.pid")


def test_resume_requires_exactly_one_thread_and_previous_count_one(tmp_path, monkeypatch):
    child = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"], creationflags=CREATE_SUSPENDED)
    try:
        real_process = windows_job.psutil.Process

        class TwoThreads:
            def __init__(self, pid):
                pass

            def threads(self):
                return [1, 2]

        class NoThreads(TwoThreads):
            def threads(self):
                return []

        for fake in (TwoThreads, NoThreads):
            monkeypatch.setattr(windows_job.psutil, "Process", fake)
            with pytest.raises(JobError) as info:
                resume_primary_thread(child.pid)
            assert info.value.code == "RESUME_FAILED"
        monkeypatch.setattr(windows_job.psutil, "Process", real_process)
        resume_primary_thread(child.pid)                           # the real one: previous count 1
        time.sleep(0.3)
        with pytest.raises(JobError) as info:                      # already running: previous count 0 / several threads
            resume_primary_thread(child.pid)
        assert info.value.code == "RESUME_FAILED"
    finally:
        child.kill()


def test_closing_the_job_handle_kills_the_tree_kill_on_close(tmp_path):
    job = WindowsJob()
    child = subprocess.Popen([PY, "-c", TREE], cwd=tmp_path, stdin=subprocess.DEVNULL, creationflags=CREATE_SUSPENDED)
    try:
        job.assign(child.pid)
        job.resume(child.pid)
        assert eventually(lambda: (tmp_path / "grandchild.pid").exists() and (tmp_path / "grandchild.pid").read_text())
        job.close()
        assert eventually(lambda: gone(pid_of(tmp_path, "grandchild.pid")) and gone(pid_of(tmp_path, "child.pid"))
                          and child.poll() is not None, 15)
    finally:
        child.kill()
        job.close()
        kill_pid_file(tmp_path, "grandchild.pid", "child.pid")


OWNER = textwrap.dedent("""
    import os, subprocess, sys, time
    sys.path.insert(0, {repo!r})
    from app.infra.windows_job import WindowsJob, CREATE_SUSPENDED
    job = WindowsJob()
    # close_fds=False: the most permissive spawn, still no inheritable Job handle may reach the tree
    p = subprocess.Popen([sys.executable, '-c', {tree!r}], close_fds=False, stdin=subprocess.DEVNULL,
                         creationflags=CREATE_SUSPENDED)
    job.assign(p.pid)
    job.resume(p.pid)
    print(os.getpid(), flush=True)
    time.sleep(120)
""")


def test_the_death_of_the_owner_process_ends_the_whole_tree(tmp_path):
    """Runner crash (5.1 step 10): the Job handle dies with its owner and KILL_ON_JOB_CLOSE ends the tree. If a grandchild
    had inherited the handle the Job would stay open and the tree would survive: this also proves non-inheritance."""
    script = tmp_path / "owner.py"
    script.write_text(OWNER.format(repo=str(REPO), tree=TREE), encoding="utf-8")
    owner = subprocess.Popen([PY, str(script)], cwd=tmp_path, stdout=subprocess.PIPE, text=True)
    try:
        owner_pid = int(owner.stdout.readline())
        assert eventually(lambda: (tmp_path / "grandchild.pid").exists() and (tmp_path / "grandchild.pid").read_text(), 20)
        child_pid, grandchild_pid = pid_of(tmp_path, "child.pid"), pid_of(tmp_path, "grandchild.pid")
        assert not gone(grandchild_pid)
        psutil.Process(owner_pid).kill()                           # the real interpreter, not the venv launcher
        assert eventually(lambda: gone(grandchild_pid) and gone(child_pid), 20), "the tree survived its owner"
    finally:
        owner.kill()
        kill_pid_file(tmp_path, "grandchild.pid", "child.pid")


# --- executor, real Job -------------------------------------------------------------------------------------------------
async def test_a_normal_exit_without_descendants(tmp_cwd):
    ex = real(tmp_cwd)
    tracked = TrackedRun("e1")
    r = await ex.run(spec(tmp_cwd, "print('hi')"), tracked=tracked)
    assert (r.outcome, r.lifecycle, r.tree, r.termination, r.exit_code) == (
        CliOutcome.SUCCESS, ProcessLifecycle.EXITED, TreeProof.EMPTY, None, 0)
    assert r.stdout.strip() == "hi" and r.identity is not None and tracked.spawned and ex.unconfirmed_job_count() == 0


async def test_the_command_runs_only_after_the_identity_was_persisted(tmp_cwd):
    marker = tmp_cwd / "ran"
    seen = []

    async def on_started(identity):
        await asyncio.sleep(0.8)                                   # a slow persist: the suspended process must not run yet
        seen.append(marker.exists())

    r = await real(tmp_cwd).run(spec(tmp_cwd, "open('ran','w').write('x')"), tracked=TrackedRun("e"),
                                on_started=on_started)
    assert seen == [False] and marker.exists() and r.lifecycle is ProcessLifecycle.EXITED


async def test_root_exits_while_a_descendant_holds_the_pipes_is_a_natural_exit(tmp_cwd):
    """The reviewer scenario (timeout_seconds=3): old code reported TIMEOUT / exit 0 / ALREADY_EXITED after the whole
    lifetime of the descendant; now it is a natural exit, survivors are terminated, the root's outcome is kept."""
    t0 = time.monotonic()
    tracked = TrackedRun("e")
    r = await real(tmp_cwd).run(CommandSpec(argv=[PY, "-c", HOLDER], cwd=tmp_cwd, timeout_seconds=3, idle_seconds=None),
                                tracked=tracked)
    assert time.monotonic() - t0 < 10
    assert (r.outcome, r.exit_code, r.lifecycle, r.survivors, r.termination, r.tree) == (
        CliOutcome.SUCCESS, 0, ProcessLifecycle.EXITED, True, "TERMINATED", TreeProof.EMPTY)
    assert "root done" in r.stdout and r.truncated is False
    assert eventually(lambda: gone(pid_of(tmp_cwd, "child.pid")))


async def test_timeout_kills_a_three_level_tree_through_the_job(tmp_cwd):
    tracked = TrackedRun("e")
    ex = real(tmp_cwd)
    r = await ex.run(CommandSpec(argv=[PY, "-c", TREE], cwd=tmp_cwd, timeout_seconds=4, idle_seconds=None), tracked=tracked)
    assert (r.outcome, r.lifecycle, r.termination, r.tree) == (CliOutcome.TIMEOUT, ProcessLifecycle.TERMINATED,
                                                              "TERMINATED", TreeProof.EMPTY)
    assert r.exit_code == P.JOB_TERMINATE_EXIT_CODE                # pinned: documented as not meaningful unless SUCCESS/ERROR
    assert eventually(lambda: gone(pid_of(tmp_cwd, "grandchild.pid")) and gone(pid_of(tmp_cwd, "child.pid")))


async def test_idle_kills_the_tree(tmp_cwd):
    r = await real(tmp_cwd).run(CommandSpec(argv=[PY, "-c", TREE], cwd=tmp_cwd, timeout_seconds=60, idle_seconds=3),
                                tracked=TrackedRun("e"))
    assert (r.outcome, r.lifecycle, r.tree) == (CliOutcome.IDLE, ProcessLifecycle.TERMINATED, TreeProof.EMPTY)
    assert eventually(lambda: gone(pid_of(tmp_cwd, "grandchild.pid")))


async def test_cooperative_cancel_kills_the_tree(tmp_cwd):
    ex = real(tmp_cwd)
    task = asyncio.create_task(ex.run(spec(tmp_cwd, TREE), run_key="r:tracked:s:1", tracked=TrackedRun("e")))
    await wait_for_file(tmp_cwd / "grandchild.pid")
    ex.cancel("r")
    r = await asyncio.wait_for(task, 20)
    assert (r.outcome, r.lifecycle, r.tree) == (CliOutcome.CANCELLED, ProcessLifecycle.TERMINATED, TreeProof.EMPTY)
    assert eventually(lambda: gone(pid_of(tmp_cwd, "grandchild.pid")) and gone(pid_of(tmp_cwd, "child.pid")))


async def test_coroutine_cancel_settles_the_tree_sets_the_result_then_reraises(tmp_cwd):
    ex = real(tmp_cwd)
    tracked = TrackedRun("e")
    task = asyncio.create_task(ex.run(spec(tmp_cwd, TREE), tracked=tracked))
    await wait_for_file(tmp_cwd / "grandchild.pid")
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tracked.result is not None and tracked.result.lifecycle is ProcessLifecycle.TERMINATED
    assert tracked.result.outcome is CliOutcome.CANCELLED
    assert gone(pid_of(tmp_cwd, "grandchild.pid")) or eventually(lambda: gone(pid_of(tmp_cwd, "grandchild.pid")))
    assert ex._cancel_events == {}


async def test_a_pre_set_scope_cancel_never_spawns(tmp_cwd):
    ex = real(tmp_cwd)
    with ex.tracked_scope("r:tracked:s"):
        ex.cancel("r")
        tracked = TrackedRun("e", run_key="r:tracked:s:1")
        r = await ex.run(spec(tmp_cwd, "open('ran','w')"), tracked=tracked)
    assert r.lifecycle is ProcessLifecycle.NOT_STARTED and not (tmp_cwd / "ran").exists() and not tracked.spawned


# --- spawn failure paths: user code never ran ----------------------------------------------------------------------------
def marker_spec(tmp_path):
    return spec(tmp_path, "open('ran', 'w').write('x'); import time; time.sleep(30)")


async def test_job_assignment_failure_kills_the_suspended_process(tmp_cwd):
    holder = []
    ex = job_class_executor(tmp_cwd, AssignFailsJob, holder)
    r = await ex.run(marker_spec(tmp_cwd), tracked=TrackedRun("e"))
    assert (r.lifecycle, r.reason, r.spawn_failed, r.tree) == (ProcessLifecycle.SPAWN_FAILED, "JOB_ASSIGN_FAILED", True,
                                                              TreeProof.EMPTY)
    await asyncio.sleep(0.5)
    assert not (tmp_cwd / "ran").exists() and gone(holder[0].pid) and holder[0].closed


async def test_resume_failure_kills_the_assigned_suspended_process(tmp_cwd):
    holder = []
    r = await job_class_executor(tmp_cwd, ResumeFailsJob, holder).run(marker_spec(tmp_cwd), tracked=TrackedRun("e"))
    assert (r.lifecycle, r.reason) == (ProcessLifecycle.SPAWN_FAILED, "RESUME_FAILED")
    await asyncio.sleep(0.5)
    assert not (tmp_cwd / "ran").exists() and gone(holder[0].pid) and holder[0]._handle is None


async def test_identity_unavailable_kills_the_suspended_process(tmp_cwd, monkeypatch):
    monkeypatch.setattr(ce, "capture_identity", lambda pid: None)
    holder = []
    r = await job_class_executor(tmp_cwd, CapturingJob, holder).run(marker_spec(tmp_cwd), tracked=TrackedRun("e"))
    assert (r.lifecycle, r.reason) == (ProcessLifecycle.SPAWN_FAILED, "IDENTITY_UNAVAILABLE")
    await asyncio.sleep(0.5)
    assert not (tmp_cwd / "ran").exists() and gone(holder[0].pid)


@pytest.mark.parametrize("failure,reason", [("raise", "IDENTITY_PERSIST_FAILED"), ("fence", "IDENTITY_FENCED"),
                                            ("timeout", "IDENTITY_PERSIST_FAILED")])
async def test_on_started_failures_kill_the_process_before_it_runs(tmp_cwd, monkeypatch, failure, reason):
    monkeypatch.setattr(P, "TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS", 1.0)
    pids = []

    async def on_started(identity):
        pids.append(identity.pid)
        if failure == "raise":
            raise RuntimeError("db down")
        if failure == "fence":
            raise IdentityFenced("lost lease")
        await asyncio.sleep(60)

    r = await real(tmp_cwd).run(marker_spec(tmp_cwd), tracked=TrackedRun("e"), on_started=on_started)
    assert (r.lifecycle, r.reason, r.outcome) == (ProcessLifecycle.SPAWN_FAILED, reason, CliOutcome.ERROR)
    await asyncio.sleep(0.5)
    assert not (tmp_cwd / "ran").exists() and gone(pids[0])


async def test_on_started_cancelled_by_the_cancel_event_is_a_cancelled_spawn_failure(tmp_cwd):
    ex = real(tmp_cwd)
    pids, entered = [], asyncio.Event()

    async def on_started(identity):
        pids.append(identity.pid)
        entered.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(ex.run(marker_spec(tmp_cwd), run_key="k", tracked=TrackedRun("e"), on_started=on_started))
    await asyncio.wait_for(entered.wait(), 10)
    ex.cancel("k")
    r = await asyncio.wait_for(task, 20)
    assert (r.outcome, r.lifecycle, r.reason) == (CliOutcome.CANCELLED, ProcessLifecycle.SPAWN_FAILED,
                                                  "CANCELLED_BEFORE_RESUME")
    await asyncio.sleep(0.5)
    assert not (tmp_cwd / "ran").exists() and gone(pids[0])


async def test_on_started_raising_on_an_untracked_command_settles_the_tree_and_propagates(tmp_cwd):
    pids = []

    async def on_started(identity):
        pids.append(identity.pid)
        raise RuntimeError("persist failed")

    with pytest.raises(RuntimeError, match="persist failed"):
        await real(tmp_cwd).run(spec(tmp_cwd, TREE), on_started=on_started)
    assert eventually(lambda: gone(pids[0]))


# --- TERMINATION_UNCONFIRMED through a real Job that cannot be killed or queried ---------------------------------------
async def test_an_unkillable_job_is_unconfirmed_retained_and_later_settled(tmp_cwd, monkeypatch):
    monkeypatch.setattr(P, "PROCESS_TERMINATE_GRACE_SECONDS", 1.0)
    holder = []
    ex = job_class_executor(tmp_cwd, UnkillableJob, holder, max_concurrent=1)
    try:
        r = await ex.run(CommandSpec(argv=[PY, "-c", TREE], cwd=tmp_cwd, timeout_seconds=3, idle_seconds=None),
                         tracked=TrackedRun("x1"))
        job = holder[0]
        assert (r.outcome, r.lifecycle, r.termination, r.tree) == (
            CliOutcome.TIMEOUT, ProcessLifecycle.TERMINATION_UNCONFIRMED, "STILL_ALIVE", TreeProof.NOT_EMPTY)
        assert job.calls_to_terminate == 1 and not job.closed and ex.retained_job("x1") is job
        assert ex._slots._value == 1                               # the slot was released
        assert not gone(pid_of(tmp_cwd, "grandchild.pid"))         # the tree really is still alive
        WindowsJob.terminate(job, P.JOB_TERMINATE_EXIT_CODE)       # "later the OS lets us": the real kill
        assert await ex.resettle_retained("x1") is True
        assert job.closed and ex.unconfirmed_job_count() == 0
        assert eventually(lambda: gone(pid_of(tmp_cwd, "grandchild.pid")))
    finally:
        for job in holder:
            job.close()


async def test_a_job_that_cannot_be_queried_is_unconfirmed_unknown(tmp_cwd):
    holder = []
    ex = job_class_executor(tmp_cwd, QueryFailsJob, holder)
    try:
        r = await ex.run(CommandSpec(argv=[PY, "-c", TREE], cwd=tmp_cwd, timeout_seconds=3, idle_seconds=None),
                         tracked=TrackedRun("q1"))
        assert (r.lifecycle, r.termination, r.tree) == (ProcessLifecycle.TERMINATION_UNCONFIRMED, "UNKNOWN",
                                                       TreeProof.UNKNOWN)
        assert ex.retained_job("q1") is holder[0]
    finally:
        WindowsJob.terminate(holder[0], P.JOB_TERMINATE_EXIT_CODE)
        holder[0].close()                                          # KILL_ON_JOB_CLOSE as the backstop


# --- the startup self-test ---------------------------------------------------------------------------------------------
def test_the_startup_self_test_succeeds_on_this_host():
    result = pc.run_tracking_self_test()
    assert result.state is TrackingState.SUPPORTED and result.reason_code is None, result


@pytest.mark.parametrize("cls,step", [(AssignFailsJob, "assign"), (ResumeFailsJob, "resume")])
def test_a_failing_self_test_reports_unavailable_and_leaves_no_child(cls, step):
    holder = []

    def factory():
        job = cls()
        holder.append(job)
        return job

    result = pc.run_tracking_self_test(factory)
    assert result.state is TrackingState.UNAVAILABLE and result.reason_code == "PROCESS_TRACKING_UNAVAILABLE"
    assert result.failure.startswith(step)
    assert eventually(lambda: gone(holder[0].pid))
    assert holder[0]._handle is None


def test_a_job_factory_that_fails_makes_tracking_unavailable():
    def factory():
        raise JobError("JOB_CREATE_FAILED", "no job objects here")

    result = pc.run_tracking_self_test(factory)
    assert result == pc.SelfTestResult(TrackingState.UNAVAILABLE, "PROCESS_TRACKING_UNAVAILABLE",
                                       "create: JOB_CREATE_FAILED")


def test_an_unkillable_job_fails_the_self_test(monkeypatch):
    monkeypatch.setattr(P, "PROCESS_TERMINATE_GRACE_SECONDS", 0.5)
    holder = []

    def factory():
        job = UnkillableJob()
        holder.append(job)
        return job

    result = pc.run_tracking_self_test(factory)
    assert result.state is TrackingState.UNAVAILABLE and result.failure.startswith("prove")
    assert eventually(lambda: gone(holder[0].pid)) and holder[0]._handle is None   # the child is still reaped (closed Job)
