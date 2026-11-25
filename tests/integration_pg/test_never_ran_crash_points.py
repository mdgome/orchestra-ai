"""Crash points of a tracked run and the `pid IS NULL -> NEVER_RAN` rule (design I4, 5.1, 6.1 and 8) on the embedded PostgreSQL.

The real `TrackedCommandSession` + `CommandExecutor` (a fake Job over the REAL suspended process on Windows) is frozen at each
point of the 5.1 sequence; what a crash would leave behind is read at that moment (the row, the process) and judged by the real
verdict, the real sweeper and the real takeover. A crash cannot be forced in-process, so the frozen state IS the crash state.

    CP1  STARTING row inserted, nothing spawned           pid NULL   no process
    CP2  spawned (CREATE_SUSPENDED), not assigned          pid NULL   suspended process, NO Job
    CP3  assigned to the Job, identity not persisted       pid NULL   suspended process in the Job (KILL_ON_JOB_CLOSE)
    CP4  identity persisted, not resumed                   pid set    suspended process in the Job
    CP5  resumed                                           pid set    user code running
"""
import asyncio
import sys
import time
from pathlib import Path

import psutil
import pytest
from sqlalchemy import select, text

from app.infra.command_executor import CommandExecutor, CommandSpec
from app.infra.workspace_guard import WorkspaceGuard
from app.models.tables import Execution
from app.repositories.task_repository import TaskRepository
from app.services import orphan_sweeper as OS
from app.services.lease_service import LeaseService, StaleExecution, TrackedVerdict, default_tracked_verdict
from app.services.orphan_sweeper import OrphanSweeper
from app.services.tracked_classifier import RowClass, classify_row
from app.validation import policies as P
from app.validation.exceptions import FailClosed
from app.validation.validators import build_sweeper_pass_policy
from tests.integration_pg.test_orphan_sweeper import add_row, detail_of, klass, section, tracked
from tests.integration_pg.test_process_schema import make_project_task
from tests.integration_pg.test_tracked_services import make_env
from tests.unit.test_tracked_execution import FakeJob

WIN = sys.platform == "win32"
pytestmark = pytest.mark.skipif(not WIN, reason="the suspended spawn of the tracked protocol exists on Windows only")

TOKEN = "never_ran_marker"


@pytest.fixture(autouse=True)
def fast_constants(monkeypatch):
    monkeypatch.setattr(P, "PROCESS_TERMINATE_GRACE_SECONDS", 1.0)
    monkeypatch.setattr(P, "TREE_SETTLE_POLL_SECONDS", 0.3)
    monkeypatch.setattr(P, "PUMP_DRAIN_SECONDS", 1.0)
    monkeypatch.setattr(P, "TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS", 60.0)


def user_process(tmp_path) -> psutil.Process | None:
    """The spawned child of THIS test process whose command line carries the marker token (the root of the run)."""
    for child in psutil.Process().children(recursive=False):
        try:
            if TOKEN in " ".join(child.cmdline()):
                return child
        except psutil.Error:
            continue
    return None


def marker(tmp_path) -> Path:
    return tmp_path / "user_code_ran.txt"


def spec_for(tmp_path) -> CommandSpec:
    code = f"import pathlib, time  # {TOKEN}\npathlib.Path(r'{marker(tmp_path)}').write_text('x')\ntime.sleep(60)"
    return CommandSpec(argv=[sys.executable, "-c", code], cwd=tmp_path, idle_seconds=None, timeout_seconds=120)


