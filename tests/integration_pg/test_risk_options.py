"""B2 phase 5b on the embedded PostgreSQL: the dynamic options of the PROJECT_DIRTY gate (priority table, stale gate, CLEAN, broken
link), the risk target scans and the ordered lock phase, and the decisions that create the request row."""
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update

import app.repositories.task_repository as TR
from app.models.tables import Approval, ApprovalDecision, ExecutionRequest, Project, Task
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.process_risk_repository import ProcessRiskRequestRepository
from app.repositories.task_repository import RiskLockFailure, TaskRepository
from app.services import dirty_gate_options as DGO
from app.services.human_gate_service import HumanGateService, IdempotencyConflict
from app.services.lease_service import LeaseService
from app.services.project_lock_service import ProjectLockService
from app.services.project_recovery_effect import OptionNotAvailable, ProjectDirtyRecoveryEffect
from app.services.recovery_effects import RecoveryEffects
from app.services.task_service import TaskService
from app.services.tracked_classifier import classify_row
from app.validation import policies as P
from app.validation.exceptions import ValidationFailed
from tests.helpers import make_task
from tests.integration_pg.test_orphan_sweeper import add_row
from tests.integration_pg.test_process_schema import make_project_task


def gates_for(db) -> HumanGateService:
    locks = ProjectLockService(db, LeaseService(db, "runner-x"))
    return HumanGateService(db, effects=RecoveryEffects({P.RECOVERY_REASON_PROJECT_DIRTY: ProjectDirtyRecoveryEffect(locks)}))


async def open_gate(db, gates, project, task, *, epoch=None, dirty_epoch=1, state="DIRTY_UNRESOLVED", link=True):
    """A PROJECT_DIRTY gate on `task` (WAITING_APPROVAL) of `project` at project epoch `epoch` (gate epoch `dirty_epoch`)."""
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(
            write_epoch=dirty_epoch if epoch is None else epoch, workspace_state=state))
        await s.execute(update(Task).where(Task.id == task.id).values(status="WAITING_APPROVAL", project_id=project.id if link else None))
        row = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
        approval = await gates.open_recovery(
            s, row, None, reason="dirty", checkpoint_step_key="DEVELOP", reason_code=P.RECOVERY_REASON_PROJECT_DIRTY,
            options=P.PROJECT_DIRTY_OPTIONS, target_extra={"project_id": str(project.id), "dirty_epoch": dirty_epoch})
        return approval.id


async def options_of(db, gates, approval_id):
    async with db.transaction() as s:
        approval = await ApprovalRepository(s).get(approval_id)
        return (await gates.current_options_for_approvals(s, [approval]))[approval_id]


def ids(result) -> list[str]:
    return sorted(result.offered_ids)


def hidden(result) -> dict[str, str]:
    return {h.option_id: h.reason_code for h in result.options_hidden}


async def scene(db, *, rows=2, **kw):
    project, task = await make_project_task(db)
    for _ in range(rows):
        await add_row(db, task, pid=4242)                       # pid without identity: unresolved, never provable
    gates = gates_for(db)
    return project, task, gates, await open_gate(db, gates, project, task, **kw)


async def insert_request(db, project, task, *, kind, status, epoch=1, targets, finished_ago=None, created_ago=0, reason=None):
    async with db.transaction() as s:
        approval = Approval(id=uuid.uuid4(), task_id=task.id, type="RECOVERY", status="DECIDED", target={}, scope="TASK",
                            request_payload={}, basis={}, checkpoint_step_key="DEVELOP", valid_until=datetime.now(UTC) + timedelta(days=1))
        s.add(approval)
        await s.flush()
        await ProcessRiskRequestRepository(s).insert(approval_id=approval.id, kind=kind, task_id=task.id, project_id=project.id,
                                                     dirty_epoch=epoch, target_execution_ids=targets, rationale="r")
        await s.execute(text(
            "UPDATE process_risk_requests SET status = :st, reason = :rs, created_at = now() - make_interval(secs => :ca), "
            "finished_at = CASE WHEN CAST(:fa AS double precision) IS NULL THEN NULL ELSE now() - make_interval(secs => CAST(:fa AS double precision)) END "
            "WHERE approval_id = :id"), {"st": status, "rs": reason, "ca": created_ago, "fa": finished_ago, "id": approval.id})
        return approval.id


