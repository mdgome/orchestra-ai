"""Design 7.5 item 10, acceptance rows 12 and 13 and the approval-reuse cases, on the embedded PostgreSQL: the INPUT (request status,
project epoch, target identities), the allowed state transition of the request, the audit record (`result.supersede`) and whether a
RESUME is created. The handler never creates a RESUME; only the worker's result transaction does, insert-if-absent per approval."""
import uuid

import pytest
from sqlalchemy import select, text, update

from app.models.tables import ExecutionRequest, Project
from app.repositories.approval_repository import ExecutionRequestRepository
from app.repositories.task_repository import TaskRepository
from app.services.process_risk_worker import ProcessRiskWorker, Target
from app.services.tracked_classifier import RowClass, classify_row
from tests.integration_pg.test_orphan_sweeper import add_row
from tests.integration_pg.test_process_risk_worker import UNCLEAR_V, Notes, fixed, request_of, resumes, setup, worker
from tests.integration_pg.test_process_schema import make_project_task
from tests.integration_pg.test_resume_outcome import Rig, pending, scene
from tests.integration_pg.test_risk_options import decide
from tests.integration_pg.test_risk_options import scene as gate_scene


async def all_resumes(db, task_id):
    async with db.transaction() as s:
        return list((await s.execute(select(ExecutionRequest.id, ExecutionRequest.approval_id, ExecutionRequest.status)
                                     .where(ExecutionRequest.task_id == task_id, ExecutionRequest.kind == "RESUME"))).all())


async def bump_epoch(db, project_id, epoch=7):
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project_id).values(write_epoch=epoch))


# --- row 12: the epoch changed -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,reason,superseded", [("APPLIED", None, True), ("REFUSED_ALIVE", "LIVE_PROCESS_PRESENT", False),
                                                     ("NEEDS_REDECISION", "NO_LIVE_PROCESS_UNPROVEN", False)])
async def test_row_12_never_creates_a_resume_only_an_applied_request_is_superseded(db, tmp_path, status, reason, superseded):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path, status=status, reason=reason)
    await bump_epoch(db, ctx.project.id)
    before = await all_resumes(db, ctx.task.id)
    assert [r.approval_id for r in before] == [ctx.approval_id]                       # only the RESUME being processed
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code, outcome.parked) == ("PARKED", "DIRTY_EPOCH_CHANGED", True)
    assert await all_resumes(db, ctx.task.id) == before                               # no RESUME was created, none was changed
    req = await request_of(db, ctx.approval_id)
    if superseded:
        assert req.status == "NEEDS_REDECISION" and req.reason == "DIRTY_EPOCH_CHANGED"
        assert req.result["supersede"]["from_status"] == "APPLIED" and req.result["supersede"]["current_dirty_epoch"] == 7
    else:
        assert (req.status, req.reason) == (status, reason) and "supersede" not in (req.result or {})
    gates = await pending(db, ctx.task.id)
    assert len(gates) == 1 and gates[0].target["dirty_epoch"] == 7 and gates[0].id == outcome.gate_id


async def test_row_12_a_replay_parks_on_the_existing_gate_and_supersedes_nothing_twice(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path, status="APPLIED")
    await bump_epoch(db, ctx.project.id)
    first, _, _ = await rig.resume(ctx)
    superseded = (await request_of(db, ctx.approval_id)).result["supersede"]
    again, _, _ = await rig.resume(ctx)                                               # the RESUME is claimed again after a crash
    assert (again.code, again.gate_id) == ("GATE_ALREADY_PENDING", first.gate_id)
    assert (await request_of(db, ctx.approval_id)).result["supersede"] == superseded
    assert len(await pending(db, ctx.task.id)) == 1 and len(await all_resumes(db, ctx.task.id)) == 1


# --- row 13: a target identity is gone or changed ----------------------------------------------------------------------------------------
async def other_project_row(db):
    _, other_task = await make_project_task(db)
    return await add_row(db, other_task, pid=9001, finished=True)


@pytest.mark.parametrize("case", ["row_gone", "row_of_another_project"])
@pytest.mark.parametrize("status,reason,superseded", [("APPLIED", None, True), ("REFUSED_ALIVE", "LIVE_PROCESS_PRESENT", False),
                                                     ("NEEDS_REDECISION", "NO_LIVE_PROCESS_UNPROVEN", False)])
async def test_row_13_a_target_identity_gone_creates_no_resume_and_supersedes_only_an_applied_request(
        db, tmp_path, case, status, reason, superseded):
    rig = Rig(db, tmp_path)
    target = uuid.uuid4() if case == "row_gone" else await other_project_row(db)
    ctx = await scene(db, tmp_path, status=status, reason=reason, targets=[target])
    before = await all_resumes(db, ctx.task.id)
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code, outcome.parked) == ("PARKED", "EXECUTION_IDENTITY_CHANGED", True)
    assert await all_resumes(db, ctx.task.id) == before
    req = await request_of(db, ctx.approval_id)
    if superseded:
        assert req.status == "NEEDS_REDECISION" and req.result["supersede"]["reason_code"] == "EXECUTION_IDENTITY_CHANGED"
    else:
        assert (req.status, req.reason) == (status, reason)
    assert len(await pending(db, ctx.task.id)) == 1


