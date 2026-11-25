"""A pid-less OPEN tracked row that records an unproven spawned process (`SPAWNED_PROCESS_UNPROVEN`): what the operator can do.

A valid `risk_acceptance` record names the row's pid, so a target set with any pid-less row cannot be accepted: the options
provider hides `accept_unproven_risk` (`TARGET_WITHOUT_PID`) for the whole set, and a direct submit (a stale client) is refused by
the decision-time re-check with 409 `OPTION_NOT_AVAILABLE` and NOTHING written. `abort` still works. There is still no supported
resolution path for such a row (the project stays DIRTY_UNRESOLVED).
"""
import json

import pytest
from sqlalchemy import select

from app.models.tables import ExecutionRequest, Project, Task
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.task_repository import TaskRepository
from app.services.lease_service import TrackedVerdict
from app.services.process_risk_worker import ProcessRiskWorker
from app.services.project_recovery_effect import OptionNotAvailable
from app.services.tracked_classifier import RowClass
from tests.integration_pg.test_orphan_sweeper import add_row, klass, section, tracked
from tests.integration_pg.test_process_schema import make_project_task
from tests.integration_pg.test_risk_options import decide, gates_for, hidden, ids, open_gate, options_of, requests_of

UNPROVEN = {"tracked": True, "v": 1, "lifecycle": "TERMINATION_UNCONFIRMED", "tree": "NOT_EMPTY",
            "runner_process": {"host": "h", "pid": 1, "create_time": 1.0}}


async def snapshot(db, task, project, approval, row_ids):
    """Everything an unavailable accept must leave untouched, read in one go."""
    async with db.transaction() as s:
        rows = {}
        for i in row_ids:
            r = await TaskRepository(s).tracked_row(i)
            rows[i] = (r.finished_at, r.detail_text, r.pid)
        exec_requests = (await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task.id))).scalars().all()
        proj = (await s.execute(select(Project).where(Project.id == project.id))).scalar_one()
        task_row = (await s.execute(select(Task).where(Task.id == task.id))).scalar_one()
        approval_row = await ApprovalRepository(s).get(approval)
        decision = await ApprovalRepository(s).get_decision(approval)
        return {"rows": rows, "exec_requests": [(e.kind, e.status) for e in exec_requests],
                "project": (proj.workspace_state, proj.write_epoch), "task": task_row.status,
                "approval": approval_row.status, "decision": decision}


async def assert_refused_and_untouched(db, gates, task, project, approval, row_ids):
    before = await snapshot(db, task, project, approval, row_ids)
    with pytest.raises(OptionNotAvailable) as info:
        await decide(gates, approval, "accept_unproven_risk", requested_by="op@example")
    assert info.value.status_code == 409 and info.value.code == "OPTION_NOT_AVAILABLE"
    assert info.value.details["reason_code"] == "TARGET_WITHOUT_PID" and info.value.details["retryable"] is False
    assert await requests_of(db, approval) is None                              # no process_risk_requests row at all
    after = await snapshot(db, task, project, approval, row_ids)
    assert after == before
    assert after["exec_requests"] == [] and after["decision"] is None            # NO RESUME, no decision
    assert after["project"][0] == "DIRTY_UNRESOLVED" and after["approval"] == "PENDING"
    for finished_at, detail_text, _pid in after["rows"].values():
        assert finished_at is None and "risk_acceptance" not in detail_text      # still open, no acceptance record


async def test_a_single_pid_less_target_hides_the_accept_option_and_a_direct_submit_is_refused_with_nothing_written(db):
    project, task = await make_project_task(db)
    row_id = await add_row(db, task, detail=dict(UNPROVEN))
    gates = gates_for(db)
    approval = await open_gate(db, gates, project, task)
    shown = await options_of(db, gates, approval)
    assert "accept_unproven_risk" not in ids(shown) and hidden(shown)["accept_unproven_risk"] == "TARGET_WITHOUT_PID"
    assert shown.hidden_for("accept_unproven_risk").retryable is False
    assert "continue" in hidden(shown) and "abort" in ids(shown)
    await assert_refused_and_untouched(db, gates, task, project, approval, [row_id])      # the stale client


async def test_a_mixed_target_set_hides_accept_as_a_whole_and_never_partially_accepts_the_pid_row(db):
    project, task = await make_project_task(db)
    pid_row = await add_row(db, task, pid=4242)                                 # unresolved, with a pid
    pidless = await add_row(db, task, detail=dict(UNPROVEN))
    gates = gates_for(db)
    approval = await open_gate(db, gates, project, task)
    shown = await options_of(db, gates, approval)
    assert "accept_unproven_risk" not in ids(shown) and hidden(shown)["accept_unproven_risk"] == "TARGET_WITHOUT_PID"
    await assert_refused_and_untouched(db, gates, task, project, approval, [pid_row, pidless])
    pid_state = await tracked(db, pid_row)
    assert pid_state.finished_at is None and "risk_acceptance" not in pid_state.detail_text


async def test_a_stale_client_that_saw_accept_offered_is_refused_when_a_pid_less_target_appears_afterwards(db):
    project, task = await make_project_task(db)
    pid_row = await add_row(db, task, pid=4242)
    gates = gates_for(db)
    approval = await open_gate(db, gates, project, task)
    assert "accept_unproven_risk" in ids(await options_of(db, gates, approval))     # what the client saw
    pidless = await add_row(db, task, detail=dict(UNPROVEN))                        # then the target set changes
    await assert_refused_and_untouched(db, gates, task, project, approval, [pid_row, pidless])
    assert hidden(await options_of(db, gates, approval))["accept_unproven_risk"] == "TARGET_WITHOUT_PID"


async def test_a_pid_bearing_target_is_still_accepted_resolved_risk_applied_and_resumed(db):
    project, task = await make_project_task(db)
    row_id = await add_row(db, task, pid=4242, create_time=1.0)
    gates = gates_for(db)
    approval = await open_gate(db, gates, project, task)
    assert "accept_unproven_risk" in ids(await options_of(db, gates, approval))
    assert await decide(gates, approval, "accept_unproven_risk", requested_by="op@example") is not None
    assert await requests_of(db, approval) is not None
    async with db.transaction() as s:
        assert (await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task.id))).first() is None   # RESUME only when APPLIED
    worker = ProcessRiskWorker(db, section, tracked_verdict=lambda stale: TrackedVerdict(False, reason="IDENTITY_RECORD_MISSING"))
    assert await worker() == 1
    assert (await requests_of(db, approval)).status == "APPLIED"
    assert await klass(db, row_id) is RowClass.RESOLVED_RISK
    assert json.loads((await tracked(db, row_id)).detail_text)["risk_acceptance"]["pid"] == 4242
    async with db.transaction() as s:
        kinds = [e.kind for e in (await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task.id))).scalars()]
    assert kinds == ["RESUME"]


async def test_abort_still_works_for_a_gate_with_a_pid_less_row_and_leaves_the_project_dirty(db):
    project, task = await make_project_task(db)
    row_id = await add_row(db, task, detail=dict(UNPROVEN))
    gates = gates_for(db)
    approval = await open_gate(db, gates, project, task)
    assert "abort" in ids(await options_of(db, gates, approval))
    assert await decide(gates, approval, "abort") is not None
    after = await snapshot(db, task, project, approval, [row_id])
    assert after["task"] == "FAILED" and after["approval"] == "DECIDED" and after["exec_requests"] == []
    assert after["project"][0] == "DIRTY_UNRESOLVED"                            # abort fails the Task; it does not clean the project
    assert after["rows"][row_id][0] is None and await requests_of(db, approval) is None
