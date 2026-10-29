import asyncio
import subprocess
import sys
import uuid
from datetime import timedelta

import psutil
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.infra.process_control import ProcessIdentity, TerminationResult, capture_identity, terminate_tree
from app.models.tables import TaskRun
from app.repositories.task_repository import TaskRepository
from app.services.lease_service import AcquireStatus, LeaseService
from app.validation.exceptions import LeaseLost
from tests.helpers import FakeClock, add_execution, make_task


def svc(db, runner="runner-A", clock=None, terminator=None, ttl=30):
    kw = {"terminator": terminator} if terminator else {}
    return LeaseService(db, runner, ttl_seconds=ttl, clock=clock or FakeClock(), **kw)


async def runs(db, task_id):
    async with db.transaction() as s:
        return list((await s.execute(select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.started_at))).scalars())


async def test_concurrent_acquire_exactly_one_wins(db):
    task = await make_task(db)
    clock = FakeClock()
    results = await asyncio.gather(*[svc(db, f"r{i}", clock).acquire(task.id) for i in range(8)])
    assert sorted(r.status for r in results).count(AcquireStatus.ACQUIRED) == 1
    assert all(r.status in (AcquireStatus.ACQUIRED, AcquireStatus.HELD) for r in results)
    assert [r.status for r in await runs(db, task.id)] == ["RUNNING"]


async def test_live_lease_blocks_second_runner(db):
    task = await make_task(db)
    clock = FakeClock()
    assert (await svc(db, "A", clock).acquire(task.id)).status is AcquireStatus.ACQUIRED
    clock.advance(5)
    assert (await svc(db, "B", clock).acquire(task.id)).status is AcquireStatus.HELD


async def test_partial_unique_index_is_the_last_line_of_defence(db):
    task = await make_task(db)
    clock = FakeClock()
    await svc(db, "A", clock).acquire(task.id)
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            await TaskRepository(s).insert_run(task.id, "B", clock.now, 30)


async def test_expired_lease_without_processes_is_taken_over_atomically(db):
    task = await make_task(db)
    clock = FakeClock()
    first = await svc(db, "A", clock).acquire(task.id)
    clock.advance(31)
    second = await svc(db, "B", clock).acquire(task.id)
    assert second.status is AcquireStatus.ACQUIRED and second.previous_run_id == first.lease.run_id
    rs = await runs(db, task.id)
    assert sorted(r.status for r in rs) == ["ABANDONED", "RUNNING"]


async def test_takeover_terminates_a_live_previous_cli_and_proceeds(db):
    task = await make_task(db)
    clock = FakeClock()
    first = await svc(db, "A", clock).acquire(task.id)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    ident = capture_identity(proc.pid)
    await add_execution(db, task, first.lease.run_id, pid=ident.pid, create_time=ident.create_time)
    clock.advance(31)
    try:
        result = await svc(db, "B", clock).acquire(task.id)
        assert result.status is AcquireStatus.ACQUIRED
        proc.wait(timeout=10)
        assert not psutil.pid_exists(proc.pid) or proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.parametrize("verdict", [TerminationResult.ACCESS_DENIED, TerminationResult.STILL_ALIVE,
                                     TerminationResult.IDENTITY_MISMATCH, TerminationResult.UNKNOWN])
async def test_unconfirmed_termination_blocks_takeover(db, verdict):
    task = await make_task(db)
    clock = FakeClock()
    first = await svc(db, "A", clock).acquire(task.id)
    await add_execution(db, task, first.lease.run_id, pid=4242, create_time=1.0)
    clock.advance(31)
    result = await svc(db, "B", clock, terminator=lambda ident: verdict).acquire(task.id)
    assert result.status is AcquireStatus.UNCERTAIN
    rs = await runs(db, task.id)
    assert [r.status for r in rs] == ["RUNNING"] and rs[0].id == first.lease.run_id   # no new Run


async def test_execution_without_process_identity_is_uncertain(db):
    """STARTING was committed but we never learned whether a process was spawned."""
    task = await make_task(db)
    clock = FakeClock()
    first = await svc(db, "A", clock).acquire(task.id)
    await add_execution(db, task, first.lease.run_id, pid=None)
    clock.advance(31)
    result = await svc(db, "B", clock, terminator=lambda i: TerminationResult.TERMINATED).acquire(task.id)
    assert result.status is AcquireStatus.UNCERTAIN


async def test_takeover_is_abandoned_when_lease_is_renewed_during_os_work(db):
    """No row lock is held while the OS is inspected, so the old owner may legitimately recover."""
    task = await make_task(db)
    clock = FakeClock()
    first = await svc(db, "A", clock).acquire(task.id)
    await add_execution(db, task, first.lease.run_id, pid=777, create_time=2.0)
    clock.advance(31)
    lock_probe: dict = {}

    def terminator(ident):
        # Runs while acquire() is in its OS phase. Prove the task row is NOT locked, then renew the lease.
        async def inner():
            async with db.transaction() as s:
                await s.execute(text("SET LOCAL lock_timeout = '300ms'"))
                await TaskRepository(s).lock(task.id)            # would time out if acquire held the lock
                lock_probe["lock_free"] = True
                await s.execute(update(TaskRun).where(TaskRun.id == first.lease.run_id).values(
                    lease_expires_at=clock.now + timedelta(seconds=30), row_version=TaskRun.row_version + 1))
        asyncio.run_coroutine_threadsafe(inner(), loop).result(10)
        return TerminationResult.TERMINATED

    loop = asyncio.get_running_loop()
    result = await svc(db, "B", clock, terminator=terminator).acquire(task.id)
    assert lock_probe.get("lock_free") is True
    assert result.status is AcquireStatus.HELD
    rs = await runs(db, task.id)
    assert [r.status for r in rs] == ["RUNNING"] and rs[0].runner_id == "A"


async def test_verify_raises_lease_lost_after_takeover(db):
    task = await make_task(db)
    clock = FakeClock()
    a = svc(db, "A", clock)
    first = await a.acquire(task.id)
    clock.advance(31)
    assert (await svc(db, "B", clock).acquire(task.id)).status is AcquireStatus.ACQUIRED
    with pytest.raises(LeaseLost):
        async with db.transaction() as s:
            await a.verify(s, first.lease)


async def test_heartbeat_extends_owner_only_and_not_after_expiry(db):
    task = await make_task(db)
    clock = FakeClock()
    a = svc(db, "A", clock)
    lease = (await a.acquire(task.id)).lease
    clock.advance(20)
    assert await a.heartbeat(lease) is True
    other = svc(db, "B", clock)
    assert await other.heartbeat(type(lease)(lease.run_id, lease.task_id, "B")) is False
    clock.advance(31)
    assert await a.heartbeat(lease) is False      # expired leases cannot be resurrected


async def test_release_allows_a_fresh_run(db):
    task = await make_task(db)
    clock = FakeClock()
    a = svc(db, "A", clock)
    lease = (await a.acquire(task.id)).lease
    await a.release(lease, "WAITING_APPROVAL")
    assert (await svc(db, "B", clock).acquire(task.id)).status is AcquireStatus.ACQUIRED
    assert sorted(r.status for r in await runs(db, task.id)) == ["FINISHED", "RUNNING"]
