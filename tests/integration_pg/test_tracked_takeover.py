"""B2 phase 3: the lease takeover of tracked executions (design 8) on the embedded PostgreSQL, with real short-lived processes:
the tracked verdict, the JSONB-merge closing write with its proof, the `pid IS NULL` guard and the ordered row locks."""
import asyncio
import socket
import subprocess
import sys
import uuid

import psutil
import pytest
from sqlalchemy import event, select

from app.infra.process_control import ProcessIdentity, TerminationResult, capture_identity
from app.models.tables import Execution, TaskRun
from app.repositories.task_repository import TaskRepository
from app.services.lease_service import AcquireStatus, LeaseService, default_tracked_verdict
from app.services.tracked_classifier import RowClass, classify_row
from app.validation import policies as P
from tests.helpers import FakeClock, make_task
from tests.integration_pg.test_process_schema import insert_execution, make_project_task

HOST = socket.gethostname()


def svc(db, clock, **kw):
    return LeaseService(db, "runner-B", ttl_seconds=30, clock=clock, **kw)


async def first_run(db, clock):
    task = await make_task(db)
    return task, (await LeaseService(db, "runner-A", ttl_seconds=30, clock=clock).acquire(task.id)).lease


def runner_process(identity: ProcessIdentity | None, host: str = HOST) -> dict:
    return {"host": host, "pid": identity.pid if identity else 1, "create_time": identity.create_time if identity else 1.0}


async def add_tracked(db, task, run_id, *, pid=None, create_time=None, runner=None, detail=None) -> uuid.UUID:
    base = {"tracked": True, "v": 1, "checkpoint_step_key": "TEST", "runner_process": runner or runner_process(None)}
    async with db.transaction() as s:
        row = Execution(id=uuid.uuid4(), task_id=task.id, run_id=run_id, role="TESTER", provider="LOCAL",
                        outcome="STARTING", pid=pid, process_create_time=create_time,
                        detail=base if detail is None else detail)
        s.add(row)
        await s.flush()
        return row.id


async def row_of(db, execution_id) -> Execution:
    async with db.transaction() as s:
        return (await s.execute(select(Execution).where(Execution.id == execution_id))).scalar_one()


async def run_status(db, task_id) -> list[str]:
    async with db.transaction() as s:
        return sorted(r.status for r in (await s.execute(select(TaskRun).where(TaskRun.task_id == task_id))).scalars())


def finished_process() -> ProcessIdentity:
    """The identity of a process that has really exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = capture_identity(proc.pid)
    proc.wait(timeout=30)
    assert identity is not None
    return identity


async def classified(db, execution_id) -> RowClass:
    async with db.transaction() as s:
        row = await TaskRepository(s).tracked_row(execution_id)
    return classify_row(row.id, row.pid, row.finished_at, row.detail_text).row_class


# --- pid IS NULL: user code never ran ---------------------------------------------------------------------------------------------
async def test_a_tracked_row_without_a_pid_is_closed_as_never_ran_by_a_merge_that_keeps_the_tracked_keys(db):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    row_id = await add_tracked(db, task, lease.run_id)
    clock.advance(31)
    result = await svc(db, clock).acquire(task.id)
    assert result.status is AcquireStatus.ACQUIRED and result.previous_run_id == lease.run_id
    row = await row_of(db, row_id)
    assert row.finished_at is not None and row.outcome == "ERROR" and row.pid is None
    assert row.detail["proof"] == "NEVER_RAN" and row.detail["reason"] == "SUSPENDED_ORPHAN_POSSIBLE"
    assert row.detail["abandoned_by_takeover"] is True
    for key in ("tracked", "v", "checkpoint_step_key", "runner_process"):          # §14 row revision 34 A3: the merge keeps them
        assert key in row.detail
    assert row.detail["tracked"] is True
    assert await classified(db, row_id) is RowClass.RESOLVED_PROVEN
    assert await run_status(db, task.id) == ["ABANDONED", "RUNNING"]


async def test_the_merge_keeps_every_tracked_only_key_that_was_on_the_row(db):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    full = {"tracked": True, "v": 1, "checkpoint_step_key": "TEST", "runner_process": runner_process(None),
            "anomaly_original": {"x": 1}, "anomaly_settlement": {"kind": "k"}}
    row_id = await add_tracked(db, task, lease.run_id, detail=full)
    clock.advance(31)
    assert (await svc(db, clock).acquire(task.id)).status is AcquireStatus.ACQUIRED
    row = await row_of(db, row_id)
    assert all(row.detail[k] == full[k] for k in full)
    assert P.TRACKED_ONLY_KEYS >= {k for k in full if k in P.TRACKED_ONLY_KEYS}


async def test_a_pid_committed_after_txn_a_read_the_row_is_not_closed_as_never_ran(db):
    """The `pid IS NULL` guard: the owner's own identity write won the race, so the takeover is HELD and re-evaluated later."""
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    row_id = await add_tracked(db, task, lease.run_id)

    def verdict_then_identity_commits(row):
        verdict = default_tracked_verdict(row)                       # sees pid IS NULL ...
        asyncio.run_coroutine_threadsafe(commit_pid(), racing_loop).result(30)  # ... and then the owner persists its identity
        return verdict

    async def commit_pid():
        async with db.transaction() as s:
            await s.execute(Execution.__table__.update().where(Execution.id == row_id).values(pid=4242, process_create_time=1.5))

    racing_loop = asyncio.get_running_loop()
    clock.advance(31)
    result = await svc(db, clock, tracked_verdict=verdict_then_identity_commits).acquire(task.id)
    assert result.status is AcquireStatus.HELD and result.reason == "takeover raced"
    row = await row_of(db, row_id)
    assert row.finished_at is None and row.pid == 4242 and "proof" not in row.detail
    assert await run_status(db, task.id) == ["RUNNING"]              # nothing was abandoned


