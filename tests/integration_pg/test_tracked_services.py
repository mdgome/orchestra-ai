"""B2 phase 3: tracked commands through the services (design 7.1, 7.2, 6.1) on the embedded PostgreSQL.

`TestPolicyService` -> `TrackedCommandSession` (registration, fenced identity, closing write) -> `CommandExecutor`. Tests marked
`real` use the real Windows Job Object and real child processes; the others use the fake Job of the phase-2 tests so that
unconfirmed terminations and infrastructure failures can be produced. No CLI is run."""
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest
from sqlalchemy import select, update

from app.infra.command_executor import CommandExecutor
from app.infra.event_bus import EventBus
from app.infra.process_control import JobError
from app.infra.workspace_guard import WorkspaceGuard
from app.models.tables import Execution, Task
from app.repositories.task_repository import TaskRepository
from app.services.lease_service import LeaseService
from app.services.session_service import SessionService
from app.services.step_runner import (
    ProcessTrackingUnavailable, StepRunner, TaskCancelled, TerminationUnconfirmed, TrackedCommandSession,
)
from app.services.test_policy_service import TestPolicyService, TrackedCommandPort
from app.services.tracked_classifier import RowClass, classify_row
from app.validation import policies as P
from app.validation.exceptions import FailClosed, LeaseLost
from tests.harness import Harness
from tests.helpers import make_task
from tests.integration_pg.test_project_lock import make_project, new_task
from tests.unit.test_tracked_execution import FakeJob

WIN = sys.platform == "win32"
real = pytest.mark.skipif(not WIN, reason="needs the Windows Job Object")
SLEEP = ["python", "-c", "import time; time.sleep(60)"]


def policy(*commands, timeout=30) -> dict:
    return {"commands": [list(c) for c in commands], "timeout_seconds": timeout, "junit_path": "report.xml",
            "min_tests_executed": 1, "env_allow": []}


class Env:
    """One Task with a live lease, a StepRunner, an executor and the service under test."""

    def __init__(self, db, tmp_path: Path, task, lease, *, executor_kw=None, tracking_state=None, owner_check=None):
        self.db, self.task, self.lease, self.tmp = db, task, lease, tmp_path
        self.leases = lease_service_of[db]
        self.steps = StepRunner(db, self.leases, SessionService(), {}, EventBus(), cancel_poll_seconds=0.1,
                                process_tracking_state=tracking_state)
        self.executor = CommandExecutor(WorkspaceGuard(tmp_path), **(executor_kw or {}))
        self.service = TestPolicyService(self.executor)
        self.owner_check = owner_check

    def session(self, scope="s", step_id=None) -> TrackedCommandSession:
        return TrackedCommandSession(self.steps, self.lease, self.task.id, scope_id=scope, checkpoint_step_key="TEST",
                                     step_id=step_id, owner_check=self.owner_check)

    async def rows(self) -> list[Execution]:
        async with self.db.transaction() as s:
            rows = await s.execute(select(Execution).where(Execution.task_id == self.task.id).order_by(Execution.started_at))
            return list(rows.scalars())

    async def classified(self, execution_id) -> RowClass:
        async with self.db.transaction() as s:
            row = await TaskRepository(s).tracked_row(execution_id)
        return classify_row(row.id, row.pid, row.finished_at, row.detail_text).row_class


lease_service_of: dict = {}


async def make_env(db, tmp_path, **kw) -> Env:
    task = await make_task(db)
    leases = LeaseService(db, "runner-test", ttl_seconds=60)
    lease_service_of[db] = leases
    lease = (await leases.acquire(task.id)).lease
    return Env(db, tmp_path, task, lease, **kw)


