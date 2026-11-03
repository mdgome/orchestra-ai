"""Bounded parallel dispatch, claim fencing, stale-claim rules and persistent backoff (real PostgreSQL)."""
import asyncio
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from app.infra.command_executor import CommandExecutor, CommandSpec
from app.models.states import CliOutcome
from app.models.tables import ExecutionRequest, Task
from app.repositories.approval_repository import ExecutionRequestRepository
from app.services.lease_service import AcquireStatus, LeaseService
from app.services.project_service import ProjectService
from app.services.request_backoff import defer_delay
from app.services.runner import Runner
from app.validation import policies as P
from tests.harness import PLAN, Harness, structured, wait_for, recover_for_test


# --- helpers -------------------------------------------------------------------------------------------------------
async def make_project(h: Harness, tmp_path: Path, name: str):
    root = tmp_path / "projects" / name
    root.mkdir(parents=True)
    service = ProjectService(h.db, None, clone_root=tmp_path / "clones", protected=(h.guard.root,))
    return await service.create_project(name=name, workspace_path=str(root))


async def project_task(h: Harness, project, text_: str = "implement add(a, b)") -> Task:
    return await h.tasks.create_task(requirements=text_, project_id=project.id)


async def request_for(h: Harness, task_id) -> ExecutionRequest:
    await h.tasks.request_execution(task_id)
    async with h.db.transaction() as s:
        return (await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task_id))).scalar_one()


async def load(h: Harness, request_id) -> ExecutionRequest:
    async with h.db.transaction() as s:
        return (await s.execute(select(ExecutionRequest).where(ExecutionRequest.id == request_id))).scalar_one()


async def claim(h: Harness, **kw) -> ExecutionRequest | None:
    async with h.db.transaction() as s:
        return await ExecutionRequestRepository(s).claim_next(h.runner.runner_id, **kw)


async def age_claim(h: Harness, request_id, seconds: int) -> None:
    async with h.db.transaction() as s:
        await s.execute(text("update execution_requests set claimed_at = now() - make_interval(secs => :n) "
                             "where id = :i"), {"n": seconds, "i": request_id})


def make_runner(h: Harness, *, tasks=2, per_project=1, grace=0.3) -> Runner:
    return Runner(h.db, h.leases, h.workflow, h.gates, [h.claude, h.codex], advisory_key=987_654_322,
                  claim_timeout_seconds=300, poll_seconds=0.05, max_concurrent_tasks=tasks,
                  max_tasks_per_project=per_project, shutdown_grace_seconds=grace)


class Gate:
    """Planner handler that records concurrency and waits until released."""

    def __init__(self):
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.started: list = []

    async def planner(self, call):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.append(call.cwd)
        try:
            await self.release.wait()
        finally:
            self.active -= 1
        return structured(**PLAN)


async def statuses(h: Harness, *task_ids) -> list[str]:
    return [(await h.task(t)).status for t in task_ids]


# --- backoff -----------------------------------------------------------------------------------------------------------
def test_backoff_delays_are_5_10_20_then_capped():
    assert [defer_delay(n, jitter=1.0) for n in range(3)] == [5.0, 10.0, 20.0]
    assert defer_delay(3, jitter=1.0) == 40.0 and defer_delay(4, jitter=1.0) == P.DEFER_MAX_SECONDS
    assert defer_delay(30, jitter=1.0) == P.DEFER_MAX_SECONDS
    low, high = P.DEFER_JITTER
    assert all(5.0 * low <= defer_delay(0) <= 5.0 * high for _ in range(50))


