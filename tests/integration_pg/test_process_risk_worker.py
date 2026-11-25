"""B2 phase 5 (design 7.5 items 4 to 8): the process-risk request lifecycle and its worker on the embedded PostgreSQL. Real rows,
real short-lived processes for the live-process cases; the OS verification is injected only where a case needs a fixed verdict."""
import contextlib
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from app.infra.process_control import capture_identity
from app.models.tables import Approval, ApprovalDecision, Execution, ExecutionRequest, Project, Task
from app.repositories.process_risk_repository import ProcessRiskRequestRepository
from app.repositories.task_repository import TaskRepository
from app.services.lease_service import TrackedVerdict
from app.services.process_risk_worker import ProcessRiskWorker, recheck_cooldown_seconds, retry_delay_seconds
from app.services.tracked_classifier import RowClass, classify_row
from app.validation import policies as P
from tests.integration_pg.test_orphan_sweeper import add_row, detail_of, tracked
from tests.integration_pg.test_process_schema import ids_where, make_project_task
from tests.integration_pg.test_tracked_takeover import finished_process, runner_process

INC_A, INC_B = uuid.uuid4(), uuid.uuid4()


@contextlib.asynccontextmanager
async def section():
    yield


class Notes:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1


def worker(db, notes=None, me=INC_A, **kw) -> ProcessRiskWorker:
    return ProcessRiskWorker(db, section, incarnation=lambda: me, notify=notes or Notes(), **kw)


def fixed(verdict: TrackedVerdict):
    return lambda stale: verdict


UNCLEAR_V = TrackedVerdict(False, reason="IDENTITY_RECORD_MISSING")
PROVABLE_V = TrackedVerdict(True, "JOB_KILL_ON_CLOSE_ASSUMED", "ALREADY_EXITED", None)


async def setup(db, *, kind="ACCEPT", n=1, epoch=1, option=None, rows=None, status="WAITING_APPROVAL", targets=None, pt=None):
    """A project at `epoch`, a WAITING_APPROVAL task, a DECIDED RECOVERY approval with its decision and the REQUESTED request."""
    project, task = pt or await make_project_task(db)
    ids = rows if rows is not None else [await add_row(db, task, pid=4242 + i) for i in range(n)]
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(write_epoch=epoch, workspace_state="DIRTY_UNRESOLVED"))
        await s.execute(update(Task).where(Task.id == task.id).values(status=status))
        approval = Approval(id=uuid.uuid4(), task_id=task.id, type="RECOVERY", status="DECIDED", target={"reason_code": "PROJECT_DIRTY"},
                            scope="TASK", request_payload={}, basis={}, checkpoint_step_key="DEVELOP",
                            valid_until=datetime.now(UTC) + timedelta(days=1))
        s.add(approval)
        await s.flush()
        s.add(ApprovalDecision(approval_id=approval.id, decision="APPROVED", rationale="r", idempotency_key="k" + uuid.uuid4().hex,
                               request_fingerprint="f", basis={},
                               selected_option=option or {"ACCEPT": "accept_unproven_risk", "RECHECK": "recheck_processes"}[kind]))
        await ProcessRiskRequestRepository(s).insert(
            approval_id=approval.id, kind=kind, task_id=task.id, project_id=project.id, dirty_epoch=epoch,
            target_execution_ids=targets or ids, rationale="the process is gone, I checked", requested_by="op@example")
        return SimpleNamespace_(project=project, task=task, approval_id=approval.id, ids=sorted(ids))


class SimpleNamespace_:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def request_of(db, approval_id):
    async with db.transaction() as s:
        return await ProcessRiskRequestRepository(s).get(approval_id)


async def resumes(db, approval_id) -> int:
    async with db.transaction() as s:
        return (await s.execute(text("SELECT count(*) FROM execution_requests WHERE approval_id = :a AND kind = 'RESUME'"),
                                {"a": approval_id})).scalar_one()


async def klass(db, row_id) -> RowClass:
    row = await tracked(db, row_id)
    return classify_row(row.id, row.pid, row.finished_at, row.detail_text).row_class