# --- the happy path, registered BEFORE spawn, identity persisted BEFORE resume -----------------------------------------------------
@real
async def test_a_tracked_run_registers_before_spawn_persists_the_identity_before_resume_and_closes_with_job_empty(db, tmp_path):
    env = await make_env(db, tmp_path)
    session = env.session(step_id=None)
    seen = {}

    class Spy(TrackedCommandPort):
        scope_key, checkpoint_step_key = session.scope_key, "TEST"
        check_capability = staticmethod(session.check_capability)
        finish_command = session.finish_command
        watch_cancel = session.watch_cancel

        async def begin_command(self):
            tracked, on_started = await session.begin_command()
            async def spying(identity):
                async with db.transaction() as s:
                    row = await TaskRepository(s).tracked_row(tracked.execution_id)
                seen["at_identity"] = (row.pid, row.finished_at, row.detail_text)
                await on_started(identity)
                async with db.transaction() as s:
                    seen["after_identity"] = (await TaskRepository(s).tracked_row(tracked.execution_id)).pid
            return tracked, spying

    result = await env.service.run(tmp_path, policy(["python", "-c", "print('hello')"]), tracked=Spy())
    assert result.outcomes == ["SUCCESS"] and not result.passed                      # no JUnit: never a pass
    pid, finished_at, text_ = seen["at_identity"]
    assert pid is None and finished_at is None and '"tracked": true' in text_ and '"runner_process"' in text_
    assert seen["after_identity"] is not None
    (row,) = await env.rows()
    assert row.role == "TESTER" and row.provider == "LOCAL" and row.run_id == env.lease.run_id
    assert row.runner_id == env.lease.runner_id and row.pid and row.process_create_time and row.finished_at is not None
    assert row.outcome == "SUCCESS" and row.exit_code == 0 and row.terminated_at is None
    d = row.detail
    assert d["tracked"] is True and d["v"] == 1 and d["checkpoint_step_key"] == "TEST" and d["proof"] == "JOB_EMPTY"
    assert d["lifecycle"] == "EXITED" and d["tree"] == "EMPTY" and d["survivors"] is False
    assert set(d["runner_process"]) == {"host", "pid", "create_time"}
    assert await env.classified(row.id) is RowClass.RESOLVED_PROVEN
    assert row.id is not None and env.executor.unconfirmed_job_count() == 0


@real
async def test_a_timeout_kills_the_tree_and_the_documented_exit_code_is_the_job_terminate_code(db, tmp_path):
    env = await make_env(db, tmp_path)
    result = await env.service.run(tmp_path, policy(SLEEP, timeout=1), tracked=env.session())
    assert result.outcomes == ["TIMEOUT"] and result.exit_codes == [P.JOB_TERMINATE_EXIT_CODE]
    assert next((c for c in result.exit_codes if c), 0) == P.JOB_TERMINATE_EXIT_CODE   # what the TESTER Execution row stores
    (row,) = await env.rows()
    assert row.finished_at is not None and row.terminated_at is not None and row.exit_code == P.JOB_TERMINATE_EXIT_CODE
    assert row.detail["proof"] == "JOB_EMPTY" and row.detail["lifecycle"] == "TERMINATED" and row.detail["termination"] == "TERMINATED"
    assert await env.classified(row.id) is RowClass.RESOLVED_PROVEN


@real
async def test_a_survivor_of_a_naturally_exited_root_is_terminated_and_reported_not_leaked(db, tmp_path):
    env = await make_env(db, tmp_path)
    marker = tmp_path / "child.pid"
    spawn = ("import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
             f"open(r'{marker}', 'w').write(str(c.pid))")
    result = await env.service.run(tmp_path, policy(["python", "-c", spawn]), tracked=env.session())
    assert result.outcomes == ["SUCCESS"]
    (row,) = await env.rows()
    assert row.detail["survivors"] is True and row.detail["termination"] == "TERMINATED" and row.detail["proof"] == "JOB_EMPTY"
    assert row.terminated_at is not None
    assert not psutil.pid_exists(int(marker.read_text())) or psutil.Process(int(marker.read_text())).status() == psutil.STATUS_ZOMBIE