async def test_held_request_is_deferred_with_growing_persistent_not_before(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    task = await h.new_task()
    other = LeaseService(db, "other-runner", ttl_seconds=600)
    assert (await other.acquire(task.id)).status is AcquireStatus.ACQUIRED         # a live lease elsewhere
    request = await request_for(h, task.id)

    delays = []
    for expected_count in (1, 2, 3):
        claimed = await claim(h) if expected_count == 1 else await _force_claim(h, request.id)
        assert claimed.claim_token is not None
        assert await h.runner.process(claimed) == "HELD"
        row = await load(h, request.id)
        assert row.status == "PENDING" and row.defer_count == expected_count
        assert row.claim_token is None and row.runner_id is None and row.claimed_at is None and row.task_run_id is None
        async with db.transaction() as s:
            seconds = (await s.execute(text("select extract(epoch from (not_before - now())) from execution_requests "
                                            "where id = :i"), {"i": request.id})).scalar()
        delays.append(seconds)
        assert await claim(h) is None                                                # deferred: skipped, not claimed
    for got, base in zip(delays, (5, 10, 20)):
        assert base * 0.75 <= got <= base * 1.25, delays

    # "restart": a brand-new Runner/service stack sees the same persisted backoff, and crash recovery keeps it
    h2 = Harness(db, tmp_path / "ws")
    assert await claim(h2) is None
    row = await load(h, request.id)
    assert row.defer_count == 3 and row.not_before is not None


async def _force_claim(h: Harness, request_id) -> ExecutionRequest:
    """claim_next skips deferred requests; tests that re-process one make it eligible without touching defer_count."""
    async with h.db.transaction() as s:
        await s.execute(update(ExecutionRequest).where(ExecutionRequest.id == request_id).values(not_before=None))
    return await claim(h)


async def test_recover_requeue_after_crash_does_not_touch_the_defer_counter(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    task = await h.new_task()
    request = await request_for(h, task.id)
    async with db.transaction() as s:
        await s.execute(update(ExecutionRequest).where(ExecutionRequest.id == request.id).values(defer_count=2))
    claimed = await claim(h)
    await age_claim(h, claimed.id, 10_000)
    assert (await recover_for_test(h.runner))["requeued"] == 1
    row = await load(h, request.id)
    assert row.status == "PENDING" and row.defer_count == 2 and row.not_before is None and row.claim_token is None


# --- claim fencing / stale rules ------------------------------------------------------------------------------------
async def test_claim_sets_a_fresh_token_each_time(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    await request_for(h, (await h.new_task()).id)
    first = await claim(h)
    await age_claim(h, first.id, 10_000)
    await recover_for_test(h.runner)
    second = await claim(h)
    assert first.id == second.id and first.claim_token != second.claim_token


async def test_stale_owner_is_fenced_after_a_requeue(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    task = await h.new_task()
    request = await request_for(h, task.id)
    old = await claim(h)                                          # coroutine A claims ...
    await age_claim(h, old.id, 10_000)                            # ... and stalls past the grace period
    assert (await recover_for_test(h.runner))["requeued"] == 1            # the request is requeued
    new = await claim(h)                                          # coroutine B owns it now
    assert new.claim_token != old.claim_token

    assert await h.runner.process(old) == "FENCED"                # A wakes up: fenced at the very start
    assert h.claude.calls == [] and await h.runs(task.id) == []   # no lease, no CLI
    async with db.transaction() as s:                             # A's late finish/requeue are no-ops
        repo = ExecutionRequestRepository(s)
        assert await repo.finish(request.id, "DONE", "late", token=old.claim_token) is False
        assert await repo.requeue(request.id, "late", token=old.claim_token) is False
    assert (await load(h, request.id)).status == "CLAIMED"        # B's claim is untouched
    assert await h.runner.process(new) == "COMPLETED"


async def test_lease_acquire_itself_is_fenced(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    task = await h.new_task()
    await request_for(h, task.id)
    old = await claim(h)
    await age_claim(h, old.id, 10_000)
    await recover_for_test(h.runner)
    result = await h.leases.acquire(task.id, fence=(old.id, old.claim_token))
    assert result.status is AcquireStatus.FENCED and await h.runs(task.id) == []


async def test_claim_between_claim_and_lease_is_never_requeued_inside_the_grace_period(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    task = await h.new_task()
    await request_for(h, task.id)
    claimed = await claim(h)                                      # claimed, lease not yet taken (the normal window)
    assert (await recover_for_test(h.runner))["requeued"] == 0
    assert (await load(h, claimed.id)).status == "CLAIMED"
    await age_claim(h, claimed.id, 299)                           # still inside the 300 s grace
    assert (await recover_for_test(h.runner))["requeued"] == 0


async def test_a_live_lease_protects_an_old_claim_but_an_expired_one_does_not(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    task = await h.new_task()
    await request_for(h, task.id)
    claimed = await claim(h)
    lease = (await h.leases.acquire(task.id, fence=(claimed.id, claimed.claim_token))).lease
    await age_claim(h, claimed.id, 10_000)
    assert (await recover_for_test(h.runner))["requeued"] == 0            # a long CLI run with a live lease is not stale
    async with db.transaction() as s:
        await s.execute(text("update task_runs set lease_expires_at = now() - interval '1 minute' where id = :r"),
                        {"r": lease.run_id})
    assert (await recover_for_test(h.runner))["requeued"] == 1            # lease expired: now it is


async def test_crash_after_claim_before_lease_is_recovered_after_the_grace_period(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    task = await h.new_task()
    await request_for(h, task.id)
    await claim(h)                                                # runner dies here: CLAIMED, no lease
    h2 = Harness(db, tmp_path / "ws")                             # a new process starts: startup recovery
    await h2.runner.start()                                       # startup recovery runs inside start()
    try:
        async def completed():
            return (await h2.task(task.id)).status == "COMPLETED"
        assert await wait_for(completed, timeout=60)              # the dead runner's CLAIMED request was requeued
    finally:
        await h2.runner.stop()
    assert h2.runner.lifecycle.value == "STOPPED"


# --- cap SQL -----------------------------------------------------------------------------------------------------------
async def test_claim_skips_busy_projects_but_never_legacy_tasks(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    busy = await make_project(h, tmp_path, "busy")
    free = await make_project(h, tmp_path, "free")
    t_busy = await project_task(h, busy)
    t_free = await project_task(h, free)
    t_legacy = await h.new_task()
    r_busy, r_free, r_legacy = [await request_for(h, t.id) for t in (t_busy, t_free, t_legacy)]

    first = await claim(h, busy_project_ids=[busy.id])            # oldest request belongs to the busy project
    assert first.id == r_free.id
    second = await claim(h, busy_project_ids=[busy.id])           # legacy (project_id NULL) is NOT excluded
    assert second.id == r_legacy.id
    assert await claim(h, busy_project_ids=[busy.id]) is None
    assert (await load(h, r_busy.id)).status == "PENDING"         # skipped, not requeued/changed
    assert (await claim(h, busy_project_ids=[])).id == r_busy.id  # an empty list adds no condition (and no SQL error)


async def test_claim_with_empty_and_default_busy_list(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    request = await request_for(h, (await h.new_task()).id)
    assert (await claim(h, busy_project_ids=())).id == request.id


# --- bounded dispatch -----------------------------------------------------------------------------------------------
async def test_different_projects_run_in_parallel_up_to_the_task_limit(db, tmp_path):
    gate = Gate()
    h = Harness(db, tmp_path / "ws", claude_handlers={"PLANNER": gate.planner})
    projects = [await make_project(h, tmp_path, n) for n in ("a", "b", "c")]
    tasks = [await project_task(h, p) for p in projects]
    runner = make_runner(h, tasks=2, per_project=1)
    await runner.start()
    try:
        for t in tasks:
            await h.tasks.request_execution(t.id)
        runner.notify()
        assert await wait_for(lambda: _true(gate.active == 2))
        await asyncio.sleep(0.4)
        assert gate.peak == 2 and runner.in_flight == 2                       # the third project waits for a slot
        assert [s for s in await statuses(h, *[t.id for t in tasks])].count("PENDING") == 1
        gate.release.set()
        assert await wait_for(lambda: _all_completed(h, tasks), timeout=60)
        assert runner.in_flight == 0
    finally:
        await runner.stop()
    assert len({str(c) for c in gate.started}) == 3                           # three distinct project workspaces


async def _true(value):
    return value


async def _all_completed(h, tasks):
    return all(s == "COMPLETED" for s in await statuses(h, *[t.id for t in tasks]))


async def test_same_project_tasks_never_run_at_the_same_time(db, tmp_path):
    gate = Gate()
    h = Harness(db, tmp_path / "ws", claude_handlers={"PLANNER": gate.planner})
    project = await make_project(h, tmp_path, "solo")
    tasks = [await project_task(h, project, f"change number {i}") for i in range(2)]
    runner = make_runner(h, tasks=3, per_project=1)                           # plenty of slots: the project cap decides
    await runner.start()
    try:
        for t in tasks:
            await h.tasks.request_execution(t.id)
        runner.notify()
        assert await wait_for(lambda: _true(gate.active == 1))
        await asyncio.sleep(0.4)
        assert gate.peak == 1 and runner.in_flight == 1
        async with db.transaction() as s:                                     # the second request was skipped, not claimed
            rows = (await s.execute(select(ExecutionRequest.status))).scalars().all()
        assert sorted(rows) == ["CLAIMED", "PENDING"]
        gate.release.set()
        assert await wait_for(lambda: _all_completed(h, tasks), timeout=60)   # no notify(): the done callback woke the loop
        assert gate.peak == 1
    finally:
        await runner.stop()


async def test_legacy_tasks_are_not_blocked_by_a_busy_project_or_each_other(db, tmp_path):
    gate = Gate()
    h = Harness(db, tmp_path / "ws", claude_handlers={"PLANNER": gate.planner})
    project = await make_project(h, tmp_path, "busy")
    p1, p2 = await project_task(h, project), await project_task(h, project, "second change here")
    l1, l2 = await h.new_task("legacy one task"), await h.new_task("legacy two task")
    runner = make_runner(h, tasks=3, per_project=1)
    await runner.start()
    try:
        for t in (p1, p2, l1, l2):
            await h.tasks.request_execution(t.id)
        runner.notify()
        assert await wait_for(lambda: _true(gate.active == 3))                # p1 + l1 + l2; p2 waits on its project cap
        await asyncio.sleep(0.3)
        assert gate.peak == 3 and (await h.task(p2.id)).status == "PENDING"
        gate.release.set()
        assert await wait_for(lambda: _all_completed(h, [p1, p2, l1, l2]), timeout=90)
    finally:
        await runner.stop()


async def test_one_failing_request_does_not_stop_the_others_and_frees_its_slot(db, tmp_path):
    bad_dir_marker = "explode"

    async def planner(call):
        if bad_dir_marker in str(call.cwd):
            raise RuntimeError("boom")                                        # an unexpected error inside a child task
        return structured(**PLAN)

    h = Harness(db, tmp_path / "ws", claude_handlers={"PLANNER": planner})
    bad = await make_project(h, tmp_path, bad_dir_marker)
    good = await make_project(h, tmp_path, "fine")
    t_bad, t_good = await project_task(h, bad), await project_task(h, good)
    runner = make_runner(h, tasks=1, per_project=1)                           # ONE slot: the failure must release it
    await runner.start()
    try:
        await h.tasks.request_execution(t_bad.id)
        runner.notify()
        assert await wait_for(lambda: _true(runner.in_flight == 0 and len(h.claude.calls) >= 1))
        await h.tasks.request_execution(t_good.id)
        runner.notify()
        assert await wait_for(lambda: _all_completed(h, [t_good]), timeout=60)
    finally:
        await runner.stop()


# --- shutdown ------------------------------------------------------------------------------------------------------------
async def test_stop_drains_a_task_that_finishes_within_the_grace_period(db, tmp_path):
    release = asyncio.Event()

    async def planner(call):
        await release.wait()
        return structured(**PLAN)

    h = Harness(db, tmp_path / "ws", claude_handlers={"PLANNER": planner})
    task = await h.new_task()
    runner = make_runner(h, grace=20)
    await runner.start()
    await h.tasks.request_execution(task.id)
    runner.notify()
    assert await wait_for(lambda: _true(len(h.claude.calls) == 1))
    stopper = asyncio.create_task(runner.stop())
    await asyncio.sleep(0.2)
    assert not stopper.done()                                                 # still draining: nothing is cut off
    release.set()
    await asyncio.wait_for(stopper, 60)
    assert (await h.task(task.id)).status == "COMPLETED" and runner.in_flight == 0


async def test_stop_cancels_a_stuck_task_revokes_its_lease_and_releases_the_runner_lock(db, tmp_path):
    async def planner(call):
        await asyncio.sleep(3600)

    h = Harness(db, tmp_path / "ws", claude_handlers={"PLANNER": planner})
    task = await h.new_task()
    runner = make_runner(h, grace=0.3)
    await runner.start()
    await h.tasks.request_execution(task.id)
    runner.notify()
    assert await wait_for(lambda: _true(len(h.claude.calls) == 1))
    runs = await h.runs(task.id)
    started = time.monotonic()
    await asyncio.wait_for(runner.stop(), 30)
    assert time.monotonic() - started < 10
    assert runner.in_flight == 0
    assert any(str(runs[0].id) in key for key in h.claude.cancelled)          # its CLI processes were told to stop
    assert runs[0].id in h.leases._revoked                                   # nothing more can be recorded by this run
    # the advisory lock is free again and the unfinished claim is recovered by the next start
    second = make_runner(h)
    await second.start()
    await second.stop()
    assert (await h.task(task.id)).status != "COMPLETED"                      # never silently marked finished


async def test_stop_without_running_tasks_is_immediate(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    runner = make_runner(h)
    await runner.start()
    await asyncio.wait_for(runner.stop(), 5)


# --- CLI process slots ------------------------------------------------------------------------------------------------------
async def test_cli_process_limit_serialises_children_and_cancel_ends_the_wait(tmp_path):
    from app.infra.workspace_guard import WorkspaceGuard
    root = tmp_path / "ws"
    root.mkdir()
    executor = CommandExecutor(WorkspaceGuard(root), max_concurrent=1)
    slow = CommandSpec(argv=[sys.executable, "-c", "import time; time.sleep(1.5)"], cwd=root, idle_seconds=None)
    first = asyncio.create_task(executor.run(slow, run_key="run-1"))
    await asyncio.sleep(0.4)
    queued = asyncio.create_task(executor.run(slow, run_key="run-2"))
    await asyncio.sleep(0.3)
    assert not queued.done()                                                  # waiting for the single slot
    executor.cancel("run-2")
    result = await asyncio.wait_for(queued, 5)
    assert result.outcome is CliOutcome.CANCELLED and result.exit_code is None  # never spawned
    assert (await first).outcome is CliOutcome.SUCCESS
    again = await executor.run(CommandSpec(argv=[sys.executable, "-c", "pass"], cwd=root))
    assert again.outcome is CliOutcome.SUCCESS                                # the slot was released correctly


async def test_unbounded_executor_runs_children_concurrently(tmp_path):
    from app.infra.workspace_guard import WorkspaceGuard
    root = tmp_path / "ws"
    root.mkdir()
    executor = CommandExecutor(WorkspaceGuard(root))
    spec = CommandSpec(argv=[sys.executable, "-c", "import time; time.sleep(1.0)"], cwd=root, idle_seconds=None)
    started = time.monotonic()
    results = await asyncio.gather(executor.run(spec), executor.run(spec))
    assert all(r.outcome is CliOutcome.SUCCESS for r in results) and time.monotonic() - started < 1.9