class Frozen:
    """One tracked run frozen at a named point; `release()` lets it go on, `cancel()` ends it through the normal settlement."""

    def __init__(self, env, point: str, job: FakeJob):
        self.env, self.point, self.job = env, point, job
        self.reached, self.gate = asyncio.Event(), asyncio.Event()
        self.task: asyncio.Task | None = None
        self.tracked_run = None
        self.error: BaseException | None = None

    async def start(self, tmp_path):
        session = self.env.session()
        self.tracked_run, on_started = await session.begin_command()        # CP1: the row exists, nothing else
        self.session = session
        if self.point == "CP1":
            return self
        executor = self.env.executor
        if self.point == "CP2":
            original = executor._start_phase

            async def frozen_start(run, spec, cancel_event, callback):
                self.reached.set()
                await self.gate.wait()
                return await original(run, spec, cancel_event, callback)
            executor._start_phase = frozen_start
        else:
            real = on_started

            async def hooked(identity):
                if self.point == "CP3":
                    self.reached.set()
                    await self.gate.wait()
                await real(identity)
                if self.point == "CP4":
                    self.reached.set()
                    await self.gate.wait()
            on_started = hooked

        async def drive():
            try:
                await executor.run(spec_for(tmp_path), tracked=self.tracked_run, on_started=on_started)
            except BaseException as exc:  # noqa: BLE001 - recorded for the test
                self.error = exc
            finally:
                await self.session.finish_command(self.tracked_run, self.error)
        self.task = asyncio.create_task(drive())
        if self.point == "CP5":
            for _ in range(200):
                if marker(tmp_path).exists():
                    break
                await asyncio.sleep(0.1)
        else:
            await asyncio.wait_for(self.reached.wait(), 30)
        return self

    async def row(self):
        async with self.env.db.transaction() as s:
            return await TaskRepository(s).tracked_row(self.tracked_run.execution_id)

    async def end(self):
        """Settle the run the normal way and kill whatever is left (the test's own cleanup)."""
        self.gate.set()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        proc = user_process(None)
        if proc is not None:
            proc.kill()


async def freeze(db, tmp_path, point, job=None):
    job = job or FakeJob()
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: job})
    return await Frozen(env, point, job).start(tmp_path)


def stale_of(row) -> StaleExecution:
    import json
    detail = json.loads(row.detail_text) if row.detail_text else {}
    return StaleExecution(row.id, row.pid, row.process_create_time, detail)


async def grace():
    await asyncio.sleep(1.5)                                            # a resumed python would have written its marker by now


# --- CP1: crash after the STARTING insert, before the spawn ---------------------------------------------------------------------
async def test_cp1_row_without_pid_and_no_process_is_never_ran_and_nothing_survives(db, tmp_path):
    f = await freeze(db, tmp_path, "CP1")
    row = await f.row()
    assert row.pid is None and row.finished_at is None
    assert user_process(tmp_path) is None and not marker(tmp_path).exists()               # nothing was spawned
    verdict = default_tracked_verdict(stale_of(row))
    assert verdict.confirmed and verdict.proof == P.PROOF_NEVER_RAN and verdict.reason == P.TRACKED_SUSPENDED_ORPHAN_POSSIBLE
    await f.end()


# --- CP2: crash after the spawn, before the Job assignment ----------------------------------------------------------------------
async def test_cp2_spawned_but_not_assigned_never_ran_user_code_yet_a_suspended_process_survives(db, tmp_path):
    """The accepted residual of the contract (design 8, `SUSPENDED_ORPHAN_POSSIBLE`): the row cannot tell CP1 from CP2, user code
    cannot have run (the primary thread was never resumed), but a Job-less suspended process exists until someone kills it."""
    f = await freeze(db, tmp_path, "CP2")
    row = await f.row()
    proc = user_process(tmp_path)
    assert row.pid is None and row.finished_at is None
    assert proc is not None and proc.is_running() and f.job.calls == []                 # exists, never assigned to the Job
    await grace()
    assert not marker(tmp_path).exists()                                                # suspended: no user code
    verdict = default_tracked_verdict(stale_of(row))
    assert verdict.confirmed and verdict.proof == P.PROOF_NEVER_RAN and verdict.reason == P.TRACKED_SUSPENDED_ORPHAN_POSSIBLE
    await f.end()