async def test_row_12_wins_over_row_13_when_both_the_epoch_and_an_identity_changed(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path, status="APPLIED", targets=[uuid.uuid4()])
    await bump_epoch(db, ctx.project.id)
    outcome, _, _ = await rig.resume(ctx)
    assert outcome.code == "DIRTY_EPOCH_CHANGED"


async def accepted_scene(db, tmp_path):
    """An APPLIED ACCEPT request whose target carries a real `OPERATOR_RISK_ACCEPTED` record (what the worker wrote)."""
    ctx = await scene(db, tmp_path, status="APPLIED")
    request = await request_of(db, ctx.approval_id)
    async with db.transaction() as s:
        repo = TaskRepository(s)
        for exec_id in request.target_execution_ids:
            row = await repo.tracked_row(exec_id)
            assert await repo.write_disposition(row, ProcessRiskWorker._record(request, row, Target(row, reverify="UNKNOWN")))
        rows = [await repo.tracked_row(i) for i in request.target_execution_ids]
    assert all(classify_row(r.id, r.pid, r.finished_at, r.detail_text).row_class is RowClass.RESOLVED_RISK for r in rows)
    return ctx, request


async def test_row_13_a_resolved_risk_row_that_still_matches_its_record_proceeds_at_row_14(db, tmp_path):
    rig = Rig(db, tmp_path)
    ctx, _ = await accepted_scene(db, tmp_path)
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code) == ("RESUME", None)
    assert (await request_of(db, ctx.approval_id)).status == "APPLIED"


@pytest.mark.parametrize("column,value", [("pid", 5151), ("process_create_time", 99.5)])
async def test_row_13_a_changed_pid_or_create_time_against_the_acceptance_record_is_an_identity_change(db, tmp_path, column, value):
    rig = Rig(db, tmp_path)
    ctx, request = await accepted_scene(db, tmp_path)
    async with db.transaction() as s:
        await s.execute(text(f"UPDATE executions SET {column} = :v WHERE id = :i"), {"v": value, "i": request.target_execution_ids[0]})
    before = await all_resumes(db, ctx.task.id)
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code) == ("PARKED", "EXECUTION_IDENTITY_CHANGED")
    assert await all_resumes(db, ctx.task.id) == before
    assert (await request_of(db, ctx.approval_id)).status == "NEEDS_REDECISION"


# --- approval reuse and the duplicate RESUME ---------------------------------------------------------------------------------------------
async def test_an_approval_used_after_applied_and_replayed_creates_no_second_resume(db):
    ctx = await setup(db)
    notes = Notes()
    await worker(db, notes, tracked_verdict=fixed(UNCLEAR_V))()                       # APPLIED: the worker inserts the one RESUME
    assert (await request_of(db, ctx.approval_id)).status == "APPLIED"
    assert (await resumes(db, ctx.approval_id), notes.count) == (1, 1)
    await worker(db, notes, tracked_verdict=fixed(UNCLEAR_V))()                       # the next pass: nothing is claimable any more
    assert (await resumes(db, ctx.approval_id), notes.count) == (1, 1)
    async with db.transaction() as s:                                                 # the same approval presented again
        assert await ExecutionRequestRepository(s).add_resume_if_absent(ctx.task.id, ctx.approval_id) is False
    async with db.transaction() as s:                                                 # a DONE RESUME is not re-created either
        await s.execute(text("UPDATE execution_requests SET status = 'DONE' WHERE approval_id = :a"), {"a": ctx.approval_id})
    async with db.transaction() as s:
        assert await ExecutionRequestRepository(s).add_resume_if_absent(ctx.task.id, ctx.approval_id) is False
    assert await resumes(db, ctx.approval_id) == 1
    async with db.transaction() as s:
        status = (await s.execute(text("select status from execution_requests where approval_id = :a"), {"a": ctx.approval_id})).scalar_one()
    assert status == "DONE"


async def test_a_decision_replay_after_applied_is_idempotent_and_creates_neither_a_request_nor_a_resume(db):
    project, task, gates, approval = await gate_scene(db)
    first = await decide(gates, approval, "accept_unproven_risk", requested_by="op")
    assert first.created
    await worker(db, Notes(), tracked_verdict=fixed(UNCLEAR_V))()
    assert (await request_of(db, approval)).status == "APPLIED" and await resumes(db, approval) == 1
    again = await decide(gates, approval, "accept_unproven_risk", requested_by="op")
    assert again.created is False
    assert (await request_of(db, approval)).status == "APPLIED" and await resumes(db, approval) == 1
    async with db.transaction() as s:
        n = (await s.execute(text("select count(*) from process_risk_requests where approval_id = :a"), {"a": approval})).scalar_one()
    assert n == 1
