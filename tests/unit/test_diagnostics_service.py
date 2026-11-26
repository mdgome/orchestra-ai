"""Read-only project diagnostics: results, what is never executed, single flight, cache and bounded file inspection."""
import asyncio
import os
import subprocess
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from app.infra.command_executor import CommandResult
from app.models.states import CliOutcome
from app.providers import preflight as preflight_module
from app.providers.preflight import PreflightReport
from app.services.diagnostics_service import DiagnosticsService, Status, TrackedVerdict
from app.services.runner import RunnerLifecycle as L
from app.services.runtime_status import StartupOutcome as S
from app.services.runtime_status import runtime_snapshot
from app.validation import policies as P
from app.validation.exceptions import FailClosed, NotFound

GOOD_PREFLIGHT = PreflightReport(versions={"claude": "2.1.0", "codex": "0.9.0"},
                                 missing_flags={"claude": [], "codex": []})


class FakeProjects:
    def __init__(self):
        self.rows: dict[uuid.UUID, SimpleNamespace] = {}

    def add(self, path, test_policy=None, **extra) -> SimpleNamespace:
        row = SimpleNamespace(id=uuid.uuid4(), workspace_path=str(path), test_policy=test_policy,
                              write_owner_task_id=None, workspace_state="CLEAN", **extra)
        self.rows[row.id] = row
        return row

    async def get_project_and_profile_hash(self, project_id):
        if project_id not in self.rows:
            raise NotFound("project not found")
        row = self.rows[project_id]
        return row, getattr(row, "profile_hash", None)


class FakeExecutor:
    """Records every command. Anything but a tool version probe would be a violation of the read-only contract."""

    def __init__(self, missing=(), gate: asyncio.Event | None = None, error: Exception | None = None):
        self.calls: list[list[str]] = []
        self.missing, self.gate, self.error = set(missing), gate, error

    async def run(self, spec, **_kw):
        self.calls.append(list(spec.argv))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        if spec.argv[0] in self.missing:
            raise FailClosed("executable not found", {"executable": spec.argv[0]})
        if spec.argv[0] == "java":
            return CommandResult(spec.argv, CliOutcome.SUCCESS, 0, "", 'openjdk version "21"\n', 0.0)
        return CommandResult(spec.argv, CliOutcome.SUCCESS, 0, "git version 2.45.0\n", "", 0.0)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


READY = runtime_snapshot(L.RUNNING, S.STARTED, False)


def make(tmp_path, *, executor=None, preflight=GOOD_PREFLIGHT, runtime=READY, clock=None):
    projects = FakeProjects()
    ex = executor or FakeExecutor()
    state = SimpleNamespace(runtime=runtime, preflight=preflight)
    svc = DiagnosticsService(projects, ex, protected=(), get_runtime=lambda: state.runtime,
                             get_preflight=lambda: state.preflight, clock=clock or Clock())
    return svc, projects, ex, state


def workspace(tmp_path, name="proj"):
    path = tmp_path / name
    path.mkdir()
    return path


def by_code(report):
    return {i.code: i for i in report.items}


# --- results ---------------------------------------------------------------------------------------------------------
async def test_a_healthy_pytest_project_has_no_blocker_and_auth_stays_unknown(tmp_path):
    svc, projects, ex, _ = make(tmp_path)
    report = await svc.diagnose(projects.add(workspace(tmp_path)).id)
    items = by_code(report)
    assert report.can_start and report.blockers == ()
    assert items["WORKSPACE_ACCESS"].status is Status.PASS and items["RUNNER_STATE"].status is Status.PASS
    assert items["CLI_AUTH"].status is Status.UNKNOWN          # never probed: a probe would spend tokens
    assert items["JAVA_RUNTIME"].status is Status.PASS and "not required" in items["JAVA_RUNTIME"].cause
    assert "guarantee" in report.disclaimer
    assert all(i.checked_at is not None for i in report.items)