@real
async def test_collect_ids_is_tracked_too_and_a_non_pytest_policy_starts_nothing(db, tmp_path):
    env = await make_env(db, tmp_path)
    (tmp_path / "test_a.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    ids = await env.service.collect_ids(tmp_path, policy(["python", "-m", "pytest", "-q"]), tracked=env.session("ids"))
    assert ids == {"test_a.py::test_one"}
    (row,) = await env.rows()
    assert row.detail["proof"] == "JOB_EMPTY" and row.pid and row.finished_at is not None
    assert await env.service.collect_ids(tmp_path, policy(["npm", "test"]), tracked=env.session("none")) is None
    assert len(await env.rows()) == 1


# --- TERMINATION_UNCONFIRMED, cancel, infrastructure failures ---------------------------------------------------------------------
async def test_an_unconfirmed_termination_raises_keeps_the_row_open_and_stops_the_sequence(db, tmp_path):
    job = FakeJob(counts=lambda j: 1)                                               # the Job never proves empty
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: job})
    commands = [["python", "-c", "pass"], ["python", "-c", "pass"]]
    with pytest.raises(TerminationUnconfirmed) as info:
        await env.service.run(tmp_path, policy(*commands), tracked=env.session())
    assert info.value.checkpoint_step_key == "TEST"
    (row,) = await env.rows()                                                        # the second command never started
    assert info.value.execution_id == row.id
    assert row.finished_at is None and row.pid is not None and "proof" not in row.detail
    assert row.detail["lifecycle"] == "TERMINATION_UNCONFIRMED" and row.detail["tree"] in ("NOT_EMPTY", "UNKNOWN")
    assert await env.classified(row.id) is RowClass.UNRESOLVED
    assert env.executor.unconfirmed_job_count() == 1 and env.executor.retained_job(row.id) is job


async def test_an_unconfirmed_collect_ids_carries_the_test_checkpoint(db, tmp_path):
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: FakeJob(counts=lambda j: 1)})
    (tmp_path / "test_a.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    with pytest.raises(TerminationUnconfirmed) as info:
        await env.service.collect_ids(tmp_path, policy(["python", "-m", "pytest", "-q"]), tracked=env.session("ids"))
    assert info.value.checkpoint_step_key == "TEST"
    (row,) = await env.rows()
    assert row.finished_at is None and row.detail["checkpoint_step_key"] == "TEST"


@real
async def test_a_task_cancel_stops_the_running_command_and_raises_task_cancelled_after_the_row_is_closed(db, tmp_path):
    env = await make_env(db, tmp_path)
    marker = tmp_path / "second.txt"
    second = ["python", "-c", f"open(r'{marker}', 'w').write('ran')"]

    async def cancel_soon():
        await asyncio.sleep(1.0)
        async with db.transaction() as s:
            await s.execute(update(Task).where(Task.id == env.task.id).values(cancel_requested_at=datetime.now(UTC)))

    canceller = asyncio.create_task(cancel_soon())
    with pytest.raises(TaskCancelled):
        await env.service.run(tmp_path, policy(SLEEP, second, timeout=60), tracked=env.session())
    await canceller
    (row,) = await env.rows()                                                        # the second command was never started
    assert row.outcome == "CANCELLED" and row.finished_at is not None and row.detail["proof"] == "JOB_EMPTY"
    assert not marker.exists()
    assert await env.classified(row.id) is RowClass.RESOLVED_PROVEN


@real
async def test_cancelling_the_calling_coroutine_still_closes_the_row_and_settles_the_tree(db, tmp_path):
    env = await make_env(db, tmp_path)
    work = asyncio.create_task(env.service.run(tmp_path, policy(SLEEP, timeout=60), tracked=env.session()))
    for _ in range(200):                                                             # wait until the identity is persisted
        rows = await env.rows()
        if rows and rows[0].pid:
            break
        await asyncio.sleep(0.05)
    pid = rows[0].pid
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    (row,) = await env.rows()
    assert row.finished_at is not None and row.detail["proof"] == "JOB_EMPTY" and row.outcome == "CANCELLED"
    assert not psutil.pid_exists(pid)


@pytest.mark.parametrize("label,job_kw,reason", [
    ("assign", {"assign_error": JobError("JOB_ASSIGN_FAILED", "denied")}, "JOB_ASSIGN_FAILED"),
    ("resume", {"resume_error": JobError("RESUME_FAILED", "x")}, "RESUME_FAILED"),
])
async def test_an_infrastructure_spawn_failure_fails_closed_and_closes_the_row_as_never_ran(db, tmp_path, label, job_kw, reason):
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: FakeJob(**job_kw)})
    with pytest.raises(ProcessTrackingUnavailable) as info:
        await env.service.run(tmp_path, policy(["python", "-c", "pass"]), tracked=env.session())
    assert isinstance(info.value, FailClosed) and info.value.message == P.PROCESS_TRACKING_UNAVAILABLE
    (row,) = await env.rows()
    assert row.finished_at is not None and row.detail["proof"] == "NEVER_RAN" and row.detail["reason"] == reason
    assert row.detail["lifecycle"] == "SPAWN_FAILED"
    assert await env.classified(row.id) is RowClass.RESOLVED_PROVEN