# --- ACCEPT ------------------------------------------------------------------------------------------------------------------
async def test_an_accept_request_gives_every_unclear_row_a_valid_disposition_and_inserts_one_resume_after_the_commit(db):
    ctx = await setup(db, n=2)
    notes = Notes()
    assert await worker(db, notes, tracked_verdict=fixed(UNCLEAR_V))() == 1
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.attempts, req.finished_at is not None) == ("APPLIED", 0, True)
    for row_id in ctx.ids:
        assert await klass(db, row_id) is RowClass.RESOLVED_RISK                 # the classifier validates the whole schema
        record = detail_of(await tracked(db, row_id))["risk_acceptance"]
        assert record["requested_by"] == "op@example" and record["reverify_result"] == "IDENTITY_RECORD_MISSING"
    assert (await resumes(db, ctx.approval_id), notes.count) == (1, 1)
    assert await ids_where(db, "(detail ->> 'proof') IS NOT NULL") == set()      # a disposition is never a proof
    assert await ids_where(db, "(detail ->> 'disposition') = 'OPERATOR_RISK_ACCEPTED' AND finished_at IS NOT NULL AND outcome = 'ERROR'") == set(ctx.ids)


async def test_targets_of_another_project_are_an_identity_change_and_leave_the_rows_untouched(db):
    project, task = await make_project_task(db)
    open_id, closed_id = await add_row(db, task, pid=4242), await add_row(db, task, pid=4243, finished=True)
    ctx = await setup(db, rows=[open_id, closed_id])
    closed_before = (await tracked(db, closed_id)).finished_at
    await worker(db, tracked_verdict=fixed(UNCLEAR_V))()
    # the rows belong to another task of ANOTHER project: identity (project) check refuses the request instead
    assert (await request_of(db, ctx.approval_id)).reason == "EXECUTION_IDENTITY_CHANGED"
    assert (await tracked(db, closed_id)).finished_at == closed_before


async def test_an_open_unproven_and_a_closed_unproven_row_of_the_same_project_are_both_accepted(db):
    project, task = await make_project_task(db)
    open_id, closed_id = await add_row(db, task, pid=4242), await add_row(db, task, pid=4243, finished=True)
    ctx = await setup(db, pt=(project, task), rows=[open_id, closed_id])
    closed_before = (await tracked(db, closed_id)).finished_at
    await worker(db, tracked_verdict=fixed(UNCLEAR_V))()
    assert (await request_of(db, ctx.approval_id)).status == "APPLIED"
    assert (await tracked(db, closed_id)).finished_at == closed_before
    assert (await tracked(db, open_id)).finished_at is not None
    assert await ids_where(db, "terminated_at IS NOT NULL") == set()


async def test_a_live_survivor_refuses_the_whole_request_and_no_disposition_or_resume_is_written(db):
    project, task = await make_project_task(db)
    owner = finished_process()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ident = capture_identity(proc.pid)
        alive_id = await add_row(db, task, pid=ident.pid, create_time=ident.create_time, runner=runner_process(owner))
        never_ran_id = await add_row(db, task)                                 # PROVABLE: its true proof is still written
        unclear_id = await add_row(db, task, pid=4242)
        ctx = await setup(db, pt=(project, task), rows=[alive_id, never_ran_id, unclear_id])
        notes = Notes()
        await worker(db, notes)()
        req = await request_of(db, ctx.approval_id)
        assert (req.status, req.reason) == ("REFUSED_ALIVE", "LIVE_PROCESS_PRESENT") and str(alive_id) in req.result["survivors"]
        assert await klass(db, never_ran_id) is RowClass.RESOLVED_PROVEN
        assert await klass(db, unclear_id) is RowClass.UNRESOLVED and await klass(db, alive_id) is RowClass.UNRESOLVED
        assert (await resumes(db, ctx.approval_id), notes.count) == (0, 0)
    finally:
        proc.kill()
        proc.wait()


# --- RECHECK -----------------------------------------------------------------------------------------------------------------
async def test_a_recheck_proves_what_it_can_never_writes_a_disposition_and_applies_when_everything_is_proven(db):
    ctx = await setup(db, kind="RECHECK", n=1)
    await worker(db, tracked_verdict=fixed(PROVABLE_V))()
    req = await request_of(db, ctx.approval_id)
    assert req.status == "APPLIED" and await resumes(db, ctx.approval_id) == 1
    assert await klass(db, ctx.ids[0]) is RowClass.RESOLVED_PROVEN
    assert await ids_where(db, "(detail ->> 'disposition') IS NOT NULL") == set()


