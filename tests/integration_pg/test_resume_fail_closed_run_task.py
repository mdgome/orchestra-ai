"""Design 7.5 item 10, the FAIL_CLOSED rows on the REAL workflow path: `Runner.process` -> `WorkflowService.run_task` ->
`_resume_from_approval` -> `FailClosed` -> `_fail`. A real Task, a real RESUME request, real rows. What is asserted: the Task ends
FAILED with the reason `FAIL_CLOSED`, the lease is released, nothing is written where the row says nothing is written, and no
follow-up execution (a step, a CLI, a new RESUME, a second claim) can start."""
import uuid

import pytest
from sqlalchemy import select, text, update

from app.models.tables import Approval, Execution, ExecutionRequest, Task, TaskStep
from app.services.runner import Runner
from tests.harness import Harness
from tests.integration_pg.test_process_risk_worker import request_of
from tests.integration_pg.test_resume_outcome import pending, scene
from tests.integration_pg.test_runner_background import KEY


async def runner_for(db, tmp_path):
    (tmp_path.resolve() / "ws").mkdir(exist_ok=True)
    h = Harness(db, tmp_path.resolve() / "ws")
    r = Runner(db, h.leases, h.workflow, h.gates, [h.claude, h.codex], advisory_key=KEY, poll_seconds=3600.0,
               claim_timeout_seconds=300, shutdown_grace_seconds=0.1, capability_enabled=False)
    return h, r


async def claimed(db, ctx) -> ExecutionRequest:
    async with db.transaction() as s:
        return (await s.execute(select(ExecutionRequest).where(ExecutionRequest.id == ctx.resume_id))).scalar_one()


async def snapshot(db, task_id):
    async with db.transaction() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        steps = (await s.execute(select(TaskStep).where(TaskStep.task_id == task_id))).scalars().all()
        execs = (await s.execute(select(Execution).where(Execution.task_id == task_id))).scalars().all()
        reqs = (await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task_id))).scalars().all()
        running = (await s.execute(text("select count(*) from task_runs where task_id = :t and status = 'RUNNING'"),
                                   {"t": task_id})).scalar_one()
        approvals = (await s.execute(select(Approval).where(Approval.task_id == task_id))).scalars().all()
    return task, steps, execs, reqs, running, approvals


async def prepare(db, tmp_path, **kw):
    ctx = await scene(db, tmp_path, **kw)
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == ctx.task.id).values(workspace_path=str(tmp_path.resolve() / "ws")))
    return ctx


async def assert_failed_closed_and_blocked(db, h, r, ctx, label, execs_before=1):
    task, steps, execs, reqs, running, approvals = await snapshot(db, ctx.task.id)
    assert task.status == "FAILED" and task.failure_reason == "FAIL_CLOSED"                 # the Task failure state and reason
    assert label == "FAILED" and running == 0                                               # the run ended, the lease was released
    assert steps == [] and len(execs) == execs_before                                       # no step, no CLI, no new execution row
    assert len(approvals) == 1 and not await pending(db, ctx.task.id)                       # no gate was opened
    assert [q.status for q in reqs] == ["DONE"]                                             # the request ended; no second one exists
    # follow-up: a second run of the same Task and a second claim start nothing
    again = (await h.leases.acquire(ctx.task.id)).lease
    assert await h.workflow.run_task(again, ctx.task.id) == "FAILED"                        # terminal: no handler runs
    async with db.transaction() as s:
        from app.repositories.approval_repository import ExecutionRequestRepository
        assert await ExecutionRequestRepository(s).claim_next("runner-x") is None           # a DONE RESUME is never claimed again
    task2, steps2, execs2, reqs2, _, _ = await snapshot(db, ctx.task.id)
    assert (task2.status, steps2, len(execs2), len(reqs2)) == ("FAILED", [], execs_before, 1)


async def test_row_4_a_resume_that_is_not_this_approvals_fails_the_task_and_writes_nothing(db, tmp_path):
    h, r = await runner_for(db, tmp_path)
    ctx = await prepare(db, tmp_path)
    async with db.transaction() as s:                                                       # the claimed request is not a RESUME
        await s.execute(update(ExecutionRequest).where(ExecutionRequest.id == ctx.resume_id).values(kind="START"))
    request = await claimed(db, ctx)
    label = await r.process(request)
    before = await request_of(db, ctx.approval_id)
    assert before.status == "APPLIED"                                                       # row 4 writes no request status
    await assert_failed_closed_and_blocked(db, h, r, ctx, label)


async def test_row_7_a_risk_decision_without_a_matching_request_fails_the_task_and_writes_nothing(db, tmp_path):
    h, r = await runner_for(db, tmp_path)
    ctx = await prepare(db, tmp_path)
    async with db.transaction() as s:
        await s.execute(text("UPDATE process_risk_requests SET kind = 'RECHECK' WHERE approval_id = :a"), {"a": ctx.approval_id})
    label = await r.process(await claimed(db, ctx))
    req = await request_of(db, ctx.approval_id)
    assert (req.kind, req.status) == ("RECHECK", "APPLIED")                                 # untouched
    await assert_failed_closed_and_blocked(db, h, r, ctx, label)


async def test_row_1_a_foreign_approval_fails_the_task_and_writes_nothing(db, tmp_path):
    h, r = await runner_for(db, tmp_path)
    ctx = await prepare(db, tmp_path)
    other = await scene(db, tmp_path)
    async with db.transaction() as s:
        await s.execute(text("UPDATE approvals SET task_id = :t WHERE id = :a"), {"t": other.task.id, "a": ctx.approval_id})
    label = await r.process(await claimed(db, ctx))
    assert (await request_of(db, ctx.approval_id)).status == "APPLIED"
    task, *_ = await snapshot(db, ctx.task.id)
    assert task.status == "FAILED" and task.failure_reason == "FAIL_CLOSED" and label == "FAILED"
    again = (await h.leases.acquire(ctx.task.id)).lease
    assert await h.workflow.run_task(again, ctx.task.id) == "FAILED"                        # nothing follows
    _, steps, execs, reqs, running, _ = await snapshot(db, ctx.task.id)
    assert steps == [] and len(execs) == 1 and [q.status for q in reqs] == ["DONE"]


async def test_row_6_a_project_mismatch_while_waiting_redecides_the_request_then_fails_the_task_and_blocks_follow_up(db, tmp_path):
    h, r = await runner_for(db, tmp_path)
    ctx = await prepare(db, tmp_path, status="REQUESTED")
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == ctx.task.id).values(project_id=None))
    label = await r.process(await claimed(db, ctx))
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.reason) == ("NEEDS_REDECISION", "TASK_PROJECT_MISMATCH")        # written where the row says so
    await assert_failed_closed_and_blocked(db, h, r, ctx, label)


async def test_row_6_when_the_task_already_moved_on_is_audit_only_and_never_a_task_failure(db, tmp_path):
    h, r = await runner_for(db, tmp_path)
    ctx = await prepare(db, tmp_path, status="APPLIED")
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == ctx.task.id).values(project_id=None, status="FAILED", failure_reason="X"))
    label = await r.process(await claimed(db, ctx))
    req = await request_of(db, ctx.approval_id)
    assert req.status == "NEEDS_REDECISION" and req.result["supersede"]["from_status"] == "APPLIED"
    task, steps, execs, reqs, running, _ = await snapshot(db, ctx.task.id)
    assert (task.status, task.failure_reason, steps, len(execs)) == ("FAILED", "X", [], 1)      # the old reason is not overwritten
    assert [q.status for q in reqs] == ["DONE"]