async def test_job_creation_failure_fails_closed_before_any_process_exists(db, tmp_path):
    def broken():
        raise JobError("JOB_CREATE_FAILED", "no handles")

    env = await make_env(db, tmp_path, executor_kw={"job_factory": broken})
    with pytest.raises(ProcessTrackingUnavailable):
        await env.service.run(tmp_path, policy(["python", "-c", "pass"]), tracked=env.session())
    (row,) = await env.rows()
    assert row.detail["proof"] == "NEVER_RAN" and row.detail["reason"] == "JOB_CREATE_FAILED" and row.pid is None


async def test_an_oserror_at_spawn_stays_a_normal_error_outcome_not_a_fail_closed(db, tmp_path, monkeypatch):
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: FakeJob()})

    async def refuse(*args, **kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(env.executor, "_spawn", refuse)
    result = await env.service.run(tmp_path, policy(["python", "-c", "pass"]), tracked=env.session())
    assert result.outcomes == ["ERROR"] and not result.passed
    (row,) = await env.rows()
    assert row.finished_at is not None and row.detail["proof"] == "NEVER_RAN" and row.detail["reason"] == "SPAWN_OSERROR"


async def test_an_executable_that_is_not_found_raises_fail_closed_and_closes_the_row_as_never_ran(db, tmp_path):
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: FakeJob()})
    with pytest.raises(FailClosed) as info:
        await env.service.run(tmp_path, policy(["definitely-not-an-installed-program-xyz"]), tracked=env.session())
    assert not isinstance(info.value, ProcessTrackingUnavailable)
    (row,) = await env.rows()
    assert row.finished_at is not None and row.detail["proof"] == "NEVER_RAN" and row.detail["reason"] == "FailClosed"


# --- the fence: no user code before the identity is persisted -----------------------------------------------------------------------
@pytest.mark.parametrize("how", ["row_closed", "lease_lost"])
async def test_a_lost_fence_kills_the_suspended_process_so_no_user_code_runs(db, tmp_path, how):
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: FakeJob()})
    session = env.session()
    marker = tmp_path / "user-code-ran.txt"

    class Fenced(TrackedCommandPort):
        scope_key, checkpoint_step_key = session.scope_key, "TEST"
        check_capability = staticmethod(session.check_capability)
        finish_command = session.finish_command
        watch_cancel = session.watch_cancel

        async def begin_command(self):
            tracked, on_started = await session.begin_command()

            async def fenced(identity):
                if how == "row_closed":
                    async with db.transaction() as s:
                        await s.execute(update(Execution).where(Execution.id == tracked.execution_id)
                                        .values(finished_at=datetime.now(UTC)))
                else:
                    env.leases.revoke(env.lease.run_id)
                await on_started(identity)
            return tracked, fenced

    with pytest.raises(ProcessTrackingUnavailable):
        await env.service.run(tmp_path, policy(["python", "-c", f"open(r'{marker}', 'w').write('x')"]), tracked=Fenced())
    assert not marker.exists()                                                       # I4: user code never ran
    (row,) = await env.rows()
    assert row.finished_at is not None


# --- registration contract, capability gate, POSIX degrade ------------------------------------------------------------------------
async def test_registration_refuses_a_terminal_task_a_cancel_request_a_lost_lease_and_a_non_owner(db, tmp_path):
    env = await make_env(db, tmp_path)
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == env.task.id).values(cancel_requested_at=datetime.now(UTC)))
    with pytest.raises(TaskCancelled):
        await env.session().begin_command()
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == env.task.id).values(cancel_requested_at=None, status="FAILED"))
    with pytest.raises(FailClosed):
        await env.session().begin_command()
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == env.task.id).values(status="TESTING"))
    env.leases.revoke(env.lease.run_id)
    with pytest.raises(LeaseLost):
        await env.session().begin_command()
    assert await env.rows() == []                                                    # nothing was registered by any refusal


