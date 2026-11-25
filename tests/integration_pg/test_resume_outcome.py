"""B2 phase 5c on the embedded PostgreSQL: the handling table of design 7.5 item 10 (`_resume_from_approval` rows 1 to 16 in the common
tier order, step L), the ordinary-path lineage (C-b), the unique-index race (C-7) and the guard codes."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.models.tables import Approval, ExecutionRequest, Project, Task
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.process_risk_repository import ProcessRiskRequestRepository
from app.services.approval_lineage import GateLineage, is_lineage_violation
from app.services.lease_service import LeaseService
from app.services.project_lock_service import ProjectLockService, StepGate
from app.services.project_terminal import (
    ProjectTrackedExecutionAnomaly, ProjectUnresolvedTrackedExecution, assert_no_unresolved_or_anomalous_tracked)
from app.services.workflow_service import RunContext
from app.validation import policies as P
from app.validation.exceptions import FailClosed
from tests.harness import Harness
from tests.integration_pg.test_orphan_sweeper import add_row
from tests.integration_pg.test_process_risk_worker import request_of, setup
from tests.integration_pg.test_process_schema import make_project_task
from tests.integration_pg.test_risk_options import gates_for


async def scene(db, tmp_path, *, kind="ACCEPT", status="APPLIED", reason=None, epoch=1, rows=1, option=None, resume="CLAIMED",
                targets=None, open_rows=False):
    """A WAITING_APPROVAL task on a dirty project, a DECIDED approval, its risk request in `status`, a RESUME in `resume` and a lease."""
    project, task = await make_project_task(db)
    ids = [await add_row(db, task, pid=4242 + i, finished=not open_rows) for i in range(rows)]
    ctx = await setup(db, kind=kind, epoch=epoch, pt=(project, task), rows=ids, option=option, targets=targets)
    async with db.transaction() as s:
        await s.execute(text("UPDATE approvals SET target = target || CAST(:t AS jsonb) WHERE id = :a"),
                        {"t": '{"project_id": "%s", "dirty_epoch": %d}' % (project.id, epoch), "a": ctx.approval_id})
        await s.execute(text("UPDATE process_risk_requests SET status = :st, reason = :rs, finished_at = CASE WHEN :st IN "
                             "('REQUESTED','VERIFYING','RETRYABLE') THEN NULL ELSE now() END WHERE approval_id = :a"),
                        {"st": status, "rs": reason, "a": ctx.approval_id})
        if resume:
            s.add(ExecutionRequest(id=(rid := uuid.uuid4()), task_id=task.id, kind="RESUME", status=resume,
                                   approval_id=ctx.approval_id, claim_token=uuid.uuid4()))
            ctx.resume_id = rid
    return ctx


class Rig:
    def __init__(self, db, tmp_path):
        self.h = Harness(db, tmp_path / "ws")
        self.db, self.root = db, tmp_path

    async def resume(self, ctx, *, request_id="auto", lease=None):
        lease = lease or (await self.h.leases.acquire(ctx.task.id)).lease
        rc = RunContext(lease, self.root, request_id=getattr(ctx, "resume_id", None) if request_id == "auto" else request_id)
        outcome = await self.h.workflow._resume_from_approval(rc, ctx.task.id, ctx.approval_id)
        return outcome, rc, lease


async def pending(db, task_id):
    async with db.transaction() as s:
        return await ApprovalRepository(s).pending_for_task(task_id)


async def task_status(db, task_id):
    async with db.transaction() as s:
        return (await s.execute(select(Task.status).where(Task.id == task_id))).scalar_one()


async def lease_held(db, task_id) -> bool:
    async with db.transaction() as s:
        return (await s.execute(text("select count(*) from task_runs where task_id = :t and status = 'RUNNING'"), {"t": task_id})).scalar_one() > 0


# --- rows 1, 6, 2, 3, 5 ------------------------------------------------------------------------------------------------------------
async def test_row_1_a_missing_or_foreign_approval_is_raised_and_nothing_is_written(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path)
    other = await scene(db, tmp_path)
    async with db.transaction() as s:
        await s.execute(text("UPDATE approvals SET task_id = :t WHERE id = :a"), {"t": other.task.id, "a": ctx.approval_id})
    with pytest.raises(FailClosed):
        await rig.resume(ctx)
    assert (await request_of(db, ctx.approval_id)).status == "APPLIED"


async def test_row_6_a_project_mismatch_redecides_a_live_request_supersedes_an_applied_one_and_fails_closed_only_while_waiting(db, tmp_path):
    rig = Rig(db, tmp_path)
    ctx = await scene(db, tmp_path, status="REQUESTED")
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == ctx.task.id).values(project_id=None))
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code, outcome.parked) == ("FAIL_CLOSED", "TASK_PROJECT_MISMATCH", False)
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.reason) == ("NEEDS_REDECISION", "TASK_PROJECT_MISMATCH")
    applied = await scene(db, tmp_path, status="APPLIED")
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == applied.task.id).values(project_id=None, status="FAILED"))
    outcome, _, _ = await rig.resume(applied)                               # the Task moved on: audit only
    assert (outcome.action, outcome.code) == ("IDEMPOTENT_DONE", "TASK_PROJECT_MISMATCH")
    req = await request_of(db, applied.approval_id)
    assert req.status == "NEEDS_REDECISION" and req.result["supersede"]["from_status"] == "APPLIED"
    assert set(req.result["supersede"]) == {"from_status", "reason_code", "superseded_at", "current_dirty_epoch",
                                            "current_identities", "approval_id"}


async def test_row_2_a_task_that_is_not_waiting_returns_idempotent_done_and_row_14_is_not_reached(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path)
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == ctx.task.id).values(status="DEVELOPING"))
    outcome, rc, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code, outcome.parked) == ("IDEMPOTENT_DONE", "TASK_NOT_WAITING_APPROVAL", False)
    assert await task_status(db, ctx.task.id) == "DEVELOPING"


async def test_row_3_opens_one_execution_uncertain_gate_and_a_replay_finds_it_instead_of_opening_a_second(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path, open_rows=True)       # an OPEN row of a step role: `_recovery_unsafe`
    outcome, rc, lease = await rig.resume(ctx)
    assert (outcome.action, outcome.parked) == ("PARKED", True)
    gates = await pending(db, ctx.task.id)
    assert len(gates) == 1 and gates[0].target["reason_code"] == P.RECOVERY_REASON_EXECUTION_UNCERTAIN and outcome.gate_id == gates[0].id
    assert not await lease_held(db, ctx.task.id)                                   # the lease was released in the same transaction
    again, _, _ = await rig.resume(ctx)                                            # the stale RESUME replays
    assert again.action == "PARKED" and again.gate_id == gates[0].id and len(await pending(db, ctx.task.id)) == 1


@pytest.mark.parametrize("kind", ["DESIGN", "PERMISSION", "RECOVERY"])
async def test_row_3_with_any_type_of_pending_approval_opens_no_gate_and_parks_on_the_existing_one(db, tmp_path, kind):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path, open_rows=True)
    target = {"reason_code": "PROJECT_DIRTY"} if kind == "RECOVERY" else {}
    async with db.transaction() as s:
        existing = Approval(id=uuid.uuid4(), task_id=ctx.task.id, type=kind, status="PENDING", target=target, scope="TASK",
                            request_payload={}, basis={}, checkpoint_step_key="PLAN", valid_until=datetime.now(UTC) + timedelta(days=1))
        s.add(existing)
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.parked, outcome.gate_id) == ("PARKED", True, existing.id)
    assert [a.id for a in await pending(db, ctx.task.id)] == [existing.id]
    assert not await lease_held(db, ctx.task.id)


async def test_row_5_a_pending_approval_of_any_type_parks_a_risk_resume_on_it(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path)
    async with db.transaction() as s:
        existing = Approval(id=uuid.uuid4(), task_id=ctx.task.id, type="DESIGN", status="PENDING", target={}, scope="TASK",
                            request_payload={}, basis={}, checkpoint_step_key="PLAN", valid_until=datetime.now(UTC) + timedelta(days=1))
        s.add(existing)
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code, outcome.gate_id, outcome.parked) == ("PARKED", "GATE_ALREADY_PENDING", existing.id, True)
    assert not await lease_held(db, ctx.task.id)


# --- rows 4, 7 ------------------------------------------------------------------------------------------------------------------------
async def test_row_4_the_resume_must_be_this_approvals_and_claimed(db, tmp_path):
    rig = Rig(db, tmp_path)
    ctx = await scene(db, tmp_path)
    with pytest.raises(FailClosed) as info:
        await rig.resume(ctx, request_id=None)
    assert info.value.details["code"] == "RESUME_REQUEST_MISMATCH"
    done = await scene(db, tmp_path, resume="DONE")
    with pytest.raises(FailClosed) as info:
        await rig.resume(done)
    assert info.value.details["code"] == "RESUME_REQUEST_MISMATCH"


async def test_row_7_a_risk_decision_without_a_matching_request_is_raised(db, tmp_path):
    rig = Rig(db, tmp_path)
    ctx = await scene(db, tmp_path)
    async with db.transaction() as s:
        await s.execute(text("UPDATE process_risk_requests SET kind = 'RECHECK' WHERE approval_id = :a"), {"a": ctx.approval_id})
    with pytest.raises(FailClosed) as info:
        await rig.resume(ctx)
    assert info.value.details["code"] == "RISK_REQUEST_NOT_APPLICABLE"
    assert (await request_of(db, ctx.approval_id)).kind == "RECHECK"                # nothing was written


# --- rows 8 to 13: a new gate, lineage, lease release, request status ------------------------------------------------------------------
async def gate_after(db, task_id, outcome):
    gates = await pending(db, task_id)
    assert len(gates) == 1 and outcome.gate_id == gates[0].id and outcome.parked
    return gates[0]


async def test_row_8_a_request_that_never_applied_is_redecided_fenced_and_a_new_gate_opens(db, tmp_path):
    for status in ("REQUESTED", "VERIFYING", "RETRYABLE"):
        rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path, status=status)
        outcome, _, _ = await rig.resume(ctx)
        assert (outcome.action, outcome.code, outcome.request_status) == ("PARKED", "RISK_REQUEST_NOT_APPLIED", status)
        req = await request_of(db, ctx.approval_id)
        assert (req.status, req.reason) == ("NEEDS_REDECISION", "RISK_REQUEST_NOT_APPLIED") and req.finished_at is not None
        gate = await gate_after(db, ctx.task.id, outcome)
        assert gate.target["reason_code"] == "PROJECT_DIRTY" and gate.target["supersedes_approval_id"] == str(ctx.approval_id)
        assert gate.target["lineage_origin"] == "HANDLER" and gate.target["lineage_root_id"] == str(ctx.approval_id)
        assert gate.target["lineage_dirty_epoch"] == gate.target["dirty_epoch"] == 1
        assert not await lease_held(db, ctx.task.id)


async def test_rows_9_10_11_open_a_new_gate_at_the_current_epoch_and_leave_the_terminal_request_unchanged(db, tmp_path):
    cases = [("REFUSED_ALIVE", "LIVE_PROCESS_PRESENT", "LIVE_PROCESS_PRESENT"),
             ("NEEDS_REDECISION", "NO_LIVE_PROCESS_UNPROVEN", "NO_LIVE_PROCESS_UNPROVEN"),
             ("NEEDS_REDECISION", "PROCESS_RECHECK_INCONCLUSIVE", "PROCESS_RECHECK_INCONCLUSIVE")]
    for status, reason, code in cases:
        rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path, status=status, reason=reason)
        outcome, _, _ = await rig.resume(ctx)
        assert (outcome.action, outcome.code) == ("PARKED", code)
        req = await request_of(db, ctx.approval_id)
        assert (req.status, req.reason) == (status, reason)
        gate = await gate_after(db, ctx.task.id, outcome)
        shown = "OPERATOR_OVERRIDE_REFUSED_LIVE_PROCESS" if code == "LIVE_PROCESS_PRESENT" else code
        assert gate.target["risk_request_code"] == shown


async def test_row_12_a_changed_epoch_supersedes_an_applied_request_and_beats_the_live_process_row(db, tmp_path):
    rig = Rig(db, tmp_path)
    ctx = await scene(db, tmp_path, status="APPLIED")
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == ctx.project.id).values(write_epoch=7))
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code) == ("PARKED", "DIRTY_EPOCH_CHANGED")
    req = await request_of(db, ctx.approval_id)
    assert req.status == "NEEDS_REDECISION" and req.result["supersede"]["current_dirty_epoch"] == 7
    assert req.result["supersede"]["from_status"] == "APPLIED"
    gate = await gate_after(db, ctx.task.id, outcome)
    assert gate.target["dirty_epoch"] == 7
    alive = await scene(db, tmp_path, status="REFUSED_ALIVE", reason="LIVE_PROCESS_PRESENT")
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == alive.project.id).values(write_epoch=7))
    outcome, _, _ = await rig.resume(alive)
    assert outcome.code == "DIRTY_EPOCH_CHANGED"                                    # T4 wins over T6
    assert (await request_of(db, alive.approval_id)).status == "REFUSED_ALIVE"      # an already terminal status is unchanged


async def test_row_13_a_target_whose_identity_is_gone_supersedes_an_applied_request_and_opens_a_new_gate(db, tmp_path):
    rig = Rig(db, tmp_path)
    ctx = await scene(db, tmp_path, status="APPLIED", targets=[uuid.uuid4()])
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code) == ("PARKED", "EXECUTION_IDENTITY_CHANGED")
    assert (await request_of(db, ctx.approval_id)).status == "NEEDS_REDECISION"
    await gate_after(db, ctx.task.id, outcome)


# --- row 14, step L --------------------------------------------------------------------------------------------------------------------
async def test_row_14_proceeds_and_step_l_restores_the_verified_lineage_into_the_run_context(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path)
    outcome, rc, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.parked) == ("RESUME", False)
    assert await task_status(db, ctx.task.id) != "WAITING_APPROVAL"
    assert rc.resumed_from_approval_id == ctx.approval_id and rc.lineage_root_id == ctx.approval_id
    assert (await request_of(db, ctx.approval_id)).status == "APPLIED"


async def test_step_l_runs_before_the_task_state_early_return_so_a_replay_after_the_move_keeps_the_lineage(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path)
    _, _, lease = await rig.resume(ctx)                                             # row 14 committed the Task move
    outcome, rc, _ = await rig.resume(ctx, lease=lease)                             # the run ended without parking; the RESUME replays
    assert (outcome.action, outcome.code) == ("IDEMPOTENT_DONE", "TASK_NOT_WAITING_APPROVAL")
    assert rc.resumed_from_approval_id == ctx.approval_id


async def test_step_l_refuses_when_a_successor_exists_and_never_restores_from_unverified_facts(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path)
    async with db.transaction() as s:
        s.add(Approval(id=uuid.uuid4(), task_id=ctx.task.id, type="RECOVERY", status="DECIDED",
                       target={"supersedes_approval_id": str(ctx.approval_id)}, scope="TASK", request_payload={}, basis={},
                       checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1)))
        await s.execute(update(Task).where(Task.id == ctx.task.id).values(status="DEVELOPING"))
    outcome, rc, _ = await rig.resume(ctx)
    assert rc.resumed_from_approval_id is None and rc.lineage_root_id is None
    notclaimed = await scene(db, tmp_path, resume="PENDING")
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == notclaimed.task.id).values(status="DEVELOPING"))
    outcome, rc, _ = await rig.resume(notclaimed)
    assert rc.resumed_from_approval_id is None                                       # row 4's facts failed: refused


# --- rows 15, 16 (T9) ---------------------------------------------------------------------------------------------------------------------
async def test_row_15_a_decided_successor_makes_every_gate_creating_row_and_the_proceed_row_idempotent(db, tmp_path):
    for status in ("REQUESTED", "REFUSED_ALIVE", "APPLIED"):
        rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path, status=status, reason="LIVE_PROCESS_PRESENT" if status == "REFUSED_ALIVE" else None)
        async with db.transaction() as s:
            succ = Approval(id=uuid.uuid4(), task_id=ctx.task.id, type="RECOVERY", status="DECIDED",
                            target={"supersedes_approval_id": str(ctx.approval_id)}, scope="TASK", request_payload={}, basis={},
                            checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1))
            s.add(succ)
        outcome, _, lease = await rig.resume(ctx)
        assert (outcome.action, outcome.code, outcome.parked, outcome.gate_id) == ("IDEMPOTENT_DONE", "IDEMPOTENT_SUCCESSOR", False, succ.id)
        assert (await request_of(db, ctx.approval_id)).status == status               # row 8's redecision never ran
        assert await pending(db, ctx.task.id) == [] and await task_status(db, ctx.task.id) == "WAITING_APPROVAL"
        assert await lease_held(db, ctx.task.id)                                      # nothing released: it expires by its TTL


async def test_row_16_more_than_one_or_a_foreign_successor_opens_one_conflict_root_gate_and_changes_nothing_else(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path, status="REFUSED_ALIVE", reason="LIVE_PROCESS_PRESENT")
    other = await scene(db, tmp_path)
    async with db.transaction() as s:
        s.add(Approval(id=uuid.uuid4(), task_id=other.task.id, type="RECOVERY", status="DECIDED",
                       target={"supersedes_approval_id": str(ctx.approval_id)}, scope="TASK", request_payload={}, basis={},
                       checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1)))
    outcome, _, _ = await rig.resume(ctx)
    assert (outcome.action, outcome.code, outcome.parked) == ("PARKED", "APPROVAL_LINEAGE_CONFLICT", True)
    gate = await gate_after(db, ctx.task.id, outcome)
    assert gate.target["reason_code"] == P.RECOVERY_REASON_EXECUTION_UNCERTAIN and gate.target["lineage_origin"] == "LINEAGE_CONFLICT"
    assert "supersedes_approval_id" not in gate.target                                # a root: it adds no successor
    assert gate.request_payload["detail"]["lineage_conflict"]["approval_id"] == str(ctx.approval_id)
    assert (await request_of(db, ctx.approval_id)).status == "REFUSED_ALIVE"


async def test_row_16_two_direct_successors_when_the_unique_index_is_missing(db, tmp_path):
    rig, ctx = Rig(db, tmp_path), await scene(db, tmp_path)
    async with db.transaction() as s:
        await s.execute(text("DROP INDEX uq_approvals_lineage_successor"))
        for _ in range(2):
            s.add(Approval(id=uuid.uuid4(), task_id=ctx.task.id, type="RECOVERY", status="DECIDED",
                           target={"supersedes_approval_id": str(ctx.approval_id)}, scope="TASK", request_payload={}, basis={},
                           checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1)))
    outcome, _, _ = await rig.resume(ctx)
    assert outcome.code == "APPROVAL_LINEAGE_CONFLICT"
    gate = await gate_after(db, ctx.task.id, outcome)
    assert len(gate.request_payload["detail"]["lineage_conflict"]["successor_ids"]) == 2


# --- C-b and C-7: the ordinary ensure_for_step path ------------------------------------------------------------------------------------------
async def dirty_task(db):
    project, task = await make_project_task(db)
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(write_epoch=3, workspace_state="DIRTY_UNRESOLVED"))
        await s.execute(update(Task).where(Task.id == task.id).values(status="DEVELOPING"))
    return project, task


async def prior_gate(db, task, status="DECIDED"):
    async with db.transaction() as s:
        approval = Approval(id=uuid.uuid4(), task_id=task.id, type="RECOVERY", status=status, target={"lineage_root_id": str(uuid.uuid4())},
                            scope="TASK", request_payload={}, basis={}, checkpoint_step_key="DEVELOP",
                            valid_until=datetime.now(UTC) + timedelta(days=1))
        s.add(approval)
        return approval


async def acquire(db, task):
    leases = LeaseService(db, "runner-lin")
    return leases, (await leases.acquire(task.id)).lease


async def test_without_a_lineage_the_dirty_gate_target_is_byte_identical_to_the_legacy_one(db):
    project, task = await dirty_task(db)
    leases, lease = await acquire(db, task)
    assert await ProjectLockService(db, leases).ensure_for_step(lease, task.id, "DEVELOP", gates_for(db)) is StepGate.GATE_OPENED
    gate = (await pending(db, task.id))[0]
    assert set(gate.target) == {"reason", "reason_code", "step_id", "resume_step_key", "project_id", "dirty_epoch", "baseline_ref"}


async def test_with_a_lineage_the_new_gate_carries_the_four_keys_written_once_in_the_insert(db):
    project, task = await dirty_task(db)
    prev = await prior_gate(db, task)
    leases, lease = await acquire(db, task)
    lineage = GateLineage(prev.id, uuid.UUID(prev.target["lineage_root_id"]))
    assert await ProjectLockService(db, leases).ensure_for_step(lease, task.id, "DEVELOP", gates_for(db), lineage=lineage) is StepGate.GATE_OPENED
    gate = (await pending(db, task.id))[0]
    assert gate.target["supersedes_approval_id"] == str(prev.id) and gate.target["lineage_root_id"] == prev.target["lineage_root_id"]
    assert gate.target["lineage_dirty_epoch"] == 3 == gate.target["dirty_epoch"] and gate.target["lineage_origin"] == "ENSURE_FOR_STEP"


async def test_a_reused_pending_gate_is_never_updated_and_a_mismatching_lineage_only_warns(db, caplog):
    project, task = await dirty_task(db)
    leases, lease = await acquire(db, task)
    locks = ProjectLockService(db, leases)
    await locks.ensure_for_step(lease, task.id, "DEVELOP", gates_for(db))                 # a legacy gate without lineage
    before = (await pending(db, task.id))[0]
    prev = await prior_gate(db, task)
    leases2, lease2 = await acquire(db, task)
    with caplog.at_level("WARNING"):
        assert await ProjectLockService(db, leases2).ensure_for_step(
            lease2, task.id, "DEVELOP", gates_for(db), lineage=GateLineage(prev.id, prev.id)) is StepGate.GATE_OPENED
    after = await pending(db, task.id)
    assert [a.id for a in after] == [before.id] and after[0].target == before.target
    assert any("LINEAGE_REUSE_MISMATCH" in r.message for r in caplog.records)


async def test_c7_a_lineage_race_with_a_consumed_successor_drops_the_lineage_and_uses_the_legacy_path(db):
    project, task = await dirty_task(db)
    prev = await prior_gate(db, task)
    async with db.transaction() as s:                                                    # a DECIDED successor already uses the link
        s.add(Approval(id=uuid.uuid4(), task_id=task.id, type="RECOVERY", status="DECIDED",
                       target={"supersedes_approval_id": str(prev.id)}, scope="TASK", request_payload={}, basis={},
                       checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1)))
    leases, lease = await acquire(db, task)
    result = await ProjectLockService(db, leases).ensure_for_step(lease, task.id, "DEVELOP", gates_for(db),
                                                                  lineage=GateLineage(prev.id, prev.id))
    assert result is StepGate.GATE_OPENED
    gates = await pending(db, task.id)
    assert len(gates) == 1 and "supersedes_approval_id" not in gates[0].target             # legacy gate: it cannot violate the index


async def test_c7_a_pending_successor_of_another_kind_parks_the_run_on_it(db):
    project, task = await dirty_task(db)
    prev = await prior_gate(db, task)
    async with db.transaction() as s:
        succ = Approval(id=uuid.uuid4(), task_id=task.id, type="PERMISSION", status="PENDING",
                        target={"supersedes_approval_id": str(prev.id)}, scope="TASK", request_payload={}, basis={},
                        checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1))
        s.add(succ)
    leases, lease = await acquire(db, task)
    result = await ProjectLockService(db, leases).ensure_for_step(lease, task.id, "DEVELOP", gates_for(db),
                                                                  lineage=GateLineage(prev.id, prev.id))
    assert result is StepGate.GATE_OPENED
    assert [a.id for a in await pending(db, task.id)] == [succ.id]
    async with db.transaction() as s:
        assert (await s.execute(text("select count(*) from task_runs where task_id = :t and status = 'RUNNING'"), {"t": task.id})).scalar_one() == 0


async def test_c7_no_visible_successor_is_a_retry_signal_and_the_helper_reads_the_real_exception_structure(db):
    project, task = await dirty_task(db)
    prev = await prior_gate(db, task)
    leases, lease = await acquire(db, task)
    locks = ProjectLockService(db, leases)
    assert await locks._after_lineage_race(lease, task.id, GateLineage(prev.id, prev.id)) is StepGate.READY
    async with db.transaction() as s:
        s.add(Approval(id=uuid.uuid4(), task_id=task.id, type="RECOVERY", status="DECIDED",
                       target={"supersedes_approval_id": str(prev.id)}, scope="TASK", request_payload={}, basis={},
                       checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1)))
    with pytest.raises(IntegrityError) as info:                                           # the REAL exception of the installed stack
        async with db.transaction() as s:
            s.add(Approval(id=uuid.uuid4(), task_id=task.id, type="RECOVERY", status="DECIDED",
                           target={"supersedes_approval_id": str(prev.id)}, scope="TASK", request_payload={}, basis={},
                           checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1)))
    assert is_lineage_violation(info.value) is True
    with pytest.raises(IntegrityError) as other:                                          # another unique index: NOT the lineage race
        async with db.transaction() as s:
            for _ in range(2):
                s.add(Approval(id=uuid.uuid4(), task_id=task.id, type="RECOVERY", status="PENDING",
                               target={"reason_code": "PROJECT_DIRTY"}, scope="TASK", request_payload={}, basis={},
                               checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1)))
                await s.flush()
    assert is_lineage_violation(other.value) is False


# --- the guard codes ---------------------------------------------------------------------------------------------------------------------------
async def test_the_continue_guard_is_a_plain_conflict_with_the_two_named_codes_and_the_ids_in_the_message(db):
    project, task = await make_project_task(db)
    row = await add_row(db, task, pid=1)
    async with db.transaction() as s:
        with pytest.raises(ProjectUnresolvedTrackedExecution) as info:
            await assert_no_unresolved_or_anomalous_tracked(s, project.id)
    assert info.value.status_code == 409 and info.value.code == "PROJECT_UNRESOLVED_TRACKED_EXECUTION" and str(row) in info.value.message
    assert info.value.details["reason_code"] == "UNRESOLVED_TRACKED_EXECUTION"
    async with db.transaction() as s:
        await s.execute(text("UPDATE executions SET detail = '{\"tracked\": \"yes\"}'::jsonb, finished_at = now() WHERE id = :i"), {"i": row})
    async with db.transaction() as s:
        with pytest.raises(ProjectTrackedExecutionAnomaly) as info:
            await assert_no_unresolved_or_anomalous_tracked(s, project.id)
    assert info.value.code == "PROJECT_TRACKED_EXECUTION_ANOMALY" and "OTHER_SCHEMA_INVALID" in info.value.message


async def test_a_decision_created_resume_is_insert_if_absent(db):
    from app.services.human_gate_service import HumanGateService
    from tests.integration_pg.test_risk_options import open_gate
    project, task = await make_project_task(db)
    gates = gates_for(db)
    approval = await open_gate(db, gates, project, task, state="CLEAN", epoch=1, dirty_epoch=1)
    async with db.transaction() as s:
        s.add(ExecutionRequest(id=uuid.uuid4(), task_id=task.id, kind="RESUME", status="PENDING", approval_id=approval))
    outcome = await gates.decide(approval, decision="APPROVED", selected_option="continue", rationale=None, idempotency_key="k-resume")
    assert outcome.created
    async with db.transaction() as s:
        assert (await s.execute(text("select count(*) from execution_requests where approval_id = :a"), {"a": approval})).scalar_one() == 1