async def u_ids(db, task):
    async with db.transaction() as s:
        return sorted(r.id for r in await TaskRepository(s).unresolved_for_task(task.id))


# --- the options computation ----------------------------------------------------------------------------------------------------
async def test_a_clean_project_offers_continue_and_abort_and_reads_neither_u_nor_a(db, monkeypatch):
    project, task, gates, approval = await scene(db, state="CLEAN", dirty_epoch=1, epoch=7)      # stale epoch, CLEAN: as today

    async def boom(*a, **k):
        raise AssertionError("U or A was read on a CLEAN project")

    monkeypatch.setattr(DGO, "has_unresolved_or_anomalous_for_project", boom)
    result = await options_of(db, gates, approval)
    assert ids(result) == ["abort", "continue"] and result.options_hidden == []


async def test_a_broken_link_lists_every_option_as_hidden_with_the_link_reason_and_offers_none(db):
    project, task, gates, approval = await scene(db, link=False)
    result = await options_of(db, gates, approval)
    assert result.options == [] and hidden(result) == {o: "TASK_PROJECT_LINK_BROKEN" for o in DGO.ALL_OPTIONS}
    assert not any(h.retryable for h in result.options_hidden)


async def test_a_stale_gate_of_a_dirty_project_offers_refresh_gate_and_abort_and_omits_the_rest_without_a_scan(db, monkeypatch):
    project, task, gates, approval = await scene(db, epoch=5, dirty_epoch=1)

    async def boom(*a, **k):
        raise AssertionError("a stale gate reads no U and no A")

    monkeypatch.setattr(DGO, "has_unresolved_or_anomalous_for_project", boom)
    result = await options_of(db, gates, approval)
    assert ids(result) == ["abort", "refresh_gate"] and result.options_hidden == []             # omitted, not hidden


async def test_a_current_dirty_gate_with_unresolved_rows_offers_abort_and_the_risk_option_and_hides_continue(db):
    project, task, gates, approval = await scene(db)
    result = await options_of(db, gates, approval)
    assert ids(result) == ["abort", "accept_unproven_risk"]
    assert hidden(result) == {"continue": "UNRESOLVED_ROWS_PRESENT"}                           # recheck does not apply: not alive-blocked
    risk = next(o for o in result.options if o["id"] == "accept_unproven_risk")
    assert risk["risk"] == "HIGH" and len(result.candidates["accept_unproven_risk"]) == 2


async def test_with_nothing_unresolved_continue_is_offered_and_the_risk_option_has_nothing_to_accept(db):
    project, task, gates, approval = await scene(db, rows=0)
    result = await options_of(db, gates, approval)
    assert ids(result) == ["abort", "continue"] and hidden(result) == {"accept_unproven_risk": "NO_UNRESOLVED_ROWS"}


async def test_an_anomaly_hides_continue_with_the_anomaly_reason_even_when_u_is_empty(db):
    project, task, gates, approval = await scene(db, rows=0)
    await add_row(db, task, detail={"tracked": True, "v": 2, "proof": "NEVER_RAN"}, finished=True)
    result = await options_of(db, gates, approval)
    assert hidden(result)["continue"] == "TRACKED_ANOMALY_PRESENT" and "continue" not in ids(result)
    assert next(h for h in result.options_hidden if h.option_id == "continue").retryable is False


async def test_an_alive_blocked_project_hides_accept_with_alive_blocked_and_offers_recheck(db):
    project, task, gates, approval = await scene(db)
    rows = await u_ids(db, task)
    basis = await insert_request(db, project, task, kind="ACCEPT", status="REFUSED_ALIVE", targets=rows, finished_ago=5)
    result = await options_of(db, gates, approval)
    assert ids(result) == ["abort", "recheck_processes"]
    entry = next(h for h in result.options_hidden if h.option_id == "accept_unproven_risk")
    assert (entry.reason_code, entry.basis_request_id, entry.retryable) == ("ALIVE_BLOCKED", basis, True)
    assert result.candidates["recheck_processes"]