async def test_registration_requires_the_project_write_owner(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    lease = (await h.leases.acquire(task.id)).lease
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == task.id).values(status="TESTING"))
    session = TrackedCommandSession(h.steps, lease, task.id, scope_id="x", checkpoint_step_key="TEST",
                                    owner_check=h.locks.assert_owner)
    with pytest.raises(FailClosed):
        await session.begin_command()                                                # not the owner of the write lock
    assert await h.executions(task.id) == []
    assert await h.locks.ensure_for_step(lease, task.id, "DEVELOP", h.gates) is not None
    tracked, _ = await session.begin_command()                                       # now the owner
    assert tracked.run_key == f"{session.scope_key}:{tracked.execution_id}"
    assert session.scope_key.startswith(f"{lease.run_id}:tracked:") and tracked.cancel_scope == session.scope_key


@pytest.mark.parametrize("state,raises", [("UNAVAILABLE", True), ("UNKNOWN", True), ("SUPPORTED", False),
                                          ("UNSUPPORTED_PLATFORM", False)])
async def test_the_capability_gate_fails_closed_before_any_row_exists(db, tmp_path, state, raises):
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: FakeJob()}, tracking_state=lambda: state)
    if raises:
        with pytest.raises(ProcessTrackingUnavailable):
            await env.service.run(tmp_path, policy(["python", "-c", "pass"]), tracked=env.session())
        assert await env.rows() == []
    else:
        await env.service.run(tmp_path, policy(["python", "-c", "pass"]), tracked=env.session())


async def test_without_tracking_the_commands_degrade_to_untracked_and_no_row_is_created(db, tmp_path):
    env = await make_env(db, tmp_path, executor_kw={"tracking": False})
    result = await env.service.run(tmp_path, policy(["python", "-c", "print(1)"]), tracked=env.session())
    assert result.outcomes == ["SUCCESS"] and result.tracking == "UNTRACKED_PLATFORM"
    assert await env.rows() == []
    assert "tracking" not in result.to_dict()                                         # the stored output (and its hash) is unchanged


# --- the closing write of the caller's finally ------------------------------------------------------------------------------------
async def test_a_missing_result_after_spawn_leaves_the_row_open_and_before_spawn_it_closes_never_ran(db, tmp_path):
    from app.infra.command_executor import TrackedRun
    env = await make_env(db, tmp_path)
    for spawned in (True, False):
        session = env.session(f"s{spawned}")
        tracked, _ = await session.begin_command()
        tracked.spawned = spawned
        await session.finish_command(tracked, asyncio.CancelledError() if not spawned else None)
        row = next(r for r in await env.rows() if r.id == tracked.execution_id)
        if spawned:
            assert row.finished_at is None and row.detail["reason"] == "RESULT_MISSING" and "proof" not in row.detail
            assert await env.classified(row.id) is RowClass.UNRESOLVED
        else:
            assert row.finished_at is not None and row.detail["proof"] == "NEVER_RAN" and row.outcome == "CANCELLED"
    assert isinstance(tracked, TrackedRun)


async def test_a_closing_write_that_matches_no_open_row_is_never_success_and_never_raises(db, tmp_path, caplog):
    env = await make_env(db, tmp_path)
    session = env.session()
    tracked, _ = await session.begin_command()
    async with db.transaction() as s:               # somebody else (a takeover) already closed the row with a proof
        await s.execute(update(Execution).where(Execution.id == tracked.execution_id).values(
            finished_at=datetime.now(UTC),
            detail={"tracked": True, "v": 1, "proof": "NEVER_RAN", "abandoned_by_takeover": True}))
    tracked.spawned = False
    with caplog.at_level("WARNING"):
        await session.finish_command(tracked, None)
    assert "matched no open row" in caplog.text
    (row,) = await env.rows()
    assert row.detail == {"tracked": True, "v": 1, "proof": "NEVER_RAN", "abandoned_by_takeover": True}   # untouched