async def test_an_inaccessible_workspace_fails_and_blocks_and_dependent_checks_are_unknown(tmp_path):
    svc, projects, ex, _ = make(tmp_path)
    report = await svc.diagnose(projects.add(tmp_path / "gone").id)
    items = by_code(report)
    assert not report.can_start and "WORKSPACE_ACCESS" in report.blockers
    assert items["WORKSPACE_ACCESS"].status is Status.FAIL and items["WORKSPACE_ACCESS"].blocks_execution
    for code in ("CLI_SETTINGS_SAFETY", "TEST_PRESET_RESOLVABLE", "GIT_AVAILABLE"):
        assert items[code].status is Status.UNKNOWN and not items[code].blocks_execution
    assert ex.calls == []                                      # nothing is run in a workspace that is not valid


async def test_a_registered_root_replaced_by_a_link_or_junction_fails_access(tmp_path):
    """Detectable: a symlink/junction swapped in, a changed canonical path, a vanished or inaccessible root. NOT detected
    (and not claimed): the real directory at the same canonical path being replaced by another directory."""
    ws, real = workspace(tmp_path), workspace(tmp_path, "real")
    svc, projects, ex, _ = make(tmp_path)
    row = projects.add(ws)
    ws.rmdir()
    if os.name == "nt":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(ws), str(real)], capture_output=True)
        if made.returncode != 0:
            pytest.skip("junctions are not available")
    else:
        try:
            os.symlink(real, ws, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available for this user")
    item = by_code(await svc.diagnose(row.id))["WORKSPACE_ACCESS"]
    assert item.status is Status.FAIL and item.blocks_execution and ex.calls == []


async def test_missing_cli_flags_and_a_missing_cli_block_while_unknown_preflight_does_not(tmp_path):
    report = PreflightReport(versions={"claude": "2", "codex": ""},
                             missing_flags={"claude": ["--tools"], "codex": ["--help"]})
    svc, projects, *_ = make(tmp_path, preflight=report)
    items = by_code(await svc.diagnose(projects.add(workspace(tmp_path)).id))
    assert items["CLI_CLAUDE_FLAGS"].status is Status.FAIL and "--tools" in items["CLI_CLAUDE_FLAGS"].cause
    assert items["CLI_CODEX_INSTALLED"].status is Status.FAIL and items["CLI_CODEX_FLAGS"].status is Status.UNKNOWN
    svc2, projects2, *_ = make(tmp_path, preflight=None)
    items2 = by_code(await svc2.diagnose(projects2.add(workspace(tmp_path, "p2")).id))
    assert items2["CLI_CLAUDE_INSTALLED"].status is Status.UNKNOWN and not items2["CLI_CLAUDE_INSTALLED"].blocks_execution


async def test_a_runner_that_is_not_ready_blocks_and_says_requests_will_wait(tmp_path):
    svc, projects, *_ = make(tmp_path, runtime=runtime_snapshot(L.STOPPED, S.PREFLIGHT_BLOCKED, False))
    report = await svc.diagnose(projects.add(workspace(tmp_path)).id)
    runner = by_code(report)["RUNNER_STATE"]
    assert runner.status is Status.FAIL and runner.blocks_execution and "PREFLIGHT_FAILED" in runner.cause
    assert "PENDING" in runner.cause and "RUNNER_STATE" in report.blockers


async def test_a_runner_owned_by_another_instance_is_a_warning_not_a_blocker(tmp_path):
    svc, projects, *_ = make(tmp_path, runtime=runtime_snapshot(L.STOPPED, S.ALREADY_ACTIVE, False))
    report = await svc.diagnose(projects.add(workspace(tmp_path)).id)
    runner = by_code(report)["RUNNER_STATE"]
    assert runner.status is Status.WARN and not runner.blocks_execution and "another process" in runner.cause
    assert "RUNNER_STATE" not in report.blockers


async def test_a_cached_report_is_not_served_after_the_live_state_it_contains_changed(tmp_path):
    svc, projects, ex, state = make(tmp_path, runtime=runtime_snapshot(L.STOPPED, S.PREFLIGHT_BLOCKED, False))
    pid = projects.add(workspace(tmp_path)).id
    assert not (await svc.diagnose(pid)).can_start
    state.runtime = READY                                     # the Runner became ready: no 30 s of stale "cannot start"
    after = await svc.diagnose(pid)
    assert not after.cached and by_code(after)["RUNNER_STATE"].status is Status.PASS
    assert (await svc.diagnose(pid)).cached                   # unchanged state still hits the cache


async def test_project_settings_that_bypass_permissions_fail_and_block(tmp_path):
    ws = workspace(tmp_path)
    (ws / ".claude").mkdir()
    (ws / ".claude" / "settings.json").write_text('{"permissions": {"defaultMode": "bypassPermissions"}}', "utf-8")
    svc, projects, *_ = make(tmp_path)
    item = by_code(await svc.diagnose(projects.add(ws).id))["CLI_SETTINGS_SAFETY"]
    assert item.status is Status.FAIL and item.blocks_execution and "bypassPermissions" in item.cause


async def test_a_linked_claude_directory_is_a_blocking_finding_and_is_never_followed(tmp_path):
    ws, outside = workspace(tmp_path), workspace(tmp_path, "outside")
    (outside / "settings.json").write_text('{"permissions": {"defaultMode": "bypassPermissions"}}', "utf-8")
    try:
        os.symlink(outside, ws / ".claude", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this user")
    svc, projects, *_ = make(tmp_path)
    item = by_code(await svc.diagnose(projects.add(ws).id))["CLI_SETTINGS_SAFETY"]
    assert item.status is Status.FAIL and "link" in item.cause


async def test_a_discovered_project_interpreter_is_reported_but_not_selected(tmp_path):
    ws = workspace(tmp_path)
    rel = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    (ws / rel).parent.mkdir(parents=True)
    (ws / rel).write_text("", "utf-8")
    svc, projects, *_ = make(tmp_path)
    item = by_code(await svc.diagnose(projects.add(ws).id))["PYTHON_INTERPRETER"]
    assert item.status is Status.WARN and "NOT used" in item.cause and "orchestrator's interpreter" in item.cause


async def test_a_gradle_project_passes_the_junit_check_with_the_fresh_run_preset_and_checks_java(tmp_path):
    ws = workspace(tmp_path)
    wrapper = "gradlew.bat" if os.name == "nt" else "gradlew"
    (ws / wrapper).write_text("", "utf-8")
    if os.name != "nt":
        (ws / wrapper).chmod(0o755)
    svc, projects, ex, _ = make(tmp_path)
    items = by_code(await svc.diagnose(projects.add(ws, {"preset": "gradle"}).id))
    assert items["TEST_WRAPPER"].status is Status.PASS and items["JAVA_RUNTIME"].status is Status.PASS
    junit = items["JUNIT_COLLECTION"]
    assert junit.status is Status.PASS and "cleanTest" in junit.cause and "build cache off" in junit.cause
    assert sorted(c[0] for c in ex.calls) == ["git", "java"]


async def test_a_gradle_command_that_can_be_skipped_as_up_to_date_still_warns(tmp_path):
    svc, *_ = make(tmp_path)
    base = {"preset": "gradle", "junit_path": "**/build/test-results/test/TEST-*.xml", "min_tests_executed": 1}
    for argv in (["gradlew", "test", "--console=plain"], ["gradlew", "cleanTest", "test"],
                 ["gradlew", "test", "--no-build-cache"]):                      # one of the two protections is not enough
        item = svc._junit_item({**base, "commands": [argv]})
        assert item.status is Status.WARN and "UP-TO-DATE" in item.cause
    item = svc._junit_item({**base, "commands": [["gradlew", "cleanTest", "test", "--no-build-cache"]]})
    assert item.status is Status.PASS


async def test_a_missing_jdk_blocks_a_gradle_project(tmp_path):
    ws = workspace(tmp_path)
    wrapper = "gradlew.bat" if os.name == "nt" else "gradlew"
    (ws / wrapper).write_text("", "utf-8")
    if os.name != "nt":
        (ws / wrapper).chmod(0o755)
    svc, projects, *_ = make(tmp_path, executor=FakeExecutor(missing={"java"}))
    report = await svc.diagnose(projects.add(ws, {"preset": "gradle"}).id)
    assert by_code(report)["JAVA_RUNTIME"].status is Status.FAIL and "JAVA_RUNTIME" in report.blockers


async def test_an_unresolvable_preset_fails_and_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.test_policy_resolver.shutil.which", lambda _tool: None)
    svc, projects, *_ = make(tmp_path)
    report = await svc.diagnose(projects.add(workspace(tmp_path), {"preset": "maven"}).id)
    assert by_code(report)["TEST_PRESET_RESOLVABLE"].status is Status.FAIL
    assert "TEST_PRESET_RESOLVABLE" in report.blockers


async def test_write_lock_and_dirty_workspace_are_warnings_not_blockers(tmp_path):
    svc, projects, *_ = make(tmp_path)
    row = projects.add(workspace(tmp_path))
    row.write_owner_task_id, row.workspace_state = uuid.uuid4(), "DIRTY_UNRESOLVED"
    report = await svc.diagnose(row.id)
    assert by_code(report)["PROJECT_WRITE_LOCK"].status is Status.WARN and report.can_start


# --- nothing that costs tokens or runs project code -------------------------------------------------------------------
async def test_only_tool_version_probes_run_and_no_probe_or_project_command_is_used(tmp_path, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("the auth/permission probe must never run from diagnostics")

    monkeypatch.setattr(preflight_module, "probe_write_blocked", forbidden)
    svc, projects, ex, _ = make(tmp_path)
    await svc.diagnose(projects.add(workspace(tmp_path)).id)
    await svc.diagnose(projects.add(workspace(tmp_path, "b"), None).id, refresh=True)
    assert ex.calls and all(c in (["git", "--version"], ["java", "-version"]) for c in ex.calls)


async def test_an_unknown_project_is_a_404(tmp_path):
    svc, *_ = make(tmp_path)
    with pytest.raises(NotFound):
        await svc.diagnose(uuid.uuid4())


# --- single flight, cache ---------------------------------------------------------------------------------------------
async def test_concurrent_requests_share_one_diagnosis(tmp_path):
    gate = asyncio.Event()
    svc, projects, ex, _ = make(tmp_path, executor=FakeExecutor(gate=gate))
    pid = projects.add(workspace(tmp_path)).id
    waiters = [asyncio.create_task(svc.diagnose(pid)) for _ in range(6)]
    await asyncio.sleep(0.05)
    assert not svc._lock.locked()                      # the diagnosis runs while the lock is free
    gate.set()
    reports = await asyncio.gather(*waiters)
    assert len({id(r) for r in reports}) == 1 and ex.calls == [["git", "--version"]]


async def test_a_cancelled_waiter_ends_alone_and_the_shared_task_survives(tmp_path):
    gate = asyncio.Event()
    svc, projects, ex, _ = make(tmp_path, executor=FakeExecutor(gate=gate))
    pid = projects.add(workspace(tmp_path)).id
    first, second = asyncio.create_task(svc.diagnose(pid)), asyncio.create_task(svc.diagnose(pid))
    await asyncio.sleep(0.05)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    report = await second
    assert report.items and len(ex.calls) == 1 and svc._inflight == {}


async def test_a_complete_result_is_cached_and_a_second_call_starts_nothing(tmp_path):
    svc, projects, ex, _ = make(tmp_path)
    pid = projects.add(workspace(tmp_path)).id
    first = await svc.diagnose(pid)
    second = await svc.diagnose(pid)
    assert not first.cached and second.cached and len(ex.calls) == 1


async def test_the_cache_expires_after_its_ttl(tmp_path):
    clock = Clock()
    svc, projects, ex, _ = make(tmp_path, clock=clock)
    pid = projects.add(workspace(tmp_path)).id
    await svc.diagnose(pid)
    clock.now += P.DIAGNOSTICS_CACHE_TTL_SECONDS + 1
    assert not (await svc.diagnose(pid)).cached and len(ex.calls) == 2


async def test_a_failed_diagnosis_is_not_cached_and_does_not_block_a_retry(tmp_path):
    svc, projects, ex, _ = make(tmp_path, executor=FakeExecutor(error=RuntimeError("boom")))
    pid = projects.add(workspace(tmp_path)).id
    with pytest.raises(RuntimeError):
        await svc.diagnose(pid)
    await asyncio.sleep(0)
    assert svc._inflight == {} and svc._cache == {}
    assert (await svc.diagnose(pid)).items


async def test_refresh_joins_a_diagnosis_that_is_already_running(tmp_path):
    gate = asyncio.Event()
    svc, projects, ex, _ = make(tmp_path, executor=FakeExecutor(gate=gate))
    pid = projects.add(workspace(tmp_path)).id
    normal = asyncio.create_task(svc.diagnose(pid))
    await asyncio.sleep(0.05)
    refresh = asyncio.create_task(svc.diagnose(pid, refresh=True))
    await asyncio.sleep(0.05)
    gate.set()
    a, b = await asyncio.gather(normal, refresh)
    assert a is b and len(ex.calls) == 1               # no second subprocess


async def test_refresh_after_completion_bypasses_the_cache(tmp_path):
    svc, projects, ex, _ = make(tmp_path)
    pid = projects.add(workspace(tmp_path)).id
    await svc.diagnose(pid)
    assert not (await svc.diagnose(pid, refresh=True)).cached and len(ex.calls) == 2


async def test_a_stale_done_callback_never_removes_a_newer_task_of_the_same_key(tmp_path):
    svc, *_ = make(tmp_path)
    key = ("k",)
    old, new = asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(0.01))
    await old
    svc._inflight[key] = new
    svc._finished(key, old)                       # the old task's callback, firing after `new` was registered
    assert svc._inflight[key] is new
    await new                                     # a done-callback only ever runs for a finished task
    svc._finished(key, new)
    assert key not in svc._inflight


async def test_a_new_refresh_task_replaces_a_finished_one_whose_callback_has_not_run(tmp_path):
    svc, projects, ex, _ = make(tmp_path)
    pid = projects.add(workspace(tmp_path)).id
    await svc.diagnose(pid)
    key = next(iter(svc._cache))
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    svc._inflight[key] = done                           # finished, callback still pending
    report = await svc.diagnose(pid, refresh=True)
    assert not report.cached and len(ex.calls) == 2


async def test_a_changed_workspace_or_policy_makes_the_old_entry_unreachable_and_evicts_it(tmp_path):
    svc, projects, ex, _ = make(tmp_path)
    row = projects.add(workspace(tmp_path))
    await svc.diagnose(row.id)
    row.test_policy = {"preset": "pytest"}
    assert not (await svc.diagnose(row.id)).cached
    assert len(svc._cache) == 1 and len(ex.calls) == 2
    row.workspace_path = str(workspace(tmp_path, "moved"))
    assert not (await svc.diagnose(row.id)).cached and len(svc._cache) == 1


async def test_a_changed_active_profile_makes_the_old_entry_unreachable_and_evicts_it(tmp_path):
    svc, projects, ex, _ = make(tmp_path)
    row = projects.add(workspace(tmp_path))                     # no active profile: profile_hash is None
    await svc.diagnose(row.id)
    assert (await svc.diagnose(row.id)).cached and len(ex.calls) == 1   # one run so far
    old_key = next(iter(svc._cache))
    row.profile_hash = "a" * 64                                 # a new profile version became active
    assert not (await svc.diagnose(row.id)).cached and len(ex.calls) == 2    # a new run
    assert len(svc._cache) == 1 and old_key not in svc._cache   # same project, different key: the old entry is evicted
    assert (await svc.diagnose(row.id)).cached                  # unchanged profile: a hit again
    row.profile_hash = None                                     # back to "no profile" is a different key as well
    assert not (await svc.diagnose(row.id)).cached and len(svc._cache) == 1


async def test_the_profile_hash_is_part_of_the_cache_key(tmp_path):
    svc, projects, _, _ = make(tmp_path)
    row = projects.add(workspace(tmp_path), profile_hash="b" * 64)
    await svc.diagnose(row.id)
    assert "b" * 64 in next(iter(svc._cache))


# --- bounded file inspection --------------------------------------------------------------------------------------------
def stall_fs(svc, release: threading.Event, started: list):
    original = svc._inspect_fs

    def blocking(view):
        started.append(view.id)
        release.wait(10)
        return original(view)

    svc._inspect_fs = blocking


async def wait_until(predicate, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < end, "condition not reached"
        await asyncio.sleep(0.01)


async def test_a_stalled_inspection_times_out_unknown_and_a_refresh_starts_no_second_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DIAGNOSTICS_ITEM_TIMEOUT_SECONDS", 0.05)
    svc, projects, ex, _ = make(tmp_path)
    release, started = threading.Event(), []
    stall_fs(svc, release, started)
    row = projects.add(workspace(tmp_path))
    try:
        first = by_code(await svc.diagnose(row.id))
        assert first["WORKSPACE_ACCESS"].status is Status.UNKNOWN and "did not finish" in first["WORKSPACE_ACCESS"].cause
        assert svc._fs_total == 1 and row.id in svc._fs_busy       # the abandoned thread still owns the slot
        second = by_code(await svc.diagnose(row.id, refresh=True))
        assert second["WORKSPACE_ACCESS"].status is Status.UNKNOWN and "still running" in second["WORKSPACE_ACCESS"].cause
        assert started == [row.id] and svc._fs_total == 1          # no second thread was started
        assert ex.calls == []
    finally:
        release.set()
    await wait_until(lambda: svc._fs_total == 0)                  # released when the thread really ended
    assert (await svc.diagnose(row.id, refresh=True)).items and len(started) == 2


async def test_the_global_cap_stops_inspections_of_other_projects_from_piling_up(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DIAGNOSTICS_ITEM_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(P, "DIAGNOSTICS_MAX_FS_WORKERS", 2)
    svc, projects, *_ = make(tmp_path)
    release, started = threading.Event(), []
    stall_fs(svc, release, started)
    rows = [projects.add(workspace(tmp_path, f"p{i}")) for i in range(4)]
    try:
        for row in rows:
            await svc.diagnose(row.id)
        assert len(started) == 2 and svc._fs_total == 2
    finally:
        release.set()
    await wait_until(lambda: svc._fs_total == 0)


# --- partial results are never cached; the overall deadline bounds the whole run ---------------------------------------------
class ProbeEndsExecutor(FakeExecutor):
    """`git --version` ends with the given outcome/exit code; every other command behaves as in FakeExecutor."""

    def __init__(self, outcome, exit_code):
        super().__init__()
        self.outcome, self.exit_code = outcome, exit_code

    async def run(self, spec, **kw):
        if spec.argv[0] != "git":
            return await super().run(spec, **kw)
        self.calls.append(list(spec.argv))
        return CommandResult(spec.argv, self.outcome, self.exit_code, "", "boom\n", 0.0)


def counted(svc):
    """Counts the real diagnosis runs (a cache hit starts none)."""
    runs, original = [], svc._run

    def run(*args, **kw):
        runs.append(1)
        return original(*args, **kw)

    svc._run = run
    return runs


def with_readers(svc, *, capability=None, tracked=None):
    svc._read_capability, svc._read_tracked = capability, tracked


async def _ok_capability():
    return SimpleNamespace(state="SUPPORTED", reason_code=None)


async def _ok_tracked(_pid):
    return TrackedVerdict()


async def _failing_tracked(_pid):
    raise RuntimeError("db down")


async def _hanging(*_args):
    await asyncio.Event().wait()


async def test_a_file_inspection_error_is_partial_and_not_cached(tmp_path):
    svc, projects, *_ = make(tmp_path)
    row = projects.add(workspace(tmp_path))
    runs = counted(svc)

    def boom(view):
        raise OSError("disk")

    svc._inspect_fs = boom
    first = by_code(await svc.diagnose(row.id))
    assert first["WORKSPACE_ACCESS"].status is Status.UNKNOWN and "failed unexpectedly" in first["WORKSPACE_ACCESS"].cause
    assert svc._cache == {} and runs == [1]
    assert not (await svc.diagnose(row.id)).cached and runs == [1, 1] and svc._cache == {}   # a fresh run, no cache hit
    await asyncio.sleep(0)
    assert svc._inflight == {}


async def test_a_file_inspection_timeout_is_partial_and_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DIAGNOSTICS_ITEM_TIMEOUT_SECONDS", 0.05)
    svc, projects, *_ = make(tmp_path)
    row, release = projects.add(workspace(tmp_path)), threading.Event()
    runs = counted(svc)
    stall_fs(svc, release, [])
    try:
        first = await svc.diagnose(row.id)
        assert "did not finish" in by_code(first)["WORKSPACE_ACCESS"].cause and svc._cache == {} and runs == [1]
    finally:
        release.set()
    await wait_until(lambda: svc._fs_total == 0)


async def test_a_busy_inspection_is_uncached_starts_no_second_worker_and_a_complete_run_follows_once_it_ended(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DIAGNOSTICS_ITEM_TIMEOUT_SECONDS", 0.05)
    svc, projects, ex, _ = make(tmp_path)
    row, release, started = projects.add(workspace(tmp_path)), threading.Event(), []
    runs = counted(svc)
    stall_fs(svc, release, started)
    try:
        await svc.diagnose(row.id)                           # times out; the thread is still alive
        busy = await svc.diagnose(row.id)                    # plain GET: a new run, which is busy, not a cache hit
        assert not busy.cached and "still running" in by_code(busy)["WORKSPACE_ACCESS"].cause
        assert runs == [1, 1] and started == [row.id] and svc._cache == {}      # no second worker, nothing cached
    finally:
        release.set()
    await wait_until(lambda: svc._fs_total == 0)
    monkeypatch.setattr(P, "DIAGNOSTICS_ITEM_TIMEOUT_SECONDS", 5.0)
    complete = await svc.diagnose(row.id)                    # the thread ended: a complete run ...
    assert not complete.cached and by_code(complete)["WORKSPACE_ACCESS"].status is Status.PASS and runs == [1, 1, 1]
    assert (await svc.diagnose(row.id)).cached and runs == [1, 1, 1]            # ... which IS cached


@pytest.mark.parametrize("outcome,exit_code", [(CliOutcome.TIMEOUT, -1), (CliOutcome.ERROR, 1)])
async def test_a_version_probe_timeout_or_error_is_partial_and_not_cached(tmp_path, outcome, exit_code):
    svc, projects, ex, _ = make(tmp_path, executor=ProbeEndsExecutor(outcome, exit_code))
    runs = counted(svc)
    row = projects.add(workspace(tmp_path))
    assert not (await svc.diagnose(row.id)).cached and svc._cache == {}
    assert not (await svc.diagnose(row.id)).cached and runs == [1, 1] and len(ex.calls) == 2


async def test_a_failed_state_read_is_partial_and_not_cached(tmp_path):
    svc, projects, *_ = make(tmp_path)
    with_readers(svc, capability=_ok_capability, tracked=_failing_tracked)
    runs = counted(svc)
    row = projects.add(workspace(tmp_path))
    first = by_code(await svc.diagnose(row.id))
    assert first["TRACKED_EXECUTIONS"].status is Status.UNKNOWN and svc._cache == {}
    assert not (await svc.diagnose(row.id)).cached and runs == [1, 1]


async def test_a_state_read_timeout_is_partial_and_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DIAGNOSTICS_ITEM_TIMEOUT_SECONDS", 0.05)
    svc, projects, *_ = make(tmp_path)
    with_readers(svc, capability=_hanging, tracked=_ok_tracked)
    runs = counted(svc)
    row = projects.add(workspace(tmp_path))
    first = by_code(await svc.diagnose(row.id))
    assert first["PROCESS_TRACKING"].status is Status.UNKNOWN and svc._cache == {}
    assert not (await svc.diagnose(row.id)).cached and runs == [1, 1]


async def test_a_complete_result_is_cached_with_readers_configured(tmp_path):
    svc, projects, *_ = make(tmp_path)
    with_readers(svc, capability=_ok_capability, tracked=_ok_tracked)
    runs = counted(svc)
    pid = projects.add(workspace(tmp_path)).id
    assert not (await svc.diagnose(pid)).cached
    assert (await svc.diagnose(pid)).cached and runs == [1]


async def test_a_genuinely_missing_tool_is_a_complete_answer_and_is_cached(tmp_path):
    svc, projects, ex, _ = make(tmp_path, executor=FakeExecutor(missing=("git",)))
    runs = counted(svc)
    pid = projects.add(workspace(tmp_path)).id
    assert by_code(await svc.diagnose(pid))["GIT_AVAILABLE"].status is Status.WARN
    assert (await svc.diagnose(pid)).cached and runs == [1] and len(ex.calls) == 1


class StageStubs:
    """Cooperative stages on a fake clock: each takes `duration` of clock time but never longer than the timeout it got."""

    def __init__(self, svc, clock, fs=4.0, probe=6.0):
        self.clock, self.fs_d, self.probe_d = clock, fs, probe
        self.fs_timeouts: list[float] = []
        self.probe_timeouts: list[float] = []
        original_fs = svc._fs

        async def fs(project_id, fn, timeout):
            self.fs_timeouts.append(timeout)
            result = await original_fs(project_id, fn, timeout)
            self.clock.now += min(self.fs_d, timeout)
            return result if self.fs_d <= timeout else ("timeout", None)

        svc._fs = fs
        svc._executor.run = self.run

    async def run(self, spec, **_kw):
        self.probe_timeouts.append(spec.timeout_seconds)
        self.clock.now += min(self.probe_d, spec.timeout_seconds)
        outcome = CliOutcome.TIMEOUT if self.probe_d > spec.timeout_seconds else CliOutcome.SUCCESS
        return CommandResult(spec.argv, outcome, 0, "git version 2\n", "", 0.0)


async def test_every_stage_gets_min_of_item_timeout_and_time_left_and_the_run_stays_inside_the_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DIAGNOSTICS_DEADLINE_SECONDS", 7.0)
    clock = Clock()
    svc, projects, *_ = make(tmp_path, clock=clock)
    called = []

    async def capability():
        called.append("capability")
        return await _ok_capability()

    async def tracked(pid):
        called.append("tracked")
        return await _ok_tracked(pid)

    with_readers(svc, capability=capability, tracked=tracked)
    stubs = StageStubs(svc, clock)
    started = clock.now
    report = await svc.diagnose(projects.add(workspace(tmp_path)).id)
    items = by_code(report)
    assert stubs.fs_timeouts == [5.0] and stubs.probe_timeouts == [3.0]       # min(5, 7), then min(5, 7 - 4)
    assert clock.now - started <= P.DIAGNOSTICS_DEADLINE_SECONDS + 1e-9
    assert called == []                                                       # no time left: the readers never started
    for code in ("PROCESS_TRACKING", "TRACKED_EXECUTIONS"):
        assert items[code].status is Status.UNKNOWN and "overall diagnostics deadline" in items[code].cause
        assert not items[code].blocks_execution
    assert svc._cache == {}                                                   # a deadline-cut report is partial
    assert report.can_start and report.blockers == ()                         # UNKNOWN never blocks (only FAIL does)


async def test_a_stage_with_no_time_left_is_not_started_and_its_items_are_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DIAGNOSTICS_DEADLINE_SECONDS", 4.0)
    clock = Clock()
    svc, projects, *_ = make(tmp_path, clock=clock)
    stubs = StageStubs(svc, clock, fs=0.0)
    stubs.fs_d = 4.0                                             # the inspection used the whole deadline, but finished
    report = await svc.diagnose(projects.add(workspace(tmp_path)).id)
    items = by_code(report)
    assert stubs.fs_timeouts == [4.0] and stubs.probe_timeouts == []         # no probe started, no forced 1 s
    assert items["GIT_AVAILABLE"].status is Status.UNKNOWN and "deadline" in items["GIT_AVAILABLE"].cause
    assert svc._cache == {}


async def test_with_nothing_left_not_even_the_file_inspection_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DIAGNOSTICS_DEADLINE_SECONDS", 0.0)
    clock = Clock()
    svc, projects, *_ = make(tmp_path, clock=clock)
    stubs = StageStubs(svc, clock, fs=0.0)
    report = await svc.diagnose(projects.add(workspace(tmp_path)).id)
    items = by_code(report)
    assert stubs.fs_timeouts == [] and stubs.probe_timeouts == []
    assert items["WORKSPACE_ACCESS"].status is Status.UNKNOWN and "overall diagnostics deadline" in items["WORKSPACE_ACCESS"].cause
    assert items["GIT_AVAILABLE"].status is Status.UNKNOWN and report.can_start and svc._cache == {}


async def test_a_reader_is_cut_at_the_remaining_time_by_the_real_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "DIAGNOSTICS_ITEM_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(P, "DIAGNOSTICS_DEADLINE_SECONDS", 0.4)
    svc, projects, *_ = make(tmp_path, clock=time.monotonic)
    with_readers(svc, capability=_hanging, tracked=_hanging)
    began = time.monotonic()
    report = await svc.diagnose(projects.add(workspace(tmp_path)).id)
    assert time.monotonic() - began < 2.0                                    # not 5 + 5 s of reader timeouts
    items = by_code(report)
    assert items["PROCESS_TRACKING"].status is Status.UNKNOWN and "deadline" in items["PROCESS_TRACKING"].cause
    assert items["TRACKED_EXECUTIONS"].status is Status.UNKNOWN and "deadline" in items["TRACKED_EXECUTIONS"].cause
    assert svc._cache == {}
