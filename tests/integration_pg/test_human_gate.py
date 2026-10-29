import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app.models.states import ApprovalType
from app.models.tables import Approval, ApprovalDecision, DesignDecision, ExecutionRequest
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.task_repository import TaskRepository
from app.services.human_gate_service import HumanGateService, IdempotencyConflict, StaleBasis
from app.validation.exceptions import Conflict, Gone, NotFound, ValidationFailed
from tests.helpers import FakeClock, make_step, make_task

PAYLOAD = {"problem": "pick", "options": [{"id": "A"}, {"id": "B"}], "recommendation": "A"}


async def open_design_gate(db, gate, *, status="DESIGN_REVIEWING"):
    task = await make_task(db, status=status)
    step = await make_step(db, task, step_key="DESIGN_REVIEW")
    async with db.transaction() as s:
        t = await TaskRepository(s).lock(task.id)
        st = await TaskRepository(s).get_step(step.id)
        approval = await gate.open_gate(s, t, st, type_=ApprovalType.DESIGN, checkpoint_step_key="PLAN",
                                        request_payload=PAYLOAD, target={"decision": "x"}, scope="STEP")
    return task, step, approval


async def count(db, model, *where):
    async with db.transaction() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar()


async def decide(gate, approval, decision="APPROVED", option="A", key="k1"):
    return await gate.decide(approval.id, decision=decision, selected_option=option, rationale=None,
                             idempotency_key=key)


async def test_open_gate_moves_task_and_step_to_waiting_approval_together(db):
    gate = HumanGateService(db)
    task, step, _ = await open_design_gate(db, gate)
    async with db.transaction() as s:
        t = await TaskRepository(s).get(task.id)
        st = await TaskRepository(s).get_step(step.id)
    assert t.status == "WAITING_APPROVAL" and t.resume_step_key == "PLAN"
    assert st.status == "WAITING_APPROVAL"          # waiting is not a failure


async def test_new_decision_stores_design_decision_and_a_single_resume_request(db):
    gate = HumanGateService(db)
    task, _, approval = await open_design_gate(db, gate)
    out = await decide(gate, approval)
    assert out.created and out.decision.decision == "APPROVED"
    assert await count(db, DesignDecision, DesignDecision.task_id == task.id) == 1
    assert await count(db, ExecutionRequest, ExecutionRequest.approval_id == approval.id,
                       ExecutionRequest.kind == "RESUME", ExecutionRequest.status == "PENDING") == 1
    async with db.transaction() as s:
        assert (await TaskRepository(s).get(task.id)).decisions_hash != ""


async def test_identical_resend_returns_existing_result_without_new_side_effects(db):
    gate = HumanGateService(db)
    _, _, approval = await open_design_gate(db, gate)
    first = await decide(gate, approval, key="k1")
    again = await decide(gate, approval, key="k1")
    other_key = await decide(gate, approval, key="k2")
    assert first.created and not again.created and not other_key.created
    assert again.decision.id == first.decision.id == other_key.decision.id
    assert await count(db, ExecutionRequest, ExecutionRequest.approval_id == approval.id) == 1


async def test_different_decision_or_option_conflicts(db):
    gate = HumanGateService(db)
    _, _, approval = await open_design_gate(db, gate)
    await decide(gate, approval, key="k1")
    with pytest.raises(Conflict):
        await decide(gate, approval, option="B", key="k2")
    with pytest.raises(Conflict):
        await decide(gate, approval, decision="REJECTED", option=None, key="k3")
    with pytest.raises(IdempotencyConflict):   # same key, different request
        await decide(gate, approval, decision="REJECTED", option=None, key="k1")


async def test_existing_decision_is_compared_before_expiry_and_staleness_checks(db):
    clock = FakeClock()
    gate = HumanGateService(db, clock=clock)
    _, _, approval = await open_design_gate(db, gate)
    first = await decide(gate, approval)
    clock.advance(10 * 24 * 3600)              # approval is long expired and the basis has moved on
    again = await decide(gate, approval)
    assert not again.created and again.decision.id == first.decision.id


async def test_new_decision_on_expired_approval_is_rejected(db):
    clock = FakeClock()
    gate = HumanGateService(db, clock=clock)
    _, _, approval = await open_design_gate(db, gate)
    clock.advance(10 * 24 * 3600)
    with pytest.raises(Gone):
        await decide(gate, approval)
    assert await count(db, ApprovalDecision) == 0


async def test_new_decision_on_stale_basis_is_rejected(db):
    gate = HumanGateService(db)
    task, _, approval = await open_design_gate(db, gate)
    async with db.transaction() as s:                      # requirements/decisions moved after the request
        t = await TaskRepository(s).lock(task.id)
        t.decisions_hash = "something-else"
    with pytest.raises(StaleBasis):
        await decide(gate, approval)
    assert await count(db, ApprovalDecision) == 0
    assert await count(db, ExecutionRequest) == 0


async def test_concurrent_identical_decisions_store_exactly_one(db):
    gate = HumanGateService(db)
    _, _, approval = await open_design_gate(db, gate)
    results = await asyncio.gather(*[decide(gate, approval, key=f"k{i}") for i in range(8)])
    assert sum(1 for r in results if r.created) == 1
    assert await count(db, ApprovalDecision) == 1
    assert await count(db, ExecutionRequest, ExecutionRequest.approval_id == approval.id) == 1