async def test_a_later_non_alive_observation_lifts_the_alive_block(db):
    project, task, gates, approval = await scene(db)
    rows = await u_ids(db, task)
    await insert_request(db, project, task, kind="ACCEPT", status="REFUSED_ALIVE", targets=rows, finished_ago=60)
    await insert_request(db, project, task, kind="RECHECK", status="NEEDS_REDECISION", targets=rows, finished_ago=5,
                         reason="NO_LIVE_PROCESS_UNPROVEN")
    assert "accept_unproven_risk" in ids(await options_of(db, gates, approval))


async def test_a_non_observation_redecision_never_lifts_the_alive_block(db):
    project, task, gates, approval = await scene(db)
    rows = await u_ids(db, task)
    await insert_request(db, project, task, kind="ACCEPT", status="REFUSED_ALIVE", targets=rows, finished_ago=60)
    await insert_request(db, project, task, kind="RECHECK", status="NEEDS_REDECISION", targets=rows, finished_ago=5,
                         reason="DIRTY_EPOCH_CHANGED")
    assert "accept_unproven_risk" not in ids(await options_of(db, gates, approval))


async def test_active_recheck_outranks_cooldown_and_the_window(db):
    project, task, gates, approval = await scene(db)
    rows = await u_ids(db, task)
    await insert_request(db, project, task, kind="ACCEPT", status="REFUSED_ALIVE", targets=rows, finished_ago=5)
    for _ in range(P.PROCESS_RECHECK_MAX_PER_WINDOW):
        await insert_request(db, project, task, kind="RECHECK", status="REFUSED_ALIVE", targets=rows, finished_ago=1, epoch=1)
    active = await insert_request(db, project, task, kind="RECHECK", status="REQUESTED", targets=rows)
    result = await options_of(db, gates, approval)
    entry = next(h for h in result.options_hidden if h.option_id == "recheck_processes")
    assert (entry.reason_code, entry.basis_request_id, entry.next_available_at) == ("ACTIVE_RECHECK", active, None)


async def test_the_cooldown_hides_recheck_until_it_elapses_and_names_when(db):
    project, task, gates, approval = await scene(db)
    rows = await u_ids(db, task)
    await insert_request(db, project, task, kind="ACCEPT", status="REFUSED_ALIVE", targets=rows, finished_ago=500)
    latest = await insert_request(db, project, task, kind="RECHECK", status="REFUSED_ALIVE", targets=rows, finished_ago=10)
    entry = next(h for h in (await options_of(db, gates, approval)).options_hidden if h.option_id == "recheck_processes")
    assert (entry.reason_code, entry.basis_request_id, entry.retryable) == ("COOLDOWN", latest, True)
    assert abs((entry.next_available_at - datetime.now(UTC)).total_seconds() - (P.PROCESS_RECHECK_COOLDOWN_BASE_SECONDS - 10)) < 5
    async with db.transaction() as s:
        await s.execute(text("UPDATE process_risk_requests SET finished_at = now() - interval '2 hours' WHERE approval_id = :a"), {"a": latest})
    assert "recheck_processes" in ids(await options_of(db, gates, approval))                    # elapsed: offered again


async def test_the_rolling_window_hides_recheck_and_the_option_returns_after_the_window(db):
    project, task, gates, approval = await scene(db)
    rows = await u_ids(db, task)
    await insert_request(db, project, task, kind="ACCEPT", status="REFUSED_ALIVE", targets=rows, finished_ago=7200)
    oldest = None
    for i in range(P.PROCESS_RECHECK_MAX_PER_WINDOW):
        made = await insert_request(db, project, task, kind="RECHECK", status="REFUSED_ALIVE", targets=rows,
                                    finished_ago=3000, created_ago=3500 - i * 10)
        oldest = oldest or made
    entry = next(h for h in (await options_of(db, gates, approval)).options_hidden if h.option_id == "recheck_processes")
    assert (entry.reason_code, entry.basis_request_id) == ("RATE_LIMIT_WINDOW", oldest)
    assert entry.next_available_at > datetime.now(UTC)
    async with db.transaction() as s:
        await s.execute(text("UPDATE process_risk_requests SET created_at = created_at - interval '2 hours' WHERE approval_id = :a"), {"a": oldest})
    assert "recheck_processes" in ids(await options_of(db, gates, approval))