async def test_a_recheck_with_no_live_process_but_an_unprovable_row_needs_a_redecision(db):
    ctx = await setup(db, kind="RECHECK", n=1)
    await worker(db, tracked_verdict=fixed(UNCLEAR_V))()
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.reason) == ("NEEDS_REDECISION", "NO_LIVE_PROCESS_UNPROVEN")
    assert await resumes(db, ctx.approval_id) == 0 and await klass(db, ctx.ids[0]) is RowClass.UNRESOLVED


# --- the codes of tiers T1 to T5 -----------------------------------------------------------------------------------------------
async def test_a_changed_dirty_epoch_is_decided_before_any_os_work_and_writes_nothing_else(db):
    ctx = await setup(db, epoch=1)
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == ctx.project.id).values(write_epoch=2))
    calls = []
    await worker(db, tracked_verdict=lambda stale: calls.append(1) or UNCLEAR_V)()
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.reason, calls) == ("NEEDS_REDECISION", "DIRTY_EPOCH_CHANGED", [])
    assert await resumes(db, ctx.approval_id) == 0 and await klass(db, ctx.ids[0]) is RowClass.UNRESOLVED


@pytest.mark.parametrize("mutation,code", [
    ("task_state", "TASK_NOT_WAITING_APPROVAL"), ("other_pending", "GATE_ALREADY_PENDING"),
    ("option", "RISK_REQUEST_NOT_APPLICABLE"), ("approval_task", "APPROVAL_NOT_APPLICABLE"),
    ("project_link", "TASK_PROJECT_MISMATCH")])
async def test_each_tier_code_is_written_with_no_disposition_and_no_resume(db, mutation, code):
    ctx = await setup(db)
    async with db.transaction() as s:
        if mutation == "task_state":
            await s.execute(update(Task).where(Task.id == ctx.task.id).values(status="FAILED"))
        elif mutation == "other_pending":
            s.add(Approval(id=uuid.uuid4(), task_id=ctx.task.id, type="DESIGN", status="PENDING", target={}, scope="TASK",
                           request_payload={}, basis={}, checkpoint_step_key="PLAN", valid_until=datetime.now(UTC) + timedelta(days=1)))
        elif mutation == "option":
            await s.execute(update(ApprovalDecision).where(ApprovalDecision.approval_id == ctx.approval_id)
                            .values(selected_option="recheck_processes"))
        elif mutation == "approval_task":
            other = await _other_task(db)
            await s.execute(text("UPDATE approvals SET task_id = :t WHERE id = :a"), {"t": other.id, "a": ctx.approval_id})
        elif mutation == "project_link":
            await s.execute(update(Task).where(Task.id == ctx.task.id).values(project_id=None))
    await worker(db, tracked_verdict=fixed(UNCLEAR_V))()
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.reason) == ("NEEDS_REDECISION", code)
    if code in ("APPROVAL_NOT_APPLICABLE", "TASK_PROJECT_MISMATCH"):
        assert req.result["defensive"]["code"] == code                          # the audit record of item 6 (a)
    assert await resumes(db, ctx.approval_id) == 0 and await klass(db, ctx.ids[0]) is RowClass.UNRESOLVED


async def _other_task(db):
    from tests.helpers import make_task
    return await make_task(db)


async def test_an_execution_identity_that_changes_during_the_verification_is_caught_in_the_result_transaction(db):
    ctx = await setup(db)

    async def change(fn, stale):
        verdict = fn(stale)
        async with db.transaction() as s:
            await s.execute(text("UPDATE executions SET pid = pid + 1 WHERE id = :i"), {"i": ctx.ids[0]})
        return verdict

    await worker(db, tracked_verdict=fixed(UNCLEAR_V), run_blocking=change)()
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.reason) == ("NEEDS_REDECISION", "EXECUTION_IDENTITY_CHANGED")
    assert await klass(db, ctx.ids[0]) is RowClass.UNRESOLVED