async def test_concurrent_conflicting_decisions_only_one_wins(db):
    gate = HumanGateService(db)
    _, _, approval = await open_design_gate(db, gate)
    results = await asyncio.gather(
        decide(gate, approval, option="A", key="a"), decide(gate, approval, option="B", key="b"),
        decide(gate, approval, decision="REJECTED", option=None, key="c"), return_exceptions=True)
    assert sum(1 for r in results if not isinstance(r, Exception)) == 1
    assert all(isinstance(r, Conflict) for r in results if isinstance(r, Exception))
    assert await count(db, ApprovalDecision) == 1


async def test_validation_and_missing_approval(db):
    gate = HumanGateService(db)
    _, _, approval = await open_design_gate(db, gate)
    with pytest.raises(ValidationFailed):
        await gate.decide(approval.id, decision="APPROVED", selected_option="A", rationale=None, idempotency_key=None)
    with pytest.raises(ValidationFailed):      # option not offered
        await decide(gate, approval, option="Z")
    with pytest.raises(NotFound):
        await gate.decide(uuid.uuid4(), decision="APPROVED", selected_option="A", rationale=None,
                          idempotency_key="k")


async def test_design_approval_never_grants_a_cli_permission(db):
    gate = HumanGateService(db)
    _, _, approval = await open_design_gate(db, gate)
    await decide(gate, approval)
    async with db.transaction() as s:
        row = await ApprovalRepository(s).get(approval.id)
    assert row.type == "DESIGN" and row.used_at is None and row.command_fingerprint is None


async def open_recovery(db, gate, task, step):
    async with db.transaction() as s:
        t = await TaskRepository(s).lock(task.id)
        return await gate.open_recovery(s, t, await TaskRepository(s).get_step(step.id), reason="cli unconfirmed",
                                        checkpoint_step_key="DEVELOP")


async def test_recovery_gate_is_one_atomic_state_change(db):
    gate = HumanGateService(db)
    task = await make_task(db, status="DEVELOPING")
    step = await make_step(db, task)
    approval = await open_recovery(db, gate, task, step)
    async with db.transaction() as s:
        t = await TaskRepository(s).get(task.id)
        st = await TaskRepository(s).get_step(step.id)
        row = await ApprovalRepository(s).get(approval.id)
    assert (t.status, st.status, row.type) == ("WAITING_APPROVAL", "NEEDS_MANUAL_RECOVERY", "RECOVERY")


async def test_recovery_gate_rolls_back_completely_on_failure(db):
    gate = HumanGateService(db)
    task = await make_task(db, status="DEVELOPING")
    step = await make_step(db, task)
    with pytest.raises(RuntimeError):
        async with db.transaction() as s:
            t = await TaskRepository(s).lock(task.id)
            st = await TaskRepository(s).get_step(step.id)
            await gate.open_recovery(s, t, st, reason="x", checkpoint_step_key="DEVELOP")
            raise RuntimeError("crash before commit")
    async with db.transaction() as s:
        assert (await TaskRepository(s).get(task.id)).status == "DEVELOPING"
        assert (await TaskRepository(s).get_step(step.id)).status == "RUNNING"
    assert await count(db, Approval) == 0


async def test_recovery_abort_fails_the_task_and_creates_no_resume(db):
    gate = HumanGateService(db)
    task = await make_task(db, status="DEVELOPING")
    approval = await open_recovery(db, gate, task, await make_step(db, task))
    await decide(gate, approval, option="abort")
    async with db.transaction() as s:
        t = await TaskRepository(s).get(task.id)
    assert t.status == "FAILED" and t.failure_reason == "RECOVERY_ABORTED"
    assert await count(db, ExecutionRequest) == 0


async def test_recovery_retry_creates_resume_request(db):
    gate = HumanGateService(db)
    task = await make_task(db, status="DEVELOPING")
    approval = await open_recovery(db, gate, task, await make_step(db, task))
    await decide(gate, approval, option="retry")
    assert await count(db, ExecutionRequest, ExecutionRequest.kind == "RESUME") == 1


async def open_permission(db, gate, task, step, *, enforceable):
    async with db.transaction() as s:
        t = await TaskRepository(s).lock(task.id)
        return await gate.open_gate(
            s, t, await TaskRepository(s).get_step(step.id), type_=ApprovalType.PERMISSION,
            checkpoint_step_key="DEVELOP", request_payload={"options": [{"id": "allow_once"}]},
            target={"tool": "Bash", "enforceable": enforceable}, scope="COMMAND", command_fingerprint="fp")


async def test_non_enforceable_permission_approval_holds_instead_of_widening(db):
    gate = HumanGateService(db)
    task = await make_task(db, status="DEVELOPING")
    approval = await open_permission(db, gate, task, await make_step(db, task), enforceable=False)
    await decide(gate, approval, option="allow_once")
    assert await count(db, ExecutionRequest) == 0       # nothing resumes; permissions were not widened
    async with db.transaction() as s:
        assert (await TaskRepository(s).get(task.id)).status == "WAITING_APPROVAL"


async def test_permission_is_consumed_exactly_once_under_concurrency(db):
    gate = HumanGateService(db)
    task = await make_task(db, status="DEVELOPING")
    approval = await open_permission(db, gate, task, await make_step(db, task), enforceable=True)

    async def consume():
        async with db.transaction() as s:
            return await ApprovalRepository(s).consume_permission(approval.id)
    results = await asyncio.gather(*[consume() for _ in range(8)])
    assert results.count(True) == 1 and results.count(False) == 7