async def test_the_scan_states_map_to_the_fixed_hide_reasons(db, monkeypatch):
    project, task, gates, approval = await scene(db, rows=3)
    real = DGO.classify_row
    not_unresolved = lambda *a: SimpleNamespace(is_unresolved=False)                            # noqa: E731

    monkeypatch.setattr(DGO, "classify_row", not_unresolved)
    assert hidden(await options_of(db, gates, approval))["accept_unproven_risk"] == "NO_ELIGIBLE_TARGETS"
    monkeypatch.setattr(TR, "P_CANDIDATE_MAX_SCANNED_ROWS", 2)
    monkeypatch.setattr(TR, "P_CANDIDATE_PAGE_ROWS", 1)
    entry = next(h for h in (await options_of(db, gates, approval)).options_hidden if h.option_id == "accept_unproven_risk")
    assert (entry.reason_code, entry.retryable) == ("TARGET_SCAN_ROW_LIMIT_REACHED", True)
    monkeypatch.setattr(TR, "P_CANDIDATE_MAX_SCANNED_ROWS", 1000)
    monkeypatch.setattr(TR, "P_CANDIDATE_SCAN_MAX_SECONDS", 0.0)
    assert hidden(await options_of(db, gates, approval))["accept_unproven_risk"] == "TARGET_SCAN_TIME_LIMIT_REACHED"
    monkeypatch.setattr(TR, "P_CANDIDATE_SCAN_MAX_SECONDS", 5)
    monkeypatch.setattr(DGO, "classify_row", real)

    class Timeout(Exception):
        orig = SimpleNamespace(sqlstate="57014")

    class Transient(Exception):
        orig = SimpleNamespace(sqlstate="08006")

    for error, reason, retryable in ((Timeout(), "TARGET_PAGE_TIMEOUT", True), (Transient(), "TARGET_QUERY_FAILED", True),
                                     (ValueError("contract"), "TARGET_CLASSIFICATION_ERROR", False)):
        def boom(*args, error=error):
            raise error

        monkeypatch.setattr(DGO, "classify_row", boom)
        entry = next(h for h in (await options_of(db, gates, approval)).options_hidden if h.option_id == "accept_unproven_risk")
        assert (entry.reason_code, entry.retryable) == (reason, retryable)
        assert entry.next_available_at is None and entry.basis_request_id is None
        monkeypatch.undo()
        monkeypatch.setattr(DGO, "classify_row", real)


async def test_decided_and_expired_dynamic_gates_get_empty_lists_and_static_gates_get_none(db):
    project, task, gates, approval = await scene(db)
    async with db.transaction() as s:
        design = Approval(id=uuid.uuid4(), task_id=task.id, type="DESIGN", status="PENDING", target={}, scope="TASK",
                          request_payload={"options": [{"id": "ok"}]}, basis={}, checkpoint_step_key="PLAN",
                          valid_until=datetime.now(UTC) + timedelta(days=1))
        s.add(design)
        await s.execute(update(Approval).where(Approval.id == approval).values(valid_until=datetime.now(UTC) - timedelta(seconds=1)))
    async with db.transaction() as s:
        got = await gates.current_options_for_approvals(s, await ApprovalRepository(s).for_task(task.id))
    assert got[design.id] is None
    assert got[approval].options == [] and got[approval].options_hidden == []


async def test_the_batch_reads_stage_one_once_per_project_and_not_per_approval(db, monkeypatch):
    project, task, gates, a1 = await scene(db)
    task2 = await make_task(db)
    a2 = await open_gate(db, gates, project, task2)
    calls = []
    real = DGO.has_unresolved_or_anomalous_for_project

    async def counting(session, project_id):
        calls.append(project_id)
        return await real(session, project_id)

    monkeypatch.setattr(DGO, "has_unresolved_or_anomalous_for_project", counting)
    async with db.transaction() as s:
        got = await gates.current_options_for_approvals(s, [await ApprovalRepository(s).get(a1), await ApprovalRepository(s).get(a2)])
    assert set(got) == {a1, a2} and calls == [project.id]