async def test_a_target_that_became_an_anomaly_is_an_error_and_never_overwritten(db):
    ctx = await setup(db)
    async with db.transaction() as s:
        await s.execute(text("UPDATE executions SET detail = detail || '{\"proof\": \"BOGUS\"}'::jsonb WHERE id = :i"), {"i": ctx.ids[0]})
    await worker(db, tracked_verdict=fixed(UNCLEAR_V))()
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.attempts) == ("RETRYABLE", 1)
    assert detail_of(await tracked(db, ctx.ids[0]))["proof"] == "BOGUS"


# --- retries ---------------------------------------------------------------------------------------------------------------------
async def test_a_verification_error_is_retryable_with_a_capped_backoff_then_needs_redecision_at_the_attempt_limit(db):
    ctx = await setup(db)

    def boom(stale):
        raise OSError("probe failed")

    w = worker(db, tracked_verdict=boom)
    await w()
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.attempts, req.last_error, req.finished_at) == ("RETRYABLE", 1, "VERIFICATION_ERROR", None)
    assert req.next_attempt_at > datetime.now(UTC) + timedelta(seconds=P.PROCESS_RISK_BACKOFF_BASE_SECONDS - 2)
    assert await w() == 0                                                         # not due yet: nothing is claimed
    for attempt in range(2, P.PROCESS_RISK_MAX_ATTEMPTS):
        async with db.transaction() as s:
            await s.execute(text("UPDATE process_risk_requests SET next_attempt_at = now() - interval '1 second' WHERE approval_id = :a"),
                            {"a": ctx.approval_id})
        await w()
        assert (await request_of(db, ctx.approval_id)).attempts == attempt
    async with db.transaction() as s:
        await s.execute(text("UPDATE process_risk_requests SET next_attempt_at = now() - interval '1 second' WHERE approval_id = :a"),
                        {"a": ctx.approval_id})
    await w()
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.reason, req.attempts) == ("NEEDS_REDECISION", "PROCESS_RECHECK_INCONCLUSIVE", P.PROCESS_RISK_MAX_ATTEMPTS)
    assert req.finished_at is not None and await klass(db, ctx.ids[0]) is RowClass.UNRESOLVED


def test_the_backoff_and_the_cooldown_are_capped_exponentials():
    assert [retry_delay_seconds(n) for n in (1, 2, 3, 4, 5, 9)] == [5, 10, 20, 40, 60, 60]
    assert [recheck_cooldown_seconds(k) for k in (0, 1, 2, 3, 4, 9)] == [60, 120, 240, 480, 900, 900]


# --- claim, fencing, reclaim, batch limits -------------------------------------------------------------------------------------
async def test_the_claim_is_one_conditional_update_and_a_stale_generation_cannot_write(db):
    ctx = await setup(db)
    timeout = P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS
    async with db.transaction() as s:
        repo = ProcessRiskRequestRepository(s)
        g1 = await repo.claim(ctx.approval_id, INC_A, timeout)
        assert g1 == 1
        assert await repo.claim(ctx.approval_id, INC_A, timeout) is None            # same incarnation, fresh claim: not reclaimable
        g2 = await repo.claim(ctx.approval_id, INC_B, timeout)                       # another incarnation: the old one is dead
        assert g2 == 2
        assert await repo.write_result(ctx.approval_id, g1, status="APPLIED") is False   # the lost claim writes nothing
        assert (await repo.get(ctx.approval_id)).status == "VERIFYING"
        assert await repo.lock_fenced(ctx.approval_id, g1) is None and await repo.lock_fenced(ctx.approval_id, g2) is not None
        assert await repo.write_result(ctx.approval_id, g2, status="APPLIED") is True
        assert await repo.claim(ctx.approval_id, INC_B, timeout) is None            # terminal: never claimed again


async def test_a_verifying_request_older_than_the_claim_timeout_is_reclaimed_by_the_same_incarnation(db):
    ctx = await setup(db)
    async with db.transaction() as s:
        repo = ProcessRiskRequestRepository(s)
        await repo.claim(ctx.approval_id, INC_A, 300)
        await s.execute(text("UPDATE process_risk_requests SET claimed_at = now() - interval '301 seconds' WHERE approval_id = :a"),
                        {"a": ctx.approval_id})
        assert [r.approval_id for r in await repo.list_claimable(INC_A, 300, 5)] == [ctx.approval_id]
        assert await repo.claim(ctx.approval_id, INC_A, 300) == 2