# --- CP3: crash after the assignment, before the identity persist ---------------------------------------------------------------
async def test_cp3_a_takeover_that_closes_the_row_fences_the_owner_so_the_process_is_killed_and_never_resumed(db, tmp_path):
    f = await freeze(db, tmp_path, "CP3")
    row = await f.row()
    assert row.pid is None and f.job.calls == ["assign"] and user_process(tmp_path) is not None
    await grace()
    assert not marker(tmp_path).exists()
    # the sweeper (a takeover would do the same write) settles the row as NEVER_RAN while the owner is still frozen
    _, _task = None, None
    sweep = OrphanSweeper(db, section, incarnation=lambda: "inc-1", retained_settler=None)
    # the row belongs to the Task of this env: widen the sweeper's view by expiring the run lease of that Task
    async with db.transaction() as s:
        await s.execute(text("UPDATE task_runs SET lease_expires_at = now() - interval '1 hour' WHERE id = :r"),
                        {"r": f.env.lease.run_id})
    out = await sweep(None, build_sweeper_pass_policy())
    assert out.kind == "OK"
    closed = await f.row()
    assert closed.finished_at is not None and detail_of(closed)["proof"] == "NEVER_RAN" and closed.pid is None
    f.gate.set()                                                                         # the owner continues: the identity write is fenced
    await asyncio.wait_for(f.task, 30)
    assert marker(tmp_path).exists() is False                                            # the command never ran
    assert f.job.calls.count("resume") == 0 and "terminate" in f.job.calls
    assert user_process(tmp_path) is None or not user_process(tmp_path).is_running()
    after = await f.row()
    assert after.pid is None and detail_of(after)["proof"] == "NEVER_RAN"                # no identity was ever written
    await f.end()


async def test_cp3_an_identity_committed_first_makes_the_row_not_a_never_ran_candidate(db, tmp_path):
    """The other order of the same race: the owner's identity write wins, the NEVER_RAN close matches no row."""
    f = await freeze(db, tmp_path, "CP3")
    stale_row = await f.row()                                                            # the sweeper read pid NULL
    verdict = default_tracked_verdict(stale_of(stale_row))
    assert verdict.proof == P.PROOF_NEVER_RAN
    f.gate.set()                                                                         # the owner persists its identity ...
    for _ in range(100):
        if (await f.row()).pid is not None:
            break
        await asyncio.sleep(0.1)
    async with db.transaction() as s:                                                    # ... and the stale verdict is applied
        wrote = await TaskRepository(s).write_proof_to_unresolved(
            stale_row, merge={"proof": "NEVER_RAN", "reason": verdict.reason}, close=True)
        anomaly_style = await TaskRepository(s).settle_anomaly(
            stale_row, proof="NEVER_RAN", termination=None, settlement={"kind": "X"}, original_keys=[])
    assert wrote is False and anomaly_style is False
    row = await f.row()
    assert row.pid is not None and row.finished_at is None and "proof" not in detail_of(row)
    await f.end()


# --- CP4 and CP5: the identity is persisted -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("point", ["CP4", "CP5"])
async def test_cp4_cp5_a_row_with_a_pid_is_never_judged_by_the_pid_null_rule(db, tmp_path, point):
    f = await freeze(db, tmp_path, point)
    row = await f.row()
    assert row.pid is not None and row.process_create_time is not None and row.finished_at is None
    if point == "CP4":
        await grace()
        assert not marker(tmp_path).exists()                                             # persisted BEFORE the resume (I4)
        assert f.job.calls == ["assign"]                                                 # not resumed yet
    else:
        assert marker(tmp_path).exists() and f.job.calls[-1] == "resume"                 # user code did run
    verdict = default_tracked_verdict(stale_of(row))
    assert not verdict.confirmed and verdict.proof is None                               # this Runner is alive: never proven
    assert verdict.reason == "RUNNER_NOT_PROVEN_DEAD"
    await f.end()