# --- pid + create_time: the conditional OS guarantee (H3) -------------------------------------------------------------------------
async def test_all_four_conditions_give_job_kill_on_close_assumed(db):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    dead_runner, dead_root = finished_process(), finished_process()
    row_id = await add_tracked(db, task, lease.run_id, pid=dead_root.pid, create_time=dead_root.create_time,
                               runner=runner_process(dead_runner))
    clock.advance(31)
    result = await svc(db, clock).acquire(task.id)
    assert result.status is AcquireStatus.ACQUIRED
    row = await row_of(db, row_id)
    assert row.detail["proof"] == "JOB_KILL_ON_CLOSE_ASSUMED" and row.detail["termination"] == "ALREADY_EXITED"
    assert row.finished_at is not None and row.terminated_at is not None
    assert await classified(db, row_id) is RowClass.RESOLVED_PROVEN


@pytest.mark.parametrize("case", ["runner_alive", "other_host", "no_runner_record", "root_alive"])
async def test_any_failed_condition_is_unconfirmed_never_a_proof(db, case):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    dead_runner, dead_root = finished_process(), finished_process()
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        runner, root = runner_process(dead_runner), dead_root
        if case == "runner_alive":
            runner = runner_process(capture_identity(sleeper.pid))
        elif case == "other_host":
            runner = runner_process(dead_runner, host="another-host.invalid")
        elif case == "root_alive":
            root = capture_identity(sleeper.pid)
        detail = None
        if case == "no_runner_record":
            detail = {"tracked": True, "v": 1, "checkpoint_step_key": "TEST"}
        row_id = await add_tracked(db, task, lease.run_id, pid=root.pid, create_time=root.create_time, runner=runner,
                                   detail=detail)
        clock.advance(31)
        result = await svc(db, clock).acquire(task.id)
        assert result.status is AcquireStatus.UNCERTAIN
        row = await row_of(db, row_id)
        assert row.finished_at is None and "proof" not in row.detail            # never a proof, never closed
        assert await run_status(db, task.id) == ["RUNNING"]
        assert sleeper.poll() is None                                           # a live root is NOT killed by this path
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)


async def test_h3_switched_off_makes_every_crash_unconfirmed(db):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    dead_runner, dead_root = finished_process(), finished_process()
    row_id = await add_tracked(db, task, lease.run_id, pid=dead_root.pid, create_time=dead_root.create_time,
                               runner=runner_process(dead_runner))
    clock.advance(31)
    off = lambda row: default_tracked_verdict(row, enabled=False)  # noqa: E731
    result = await svc(db, clock, tracked_verdict=off).acquire(task.id)
    assert result.status is AcquireStatus.UNCERTAIN and "H3_DISABLED" in result.reason
    assert (await row_of(db, row_id)).finished_at is None