# --- the scans and the lock phase ---------------------------------------------------------------------------------------------------
async def test_risk_batch_targets_takes_the_resumed_tasks_rows_first_then_the_rest_each_ascending_up_to_the_limit(db):
    project, task = await make_project_task(db)
    other = await make_task(db)
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == other.id).values(project_id=project.id))
    first = [await add_row(db, other, pid=1) for _ in range(2)]
    own = [await add_row(db, task, pid=1) for _ in range(2)]
    later = [await add_row(db, other, pid=1) for _ in range(2)]
    async with db.transaction() as s:
        scan = await TaskRepository(s).risk_batch_targets(project.id, task.id, 5, classify_row)
    assert scan.state == "TARGETS_READY"
    assert list(scan.ids) == sorted(own) + sorted(first + later)[:3]               # own rows first, then the others ascending
    async with db.transaction() as s:
        assert len((await TaskRepository(s).risk_batch_targets(project.id, task.id, 100, classify_row)).ids) == 6
        assert (await TaskRepository(s).risk_batch_targets(project.id, task.id, 1, classify_row)).ids == (sorted(own)[0],)


async def test_risk_batch_targets_restores_the_statement_timeout_of_the_callers_transaction(db):
    project, task = await make_project_task(db)
    await add_row(db, task, pid=1)
    async with db.transaction() as s:
        await s.execute(text("select set_config('statement_timeout', '7000', true)"))
        await TaskRepository(s).risk_batch_targets(project.id, task.id, 5, classify_row)
        assert (await s.execute(text("select current_setting('statement_timeout')"))).scalar_one() == "7s"


async def test_lock_risk_targets_sorts_dedupes_drops_rows_that_left_u_and_restores_the_timeouts(db):
    project, task = await make_project_task(db)
    a, b, c = sorted([await add_row(db, task, pid=1) for _ in range(3)])
    foreign_project, foreign_task = await make_project_task(db)
    foreign = await add_row(db, foreign_task, pid=1)
    async with db.transaction() as s:
        await s.execute(text("UPDATE executions SET detail = detail || '{\"proof\": \"NEVER_RAN\"}'::jsonb WHERE id = :i"), {"i": b})
    async with db.transaction() as s:
        await s.execute(text("select set_config('statement_timeout', '9000', true)"))
        rows = await TaskRepository(s).lock_risk_targets(project.id, [c, a, a, b, foreign])
        assert [r.id for r in rows] == [a, c]                                      # sorted, de-duplicated, b resolved, foreign dropped
        assert (await s.execute(text("select current_setting('statement_timeout'), current_setting('lock_timeout')"))).one() == ("9s", "0")
        assert await TaskRepository(s).lock_risk_targets(project.id, []) == []


async def test_a_row_lock_held_by_another_writer_ends_the_lock_phase_with_a_retryable_query_failure(db):
    project, task = await make_project_task(db)
    row = await add_row(db, task, pid=1)
    async with db.transaction() as holder:
        await TaskRepository(holder).lock_executions_ordered([row])
        with pytest.raises(RiskLockFailure) as info:
            async with db.transaction() as s:
                await TaskRepository(s).lock_risk_targets(project.id, [row])
        assert (info.value.reason, info.value.internal) == ("TARGET_QUERY_FAILED", "TARGET_LOCK_TIMEOUT")


async def test_an_exhausted_lock_budget_runs_no_statement(db, monkeypatch):
    project, task = await make_project_task(db)
    row = await add_row(db, task, pid=1)
    monkeypatch.setattr(TR, "P_RISK_LOCK_SECONDS", 0.0)
    with pytest.raises(RiskLockFailure) as info:
        async with db.transaction() as s:
            await TaskRepository(s).lock_risk_targets(project.id, [row])
    assert (info.value.reason, info.value.internal) == ("TARGET_PAGE_TIMEOUT", "TARGET_LOCK_BUDGET_EXHAUSTED")


# --- decisions ------------------------------------------------------------------------------------------------------------------
async def decide(gates, approval, option, *, rationale="the process is gone, I checked it", requested_by=None, key="k1", decision="APPROVED"):
    return await gates.decide(approval, decision=decision, selected_option=option, rationale=rationale,
                              idempotency_key=key, requested_by=requested_by)


async def requests_of(db, approval):
    async with db.transaction() as s:
        return await ProcessRiskRequestRepository(s).get(approval)