# --- a suspended process that was NOT proven gone: the row records it, NEVER_RAN is not proven -----------------------------------
async def test_a_pid_less_row_that_records_an_unproven_spawned_process_is_not_never_ran_in_any_judge(db, tmp_path):
    """The identity persist fails after a spawn and the suspended process cannot be proven gone (unkillable Job): the executor
    leaves the row OPEN with `lifecycle=TERMINATION_UNCONFIRMED` and no pid. The lease takeover, the sweeper and the risk
    verdict must not turn it into NEVER_RAN."""
    job = FakeJob(unkillable=True, counts=lambda j: 1)          # the Job never reports empty
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: job})
    session = env.session()
    run_tracked, on_started = await session.begin_command()

    async def failing_identity(identity):
        raise OSError("the identity write failed")

    result = None
    try:
        result = await env.executor.run(spec_for(tmp_path), tracked=run_tracked, on_started=failing_identity)
    finally:
        await session.finish_command(run_tracked, None)
    assert result.lifecycle.value == "TERMINATION_UNCONFIRMED"
    async with db.transaction() as s:
        row = await TaskRepository(s).tracked_row(run_tracked.execution_id)
    assert row.pid is None and row.finished_at is None
    detail = detail_of(row)
    assert detail["lifecycle"] == "TERMINATION_UNCONFIRMED" and "proof" not in detail
    # 1. the verdict
    verdict = default_tracked_verdict(stale_of(row))
    assert verdict.confirmed is False and verdict.reason == P.TRACKED_SPAWNED_PROCESS_UNPROVEN
    # 2. the sweeper leaves the row untouched
    async with db.transaction() as s:
        await s.execute(text("UPDATE task_runs SET lease_expires_at = now() - interval '1 hour' WHERE id = :r"),
                        {"r": env.lease.run_id})
    await OrphanSweeper(db, section, incarnation=lambda: "inc-1")(None, build_sweeper_pass_policy())
    again = await tracked(db, run_tracked.execution_id)
    assert again.finished_at is None and again.pid is None and "proof" not in detail_of(again)
    assert await klass(db, run_tracked.execution_id) is RowClass.UNRESOLVED
    # 3. the takeover is UNCERTAIN and closes nothing
    taker = LeaseService(db, "runner-B", ttl_seconds=30)
    acquired = await taker.acquire(env.task.id)
    assert acquired.status.value == "UNCERTAIN"
    assert (await tracked(db, run_tracked.execution_id)).finished_at is None
    # 4. even a verdict that was computed BEFORE the evidence was written cannot close the row (SQL guard)
    stale_before = row.__class__(row.id, row.task_id, None, None, None, '{"tracked": true, "v": 1}')
    async with db.transaction() as s:
        wrote = await TaskRepository(s).write_proof_to_unresolved(
            stale_before, merge={"proof": "NEVER_RAN"}, close=True)
    assert wrote is False
    job.cleanup()


async def test_the_never_ran_close_of_the_takeover_carries_the_evidence_guard(db):
    """The takeover's own UPDATE: a row that gets `TERMINATION_UNCONFIRMED` evidence between Txn A and Txn B is not closed."""
    from tests.integration_pg.test_tracked_takeover import add_tracked, first_run, row_of
    from tests.helpers import FakeClock
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    row_id = await add_tracked(db, task, lease.run_id)
    seen_verdict = {}

    def verdict_then_evidence(row):
        seen_verdict["v"] = default_tracked_verdict(row)                                # judged on pid NULL, no evidence
        import json
        import asyncio as aio
        return seen_verdict["v"]

    clock.advance(31)
    svc = LeaseService(db, "runner-B", ttl_seconds=30, clock=clock, tracked_verdict=verdict_then_evidence)
    original = svc._close_abandoned

    async def with_evidence(s, repo, stale, verdicts):
        await s.execute(text("UPDATE executions SET detail = detail || '{\"lifecycle\": \"TERMINATION_UNCONFIRMED\"}'::jsonb "
                             "WHERE id = :i"), {"i": row_id})
        await original(s, repo, stale, verdicts)
    svc._close_abandoned = with_evidence
    out = await svc.acquire(task.id)
    assert seen_verdict["v"].proof == P.PROOF_NEVER_RAN
    assert out.status.value == "HELD"                                                    # _TakeoverRaced: rolled back, re-evaluated
    row = await row_of(db, row_id)
    assert row.finished_at is None and "proof" not in row.detail