async def test_a_worker_that_lost_its_claim_writes_nothing_in_the_result_transaction(db):
    ctx = await setup(db)
    w = worker(db, tracked_verdict=fixed(UNCLEAR_V))
    request, generation = await w._claim_next()
    async with db.transaction() as s:                                               # another incarnation takes over
        assert await ProcessRiskRequestRepository(s).claim(ctx.approval_id, INC_B, 0) == generation + 1
    await w._process(request, generation)
    assert (await request_of(db, ctx.approval_id)).status == "VERIFYING"
    assert await klass(db, ctx.ids[0]) is RowClass.UNRESOLVED and await resumes(db, ctx.approval_id) == 0


async def test_an_existing_resume_is_reused_and_never_duplicated(db):
    ctx = await setup(db)
    async with db.transaction() as s:
        s.add(ExecutionRequest(id=uuid.uuid4(), task_id=ctx.task.id, kind="RESUME", status="PENDING", approval_id=ctx.approval_id))
    notes = Notes()
    await worker(db, notes, tracked_verdict=fixed(UNCLEAR_V))()
    assert (await request_of(db, ctx.approval_id)).status == "APPLIED"
    assert (await resumes(db, ctx.approval_id), notes.count) == (1, 0)              # nothing inserted, so nothing to wake


async def test_a_pass_handles_at_most_the_batch_of_requests_oldest_first(db, monkeypatch):
    first = await setup(db)
    second = await setup(db)
    monkeypatch.setattr(P, "PROCESS_RISK_BATCH_REQUESTS", 1)
    w = worker(db, tracked_verdict=fixed(UNCLEAR_V))
    assert await w() == 1
    assert (await request_of(db, first.approval_id)).status == "APPLIED"
    assert (await request_of(db, second.approval_id)).status == "REQUESTED"
    assert await w() == 1 and (await request_of(db, second.approval_id)).status == "APPLIED"


async def test_a_pass_stops_claiming_when_less_than_a_request_timeout_of_the_budget_is_left(db):
    ctx = await setup(db)
    times = iter([0.0] + [P.PROCESS_RISK_PASS_SECONDS - 1.0] * 20)
    assert await worker(db, clock=lambda: next(times))() == 0
    assert (await request_of(db, ctx.approval_id)).status == "REQUESTED"


# --- the request table lifecycle -----------------------------------------------------------------------------------------------
async def test_the_single_supersede_moves_applied_to_needs_redecision_once_and_keeps_the_audit(db):
    ctx = await setup(db)
    await worker(db, tracked_verdict=fixed(UNCLEAR_V))()
    async with db.transaction() as s:
        repo = ProcessRiskRequestRepository(s)
        assert await repo.supersede_applied(ctx.approval_id, {"code": "DIRTY_EPOCH_CHANGED"}, "DIRTY_EPOCH_CHANGED") is True
        assert await repo.supersede_applied(ctx.approval_id, {"code": "again"}, "X") is False
        req = await repo.get(ctx.approval_id)
        assert (req.status, req.reason, req.result["supersede"]) == ("NEEDS_REDECISION", "DIRTY_EPOCH_CHANGED", {"code": "DIRTY_EPOCH_CHANGED"})
        assert "rows" in req.result                                                 # the worker's audit survives the merge


async def test_a_supersede_never_touches_a_request_that_is_not_applied(db):
    ctx = await setup(db)
    async with db.transaction() as s:
        assert await ProcessRiskRequestRepository(s).supersede_applied(ctx.approval_id, {}, "X") is False


async def test_by_approvals_is_one_batch_read_and_missing_ids_are_absent(db):
    a, b = await setup(db), await setup(db)
    async with db.transaction() as s:
        got = await ProcessRiskRequestRepository(s).by_approvals([a.approval_id, b.approval_id, uuid.uuid4()])
        assert set(got) == {a.approval_id, b.approval_id}
        assert await ProcessRiskRequestRepository(s).by_approvals([]) == {}