async def count(db, sql, **params) -> int:
    async with db.transaction() as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def test_accepting_risk_stores_one_request_with_the_sorted_final_snapshot_and_no_resume_and_no_execution_write(db):
    project, task, gates, approval = await scene(db, rows=3)
    rows = await u_ids(db, task)
    before = await count(db, "select count(*) from executions where detail ? 'disposition' or detail ? 'proof'")
    outcome = await decide(gates, approval, "accept_unproven_risk", requested_by="op@example")
    assert outcome.created
    request = await requests_of(db, approval)
    assert (request.kind, request.status, request.dirty_epoch, request.requested_by) == ("ACCEPT", "REQUESTED", 1, "op@example")
    assert list(request.target_execution_ids) == sorted(rows) and request.rationale == "the process is gone, I checked it"
    assert await count(db, "select count(*) from execution_requests where approval_id = :a", a=approval) == 0
    assert await count(db, "select count(*) from executions where detail ? 'disposition' or detail ? 'proof'") == before
    async with db.transaction() as s:
        assert (await ApprovalRepository(s).get(approval)).status == "DECIDED"


async def test_a_replay_is_idempotent_and_a_different_requested_by_is_an_idempotency_conflict(db):
    project, task, gates, approval = await scene(db)
    await decide(gates, approval, "accept_unproven_risk", requested_by="op")
    assert (await decide(gates, approval, "accept_unproven_risk", requested_by="op")).created is False
    with pytest.raises(IdempotencyConflict):
        await decide(gates, approval, "accept_unproven_risk", requested_by="someone-else")
    assert await count(db, "select count(*) from process_risk_requests where approval_id = :a", a=approval) == 1


async def test_a_recheck_while_not_alive_blocked_is_refused_with_the_option_not_available_conflict_and_nothing_is_written(db):
    project, task, gates, approval = await scene(db)
    with pytest.raises(OptionNotAvailable) as info:
        await decide(gates, approval, "recheck_processes")
    assert info.value.status_code == 409 and info.value.code == "OPTION_NOT_AVAILABLE"
    async with db.transaction() as s:
        assert (await ApprovalRepository(s).get(approval)).status == "PENDING" and await ApprovalRepository(s).get_decision(approval) is None
    assert await requests_of(db, approval) is None


async def test_continue_with_unresolved_rows_and_a_bogus_option_are_409_with_the_reason_and_the_gate_stays_pending(db):
    project, task, gates, approval = await scene(db)
    with pytest.raises(OptionNotAvailable) as info:
        await decide(gates, approval, "continue")
    assert (info.value.reason_code, info.value.retryable) == ("UNRESOLVED_ROWS_PRESENT", True)
    with pytest.raises(OptionNotAvailable) as info:
        await decide(gates, approval, "frobnicate")
    assert (info.value.reason_code, info.value.retryable) == (None, False)
    async with db.transaction() as s:
        assert (await ApprovalRepository(s).get(approval)).status == "PENDING"


async def test_accept_on_an_alive_blocked_project_is_refused_with_alive_blocked(db):
    project, task, gates, approval = await scene(db)
    rows = await u_ids(db, task)
    basis = await insert_request(db, project, task, kind="ACCEPT", status="REFUSED_ALIVE", targets=rows, finished_ago=5)
    with pytest.raises(OptionNotAvailable) as info:
        await decide(gates, approval, "accept_unproven_risk")
    assert info.value.reason_code == "ALIVE_BLOCKED"
    request = await decide(gates, approval, "recheck_processes", key="k2")
    assert request.created and (await requests_of(db, approval)).kind == "RECHECK"
    assert basis != approval


async def test_the_risk_options_need_a_rationale_and_requested_by_only_goes_with_them_422_before_any_transaction(db):
    project, task, gates, approval = await scene(db)
    with pytest.raises(ValidationFailed):
        await decide(gates, approval, "accept_unproven_risk", rationale="   ")
    with pytest.raises(ValidationFailed):
        await decide(gates, approval, "abort", requested_by="op")
    async with db.transaction() as s:
        assert (await ApprovalRepository(s).get(approval)).status == "PENDING"