async def test_a_survivor_child_of_the_dead_root_defeats_the_guarantee(db):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    dead_runner = finished_process()
    # `sys.executable` of a virtualenv may be a launcher whose child is the real interpreter: the root is the process that
    # actually runs the script (it prints its own pid), not the launcher.
    parent = subprocess.Popen(
        [sys.executable, "-c", "import os, subprocess, sys; c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                               " print(os.getpid(), c.pid, flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    root_pid, child_pid = (int(v) for v in parent.stdout.readline().split())
    root = capture_identity(root_pid)
    parent.stdin.close()
    parent.wait(timeout=30)                                                       # the root is gone, its child lives on
    for _ in range(100):
        if capture_identity(root_pid) is None:
            break
        await asyncio.sleep(0.05)
    try:
        row_id = await add_tracked(db, task, lease.run_id, pid=root.pid, create_time=root.create_time,
                                   runner=runner_process(dead_runner))
        clock.advance(31)
        quick = lambda row: default_tracked_verdict(row, settle_seconds=0.3)  # noqa: E731
        result = await svc(db, clock, tracked_verdict=quick).acquire(task.id)
        assert result.status is AcquireStatus.UNCERTAIN and "SURVIVORS_AFTER_SETTLE" in result.reason
        assert (await row_of(db, row_id)).finished_at is None
    finally:
        try:
            psutil.Process(child_pid).kill()
        except psutil.Error:
            pass


async def test_the_retained_job_registry_is_consulted_first_and_proves_job_empty(db):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    root = finished_process()
    row_id = await add_tracked(db, task, lease.run_id, pid=root.pid, create_time=root.create_time)
    seen = []

    async def settler(execution_id):
        seen.append(execution_id)
        return True

    def never(row):
        raise AssertionError("the OS verdict must not run when the retained Job was proven empty")

    clock.advance(31)
    result = await svc(db, clock, tracked_verdict=never, retained_settler=settler).acquire(task.id)
    assert result.status is AcquireStatus.ACQUIRED and seen == [row_id]
    assert (await row_of(db, row_id)).detail["proof"] == "JOB_EMPTY"


# --- untracked rows keep today's behaviour ------------------------------------------------------------------------------------------
async def test_an_untracked_row_is_still_replaced_whole_and_a_wrong_marker_row_is_merged_not_replaced(db):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    gone = finished_process()
    legacy = await add_tracked(db, task, lease.run_id, pid=gone.pid, create_time=gone.create_time,
                               detail={"resumed": True, "termination": "x"})
    odd = await add_tracked(db, task, lease.run_id, pid=gone.pid, create_time=gone.create_time,
                            detail={"tracked": "true", "v": 1, "note": "kept"})
    clock.advance(31)
    result = await svc(db, clock, terminator=lambda identity: TerminationResult.ALREADY_EXITED).acquire(task.id)
    assert result.status is AcquireStatus.ACQUIRED
    assert (await row_of(db, legacy)).detail == {"abandoned_by_takeover": True}            # today's whole replacement
    odd_detail = (await row_of(db, odd)).detail
    assert odd_detail == {"tracked": "true", "v": 1, "note": "kept", "abandoned_by_takeover": True}   # merged, no proof
    assert (await row_of(db, odd)).finished_at is not None


async def test_a_non_object_detail_stays_a_legacy_row_and_is_replaced_as_today(db):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    gone = finished_process()
    row_id = await insert_execution(db, task.id, "[1, 2]", finished=False, pid=gone.pid)
    async with db.transaction() as s:
        await s.execute(Execution.__table__.update().where(Execution.id == row_id)
                        .values(run_id=lease.run_id, process_create_time=gone.create_time))
    clock.advance(31)
    result = await svc(db, clock, terminator=lambda identity: TerminationResult.ALREADY_EXITED).acquire(task.id)
    assert result.status is AcquireStatus.ACQUIRED
    assert (await row_of(db, row_id)).detail == {"abandoned_by_takeover": True}


# --- ordered row locks --------------------------------------------------------------------------------------------------------------
async def test_lock_executions_ordered_sorts_the_ids_and_ignores_unknown_ones(db):
    project, task = await make_project_task(db)
    ids = [await insert_execution(db, task.id, None, finished=False) for _ in range(5)]
    async with db.transaction() as s:
        got = await TaskRepository(s).lock_executions_ordered(list(reversed(ids)) + [uuid.uuid4()] + ids[:2])
    assert got == sorted(ids)
    async with db.transaction() as s:
        assert await TaskRepository(s).lock_executions_ordered([]) == []


async def test_the_takeover_locks_once_in_id_order_before_it_updates_any_row(db):
    clock = FakeClock()
    task, lease = await first_run(db, clock)
    ids = [await add_tracked(db, task, lease.run_id) for _ in range(4)]
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", record)
    try:
        clock.advance(31)
        assert (await svc(db, clock).acquire(task.id)).status is AcquireStatus.ACQUIRED
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", record)
    locks = [i for i, st in enumerate(statements) if "FROM executions" in st and "FOR UPDATE" in st]
    updates = [i for i, st in enumerate(statements) if st.lstrip().startswith("UPDATE executions")]
    assert len(locks) == 1 and "ORDER BY id" in statements[locks[0]] and len(updates) == len(ids)
    assert locks[0] < min(updates)


async def test_two_transactions_locking_the_same_rows_in_opposite_input_order_do_not_deadlock(db):
    project, task = await make_project_task(db)
    ids = [await insert_execution(db, task.id, None, finished=False) for _ in range(6)]

    async def lock_and_hold(order):
        async with db.transaction() as s:
            got = await TaskRepository(s).lock_executions_ordered(order)
            await asyncio.sleep(0.3)
            return got

    first, second = await asyncio.wait_for(
        asyncio.gather(lock_and_hold(ids), lock_and_hold(list(reversed(ids)))), timeout=30)
    assert first == second == sorted(ids)