async def test_recheck_observation_queries_active_count_streak_window_and_the_unique_active_index(db):
    ctx = await setup(db, kind="RECHECK", epoch=3)
    pid, ep = ctx.project.id, 3
    async with db.transaction() as s:
        repo = ProcessRiskRequestRepository(s)
        assert await repo.active_recheck_count(pid, ep) == 1 and await repo.active_recheck_count(pid, ep + 1) == 0
        assert await repo.latest_terminal(pid) is None
    # a second active recheck for the same project and epoch violates uq_process_risk_recheck_active
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            approval = Approval(id=uuid.uuid4(), task_id=ctx.task.id, type="RECOVERY", status="DECIDED", target={}, scope="TASK",
                                request_payload={}, basis={}, checkpoint_step_key="DEVELOP",
                                valid_until=datetime.now(UTC) + timedelta(days=1))
            s.add(approval)
            await s.flush()
            await ProcessRiskRequestRepository(s).insert(approval_id=approval.id, kind="RECHECK", task_id=ctx.task.id, project_id=pid,
                                                         dirty_epoch=ep, target_execution_ids=[uuid.uuid4()], rationale="x")
    # finish it as REFUSED_ALIVE: the streak and the latest terminal request follow, the active slot is free again
    async with db.transaction() as s:
        repo = ProcessRiskRequestRepository(s)
        gen = await repo.claim(ctx.approval_id, INC_A, 300)
        await repo.write_result(ctx.approval_id, gen, status="REFUSED_ALIVE", reason="LIVE_PROCESS_PRESENT")
    async with db.transaction() as s:
        repo = ProcessRiskRequestRepository(s)
        assert await repo.active_recheck_count(pid, ep) == 0
        assert (await repo.latest_terminal(pid, kind="RECHECK", dirty_epoch=ep)).approval_id == ctx.approval_id
        assert await repo.recheck_refused_streak(pid, ep, 10) == 1
        count, oldest = await repo.recheck_window(pid, ep, 3600)
        assert (count, oldest.approval_id) == (1, ctx.approval_id)
        assert await repo.recheck_window(pid, ep, 0.000001) == (0, None)


async def test_a_worker_result_status_is_validated_and_retryable_needs_a_next_attempt(db):
    ctx = await setup(db)
    async with db.transaction() as s:
        repo = ProcessRiskRequestRepository(s)
        gen = await repo.claim(ctx.approval_id, INC_A, 300)
        with pytest.raises(ValueError):
            await repo.write_result(ctx.approval_id, gen, status="REQUESTED")
        with pytest.raises(ValueError):
            await repo.write_result(ctx.approval_id, gen, status="RETRYABLE")


# --- the follow-up gate (design 7.5 items 6 (a) and 11) ----------------------------------------------------------------------------
async def pending_gates(db, task_id):
    from app.repositories.approval_repository import ApprovalRepository
    async with db.transaction() as s:
        return await ApprovalRepository(s).pending_for_task(task_id)


async def test_a_refused_alive_request_opens_one_new_project_dirty_gate_at_the_current_epoch_with_the_lineage(db):
    from app.services.risk_followup import RiskFollowUp
    from tests.integration_pg.test_risk_options import gates_for
    project, task = await make_project_task(db)
    owner = finished_process()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ident = capture_identity(proc.pid)
        alive_id = await add_row(db, task, pid=ident.pid, create_time=ident.create_time, runner=runner_process(owner))
        ctx = await setup(db, pt=(project, task), rows=[alive_id])
        w = worker(db, follow_up=RiskFollowUp(gates_for(db)))
        await w()
        assert (await request_of(db, ctx.approval_id)).status == "REFUSED_ALIVE"
        gates = await pending_gates(db, task.id)
        assert len(gates) == 1 and gates[0].target["reason_code"] == "PROJECT_DIRTY"
        assert gates[0].target["supersedes_approval_id"] == str(ctx.approval_id) and gates[0].target["dirty_epoch"] == 1
        assert await resumes(db, ctx.approval_id) == 0
    finally:
        proc.kill()
        proc.wait()