# --- through the workflow ----------------------------------------------------------------------------------------------------------
@real
async def test_the_whole_workflow_runs_every_test_command_tracked_and_ends_with_resolved_rows(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    assert await h.start(task.id) == ["COMPLETED"]
    tracked = [e for e in await h.executions(task.id) if (e.detail or {}).get("tracked")]
    assert len(tracked) >= 3                                                         # ids_before, the policy commands, ids_after
    for row in tracked:
        assert row.role == "TESTER" and row.provider == "LOCAL" and row.detail["checkpoint_step_key"] == "TEST"
        assert row.detail["proof"] == "JOB_EMPTY" and row.finished_at is not None and row.pid
    async with db.transaction() as s:
        assert [r for r in await TaskRepository(s).unresolved_for_task(task.id)] == []
    from tests.integration_pg.test_project_lock import project_row
    assert (await project_row(h, project.id)).workspace_state == "CLEAN"             # nothing unresolved: released CLEAN


async def test_an_unconfirmed_ids_before_parks_the_task_at_the_test_checkpoint_not_at_plan(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    h.tests = TestPolicyService(CommandExecutor(h.guard, job_factory=lambda: FakeJob(counts=lambda j: 1)))
    h.workflow.tests = h.tests
    task = await h.new_task()
    assert await h.start(task.id) == ["WAITING_APPROVAL"]
    approval = (await h.approvals(task.id))[0]
    assert approval.type == "RECOVERY" and approval.checkpoint_step_key == "TEST"           # not PLAN
    assert (await h.task(task.id)).status == "WAITING_APPROVAL"
    (row,) = [e for e in await h.executions(task.id) if (e.detail or {}).get("tracked")]
    assert row.finished_at is None and row.detail["lifecycle"] == "TERMINATION_UNCONFIRMED"
    assert [r.status for r in await h.runs(task.id)] == ["RUNNING"]                          # the lease is NOT released


@pytest.mark.parametrize("how", ["job_create", "capability"])
async def test_an_infrastructure_failure_fails_the_task_closed_without_consuming_an_iteration(db, tmp_path, how):
    h = Harness(db, tmp_path / "ws")

    def broken():
        raise JobError("JOB_CREATE_FAILED", "no handles")

    if how == "job_create":
        h.tests = TestPolicyService(CommandExecutor(h.guard, job_factory=broken))
        h.workflow.tests = h.tests
    else:
        h.steps._tracking_state = lambda: "UNAVAILABLE"
        h.tests = TestPolicyService(CommandExecutor(h.guard, job_factory=lambda: FakeJob()))
        h.workflow.tests = h.tests
    task = await h.new_task()
    assert await h.start(task.id) == ["FAILED"]
    done = await h.task(task.id)
    assert done.failure_reason == P.PROCESS_TRACKING_UNAVAILABLE and done.workflow_iteration == 0


async def test_an_unconfirmed_test_run_leaves_the_tester_step_running_so_the_gate_attaches_to_it(db, tmp_path):
    """Design 7.2 item 2 ('to be confirmed by test'): the exception comes from inside `evaluate`, so the tester step is still
    RUNNING; the RECOVERY gate attaches to it and the checkpoint is its own step key."""
    h = Harness(db, tmp_path / "ws")
    jobs = iter([FakeJob(), FakeJob(counts=lambda j: 1)])                            # ids_before is fine, the run is not
    h.tests = TestPolicyService(CommandExecutor(h.guard, job_factory=lambda: next(jobs)))
    h.workflow.tests = h.tests
    task = await h.new_task()
    assert await h.start(task.id) == ["WAITING_APPROVAL"]
    tester = [s for s in await h.steps_of(task.id) if s.step_key == "TEST"]
    assert [s.status for s in tester] == ["NEEDS_MANUAL_RECOVERY"]
    approval = (await h.approvals(task.id))[0]
    assert approval.step_id == tester[0].id and approval.checkpoint_step_key == "TEST"
    rows = sorted([e for e in await h.executions(task.id) if (e.detail or {}).get("tracked")], key=lambda e: e.started_at)
    assert [r.finished_at is None for r in rows] == [False, True]                    # ids_before closed, the run left open
    assert (await h.task(task.id)).workflow_iteration == 0


async def test_a_cancelled_test_run_cancels_the_task_and_is_not_a_failed_test(db, tmp_path):
    class Cancelling(TestPolicyService):
        async def run(self, *args, **kwargs):
            raise TaskCancelled("cancelled")

    h = Harness(db, tmp_path / "ws")
    h.tests = Cancelling(CommandExecutor(h.guard))
    h.workflow.tests = h.tests
    task = await h.new_task()
    assert await h.start(task.id) == ["CANCELLED"]
    done = await h.task(task.id)
    assert done.status == "CANCELLED" and done.workflow_iteration == 0               # no failed test, no iteration consumed
    assert [s.status for s in await h.steps_of(task.id) if s.step_key == "TEST"] == ["CANCELLED"]