# --- the sweeper's anomaly settlement ------------------------------------------------------------------------------------------------
async def test_an_anomaly_row_without_a_pid_is_settled_never_ran_only_while_its_identity_is_unchanged(db):
    _, task = await make_project_task(db)
    bad = {"tracked": True, "v": 1, "proof": "NOT_A_PROOF"}
    row_id = await add_row(db, task, detail=bad, finished=True)
    row = await tracked(db, row_id)
    assert classify_row(row.id, row.pid, row.finished_at, row.detail_text).row_class is RowClass.ANOMALY
    async with db.transaction() as s:                                                    # the owner committed an identity meanwhile
        await s.execute(text("UPDATE executions SET pid = 4242, process_create_time = 1.5 WHERE id = :i"), {"i": row_id})
    async with db.transaction() as s:
        wrote = await TaskRepository(s).settle_anomaly(
            row, proof="NEVER_RAN", termination=None, settlement={"kind": "INVALID_PROOF"}, original_keys=["proof"])
    assert wrote is False
    after = await tracked(db, row_id)
    assert detail_of(after)["proof"] == "NOT_A_PROOF" and after.pid == 4242


# --- a retained Job whose executor write was LOST: the registry is the evidence --------------------------------------------------
async def test_a_pid_less_row_with_a_retained_unproven_job_and_a_lost_evidence_write_is_never_ran_in_no_judge(db, tmp_path):
    """Same as above, but the executor's closing write (`finish_command`) is LOST, so the row is a bare pid-less STARTING row with
    no `TERMINATION_UNCONFIRMED` evidence. The Job is still retained in this Runner's registry and cannot be proven empty: the
    takeover, the sweeper and the risk worker consult the registry and must not close the row as NEVER_RAN (which would release
    the project / allow a re-run while the suspended process may still exist). Once the Job is proven empty the row is provable."""
    from app.services.process_risk_worker import UNCLEAR, ProcessRiskWorker
    killable = []
    job = FakeJob(unkillable=True, counts=lambda j: 0 if killable else 1)
    env = await make_env(db, tmp_path, executor_kw={"job_factory": lambda: job})
    ex = env.executor
    session = env.session()
    run_tracked, _ = await session.begin_command()

    async def failing_identity(identity):
        raise OSError("the identity write failed")

    result = await ex.run(spec_for(tmp_path), tracked=run_tracked, on_started=failing_identity)   # finish_command NOT called
    assert result.lifecycle.value == "TERMINATION_UNCONFIRMED" and ex.has_retained(run_tracked.execution_id)
    async with db.transaction() as s:
        row = await TaskRepository(s).tracked_row(run_tracked.execution_id)
    assert row.pid is None and row.finished_at is None and "lifecycle" not in detail_of(row)       # no evidence on the row
    assert default_tracked_verdict(stale_of(row)).proof == P.PROOF_NEVER_RAN                       # the row alone would say NEVER_RAN
    async with db.transaction() as s:
        await s.execute(text("UPDATE task_runs SET lease_expires_at = now() - interval '1 hour' WHERE id = :r"),
                        {"r": env.lease.run_id})
    try:
        # sweeper
        await OrphanSweeper(db, section, incarnation=lambda: "inc-1", retained_settler=ex.resettle_retained,
                            retained_present=ex.has_retained)(None, build_sweeper_pass_policy())
        assert (await tracked(db, run_tracked.execution_id)).finished_at is None
        # risk worker
        worker = ProcessRiskWorker(db, section, retained_settler=ex.resettle_retained, retained_present=ex.has_retained)
        target = await worker._judge(row, worker._clock() + 30)
        assert target.verdict == UNCLEAR and not target.tracked.confirmed
        # takeover
        taker = LeaseService(db, "runner-B", ttl_seconds=30, retained_settler=ex.resettle_retained,
                             retained_present=ex.has_retained)
        acquired = await taker.acquire(env.task.id)
        assert acquired.status.value == "UNCERTAIN" and P.TRACKED_SPAWNED_PROCESS_UNPROVEN in acquired.reason
        assert (await tracked(db, run_tracked.execution_id)).finished_at is None
        # the OS lets the Job be killed: now the registry proves it empty and the row is settled (JOB_EMPTY, not NEVER_RAN)
        job._unkillable = False
        killable.append(True)
        acquired = await taker.acquire(env.task.id)
        assert acquired.status.value == "ACQUIRED"
        closed = await tracked(db, run_tracked.execution_id)
        assert closed.finished_at is not None and detail_of(closed)["proof"] == P.PROOF_JOB_EMPTY
        assert not ex.has_retained(run_tracked.execution_id)
    finally:
        job.cleanup()