async def test_rows_settled_between_the_options_and_the_lock_empty_the_snapshot_and_the_decision_is_refused(db, monkeypatch):
    project, task, gates, approval = await scene(db)
    real = ProjectDirtyRecoveryEffect.allowed_options

    async def then_settle(self, session, approval_, task_, **kw):
        result = await real(self, session, approval_, task_, **kw)
        async with db.transaction() as other:                                   # another writer resolves every row meanwhile
            await other.execute(text("UPDATE executions SET detail = detail || '{\"proof\": \"NEVER_RAN\"}'::jsonb"))
        return result

    monkeypatch.setattr(ProjectDirtyRecoveryEffect, "allowed_options", then_settle)
    with pytest.raises(OptionNotAvailable) as info:
        await decide(gates, approval, "accept_unproven_risk")
    assert (info.value.reason_code, info.value.retryable) == (None, True)
    assert await requests_of(db, approval) is None
    async with db.transaction() as s:
        assert (await ApprovalRepository(s).get(approval)).status == "PENDING"


async def test_a_lock_phase_failure_rolls_the_whole_decision_back_with_the_mapped_reason(db, monkeypatch):
    project, task, gates, approval = await scene(db)

    async def failing(self, project_id, target_ids, clock=None):
        raise RiskLockFailure("TARGET_PAGE_TIMEOUT", "TARGET_LOCK_BUDGET_EXHAUSTED")

    monkeypatch.setattr(TaskRepository, "lock_risk_targets", failing)
    with pytest.raises(OptionNotAvailable) as info:
        await decide(gates, approval, "accept_unproven_risk")
    assert (info.value.reason_code, info.value.retryable) == ("TARGET_PAGE_TIMEOUT", True)
    async with db.transaction() as s:
        assert (await ApprovalRepository(s).get(approval)).status == "PENDING" and await ApprovalRepository(s).get_decision(approval) is None


async def test_refresh_gate_opens_one_successor_at_the_current_epoch_without_resume_or_baseline(db):
    project, task, gates, approval = await scene(db, epoch=5, dirty_epoch=1)
    outcome = await decide(gates, approval, "refresh_gate", rationale=None)
    assert outcome.created
    async with db.transaction() as s:
        old = await ApprovalRepository(s).get(approval)
        successor = await ApprovalRepository(s).successor_of(approval)
        assert old.status == "DECIDED" and successor.status == "PENDING"
        assert successor.target["dirty_epoch"] == 5 and successor.target["supersedes_approval_id"] == str(approval)
        assert (await s.execute(select(Task.status).where(Task.id == task.id))).scalar_one() == "WAITING_APPROVAL"
        proj = (await s.execute(select(Project).where(Project.id == project.id))).scalar_one()
        assert proj.workspace_state == "DIRTY_UNRESOLVED" and proj.baseline_fingerprint is None
    assert await count(db, "select count(*) from execution_requests where task_id = :t", t=task.id) == 0
    assert "refresh_gate" not in ids(await options_of(db, gates, successor.id))             # a current gate no longer offers it


async def test_continue_on_a_stale_gate_of_a_dirty_project_is_not_available(db):
    project, task, gates, approval = await scene(db, rows=0, epoch=5, dirty_epoch=1)
    with pytest.raises(OptionNotAvailable):
        await decide(gates, approval, "continue")


async def test_on_a_clean_project_the_dirty_only_options_are_refused(db):
    project, task, gates, approval = await scene(db, state="CLEAN")
    for option in ("accept_unproven_risk", "recheck_processes", "refresh_gate"):
        with pytest.raises(OptionNotAvailable):
            await decide(gates, approval, option, key="k-" + option)


# --- the read model ---------------------------------------------------------------------------------------------------------------
async def test_the_approval_view_carries_options_hidden_and_the_operator_request_and_is_static_without_a_provider(db):
    project, task, gates, approval = await scene(db)
    with_provider = TaskService(db, None, options_provider=gates.current_options_for_approvals)
    view = await with_provider.get_approval(approval)
    assert [o["id"] for o in view.options] == ["abort", "accept_unproven_risk"] or sorted(o["id"] for o in view.options) == ["abort", "accept_unproven_risk"]
    assert [h.reason_code for h in view.options_hidden] == ["UNRESOLVED_ROWS_PRESENT"] and view.operator_request is None
    await decide(gates, approval, "accept_unproven_risk", requested_by="op")
    view = await with_provider.get_approval(approval)
    assert view.options == [] and view.operator_request["status"] == "REQUESTED" and view.operator_request["target_count"] == 2
    assert view.operator_request["retryable"] is True and view.operator_request["kind"] == "ACCEPT"
    static = await TaskService(db, None).get_approval(approval)
    assert (static.options, static.options_hidden) == (None, None) and static.operator_request is not None
    listed = await with_provider.approvals(task.id)
    assert [v.id for v in listed] == [approval]