async def test_a_defensive_code_opens_the_existing_execution_uncertain_gate_and_never_fails_the_task(db):
    from app.services.risk_followup import RiskFollowUp
    from tests.integration_pg.test_risk_options import gates_for
    ctx = await setup(db)
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == ctx.task.id).values(project_id=None))   # TASK_PROJECT_MISMATCH
    await worker(db, follow_up=RiskFollowUp(gates_for(db)))()
    assert (await request_of(db, ctx.approval_id)).reason == "TASK_PROJECT_MISMATCH"
    gates = await pending_gates(db, ctx.task.id)
    assert len(gates) == 1 and gates[0].target["reason_code"] == P.RECOVERY_REASON_EXECUTION_UNCERTAIN
    assert gates[0].target["supersedes_approval_id"] == str(ctx.approval_id)
    async with db.transaction() as s:
        assert (await s.execute(text("select status from tasks where id = :t"), {"t": ctx.task.id})).scalar_one() == "WAITING_APPROVAL"


async def test_no_gate_is_opened_when_another_approval_is_pending_or_the_task_moved_on(db):
    from app.services.risk_followup import RiskFollowUp
    from tests.integration_pg.test_risk_options import gates_for
    ctx = await setup(db)
    async with db.transaction() as s:
        s.add(Approval(id=uuid.uuid4(), task_id=ctx.task.id, type="DESIGN", status="PENDING", target={}, scope="TASK",
                       request_payload={}, basis={}, checkpoint_step_key="PLAN", valid_until=datetime.now(UTC) + timedelta(days=1)))
    await worker(db, follow_up=RiskFollowUp(gates_for(db)))()
    assert (await request_of(db, ctx.approval_id)).reason == "GATE_ALREADY_PENDING"
    assert len(await pending_gates(db, ctx.task.id)) == 1                                      # only the pre-existing DESIGN gate
    moved = await setup(db, status="FAILED")
    await worker(db, follow_up=RiskFollowUp(gates_for(db)))()
    assert (await request_of(db, moved.approval_id)).reason == "TASK_NOT_WAITING_APPROVAL"
    assert await pending_gates(db, moved.task.id) == []


async def test_the_follow_up_is_idempotent_per_approval(db):
    from app.services.risk_followup import RiskFollowUp
    from tests.integration_pg.test_risk_options import gates_for
    ctx = await setup(db)
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == ctx.project.id).values(write_epoch=9))     # DIRTY_EPOCH_CHANGED
    follow = RiskFollowUp(gates_for(db))
    await worker(db, follow_up=follow)()
    first = await pending_gates(db, ctx.task.id)
    assert len(first) == 1 and first[0].target["dirty_epoch"] == 9
    async with db.transaction() as s:                                                           # a repeat finds the successor
        task = (await s.execute(text("select 1"))).scalar_one()
        from app.repositories.task_repository import TaskRepository as TR_
        t = await TR_(s).lock(ctx.task.id)
        req = await ProcessRiskRequestRepository(s).get(ctx.approval_id)
        await follow(s, t, req, "DIRTY_EPOCH_CHANGED")
    assert len(await pending_gates(db, ctx.task.id)) == 1


async def test_the_exception_path_at_the_attempt_limit_opens_the_follow_up_gate_and_never_before(db):
    from app.services.risk_followup import RiskFollowUp
    from tests.integration_pg.test_risk_options import gates_for
    ctx = await setup(db)
    w = worker(db, follow_up=RiskFollowUp(gates_for(db)))

    async def explode(self, *args, **kwargs):
        raise RuntimeError("the verification machinery failed")

    w._judge = explode.__get__(w)
    for attempt in range(1, P.PROCESS_RISK_MAX_ATTEMPTS):
        await w()
        req = await request_of(db, ctx.approval_id)
        assert (req.status, req.attempts) == ("RETRYABLE", attempt) and await pending_gates(db, ctx.task.id) == []
        async with db.transaction() as s:
            await s.execute(text("UPDATE process_risk_requests SET next_attempt_at = now() - interval '1 second' WHERE approval_id = :a"),
                            {"a": ctx.approval_id})
    await w()
    req = await request_of(db, ctx.approval_id)
    assert (req.status, req.reason) == ("NEEDS_REDECISION", "PROCESS_RECHECK_INCONCLUSIVE")
    gates = await pending_gates(db, ctx.task.id)
    assert len(gates) == 1 and gates[0].target["supersedes_approval_id"] == str(ctx.approval_id)
    assert gates[0].target["lineage_origin"] == "WORKER"