# --- the bounded keyset discovery of `continue` (design 7.5 item 2, rank 9 reasons) ------------------------------------------------------------
async def continue_entry(db, gates, approval):
    return next(h for h in (await options_of(db, gates, approval)).options_hidden if h.option_id == "continue")


async def test_continue_discovery_reads_the_whole_u_set_and_reports_unresolved_rows_present(db):
    project, task, gates, approval = await scene(db, rows=3)
    entry = await continue_entry(db, gates, approval)
    assert (entry.reason_code, entry.retryable) == ("UNRESOLVED_ROWS_PRESENT", True)


async def test_continue_discovery_stops_at_a_decode_error_row_and_names_the_anomaly(db, monkeypatch):
    project, task, gates, approval = await scene(db, rows=3)
    real = DGO.classify_row
    bad = sorted(await u_ids(db, task))[1]

    def fake(row_id, pid, finished_at, detail_text):
        if row_id == bad:
            return SimpleNamespace(is_anomaly=True, is_unresolved=False, sub_reason="decode_error")
        return real(row_id, pid, finished_at, detail_text)

    monkeypatch.setattr(DGO, "classify_row", fake)
    entry = await continue_entry(db, gates, approval)
    assert (entry.reason_code, entry.retryable) == ("TRACKED_ANOMALY_PRESENT", False)


async def test_continue_discovery_maps_caps_and_failures_to_the_fixed_reasons(db, monkeypatch):
    project, task, gates, approval = await scene(db, rows=3)
    monkeypatch.setattr(TR, "P_CANDIDATE_MAX_SCANNED_ROWS", 2)
    monkeypatch.setattr(TR, "P_CANDIDATE_PAGE_ROWS", 1)
    assert (await continue_entry(db, gates, approval)).reason_code == "TARGET_SCAN_ROW_LIMIT_REACHED"
    monkeypatch.setattr(TR, "P_CANDIDATE_MAX_SCANNED_ROWS", 1000)
    monkeypatch.setattr(TR, "P_CANDIDATE_SCAN_MAX_SECONDS", 0.0)
    assert (await continue_entry(db, gates, approval)).reason_code == "TARGET_SCAN_TIME_LIMIT_REACHED"
    monkeypatch.setattr(TR, "P_CANDIDATE_SCAN_MAX_SECONDS", 5)

    class Timeout(Exception):
        orig = SimpleNamespace(sqlstate="57014")

    class Transient(Exception):
        orig = SimpleNamespace(sqlstate="08006")

    for error, reason, retryable in ((Timeout(), "TARGET_PAGE_TIMEOUT", True), (Transient(), "TARGET_QUERY_FAILED", True),
                                     (ValueError("contract"), "TARGET_CLASSIFICATION_ERROR", False)):
        def boom(*args, error=error):
            raise error

        monkeypatch.setattr(DGO, "classify_row", boom)
        entry = await continue_entry(db, gates, approval)
        assert (entry.reason_code, entry.retryable) == (reason, retryable)
        monkeypatch.setattr(DGO, "classify_row", DGO.classify_row)
        monkeypatch.undo()


async def test_a_real_statement_timeout_in_a_scan_is_rolled_back_to_its_savepoint_and_the_options_still_compute(db):
    project, task, gates, approval = await scene(db, rows=2)
    from app.services.dirty_gate_options import _scan_in_savepoint

    class Failing:
        state = "QUERY_FAILED"

    async def abort_the_transaction(session):
        try:
            await session.execute(text("select pg_sleep(1)"), execution_options={})
        except Exception:
            pass
        return Failing()

    async with db.transaction() as s:
        await s.execute(text("select set_config('statement_timeout', '50', true)"))
        result = await _scan_in_savepoint(s, abort_the_transaction(s))                 # the sleep exceeds 50 ms: 57014 aborts the txn
        assert result.state == "QUERY_FAILED"
        await s.execute(text("select set_config('statement_timeout', '0', true)"))
        assert (await s.execute(text("select 1"))).scalar_one() == 1                    # usable again: no 25P02
