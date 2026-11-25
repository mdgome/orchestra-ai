"""B2 phase 3 on the embedded PostgreSQL: the Python classifier against the SQL predicates, the common project-wide entry point
`has_unresolved_or_anomalous_for_project`, its use in both release paths (H7) and the write-owner acquisition guard (C5).
Real database, no mocks of the SQL."""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text, update

from app.models.states import TaskState
from app.models.tables import Task, TaskBaseline
from app.repositories import tracked_sql as S
from app.repositories.task_repository import TaskRepository
from app.services.project_lock_service import StepGate
from app.services.project_terminal import (
    FingerprintProof, assert_no_unresolved_or_anomalous_tracked, has_unresolved_or_anomalous_for_project, release_on_terminal,
)
from app.services.tracked_classifier import RowClass, classify_row
from app.validation import policies as P
from app.validation.exceptions import Conflict
from tests.harness import Harness
from tests.helpers import make_task
from tests.integration_pg.test_process_schema import (
    ANOMALY, LEGACY, RESOLVED, UNRESOLVED, insert_execution, load_cases, make_project_task, tracked_cases,
)
from tests.integration_pg.test_project_lock import fail_with, make_owner, make_project, new_task, project_row

PID = 4242


async def second_task(db, project) -> Task:
    task = await make_task(db)
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == task.id).values(project_id=project.id))
    return task


async def block_of(db, project_id):
    async with db.transaction() as s:
        return await has_unresolved_or_anomalous_for_project(s, project_id)


# --- classifier parity ----------------------------------------------------------------------------------------------------
async def test_the_python_classifier_equals_the_sql_predicates_and_the_anomaly_case_on_every_matrix_row(db):
    """Python classifier = SQL `<U>` / `<RESOLVED_VALID>` / `<ANOMALY>` / `<ANOMALY_KIND>` on the whole fixture matrix of
    §7.4/§13 (the same matrix the SQL partition test uses), through the real read path: `detail::text`, strict decode."""
    _, task = await make_project_task(db)
    expected = await load_cases(db, task.id)
    async with db.transaction() as s:
        rows = (await s.execute(text(
            f"SELECT {S.TRACKED_ROW_COLUMNS_SQL}, ({S.U_SQL}) AS in_u, ({S.RESOLVED_VALID_SQL}) AS in_resolved, "
            f"({S.ANOMALY_SQL}) AS in_a, CASE WHEN ({S.ANOMALY_SQL}) THEN {S.ANOMALY_KIND_SQL} END AS kind "
            "FROM executions WHERE task_id = :t"), {"t": task.id})).all()
    assert len(rows) == len(expected) > 120
    mismatches = []
    for row in rows:
        row_id, _, pid, _, finished_at, detail_text, in_u, in_resolved, in_a, kind = row
        got = classify_row(row_id, pid, finished_at, detail_text)
        want_class = {LEGACY: RowClass.LEGACY, UNRESOLVED: RowClass.UNRESOLVED, ANOMALY: RowClass.ANOMALY,
                      RESOLVED: None}[expected[row_id][1]]
        if expected[row_id][1] == RESOLVED:
            ok = got.row_class in (RowClass.RESOLVED_PROVEN, RowClass.RESOLVED_RISK) and bool(in_resolved)
        else:
            ok = got.row_class is want_class
        if got.row_class is RowClass.UNRESOLVED:
            ok = ok and in_u is True
        if got.row_class is RowClass.ANOMALY:
            ok = ok and in_a is True and got.anomaly_kind == kind
        else:
            ok = ok and not in_a and got.anomaly_kind is None
        if not ok:
            mismatches.append((expected[row_id][0], got, in_u, in_resolved, in_a, kind))
    assert not mismatches, mismatches[:5]


async def test_every_matrix_anomaly_has_the_kind_the_matrix_names_in_python(db):
    wanted = {label: kind for label, _, klass, _, kind in tracked_cases(uuid.UUID(int=0)) if klass == ANOMALY}
    for label, detail, klass, finished, kind in tracked_cases(uuid.UUID(int=3)):
        if klass != ANOMALY:
            continue
        got = classify_row(uuid.UUID(int=3), PID, datetime.now(UTC) if finished else None, detail)
        assert got.is_anomaly and got.anomaly_kind == kind == wanted[label], (label, got)
        assert got.sub_reason is None or isinstance(got.sub_reason, str)


# --- the common project-wide entry point ---------------------------------------------------------------------------------------
async def test_the_entry_point_is_project_wide_and_sees_unresolved_and_anomalous_rows_of_every_task(db):
    project, task = await make_project_task(db)
    other = await second_task(db, project)
    stranger_project, stranger_task = await make_project_task(db)
    assert not (await block_of(db, project.id)).blocked
    unresolved = await insert_execution(db, other.id, '{"tracked": true, "v": 1}', finished=False)
    bad_marker = await insert_execution(db, task.id, '{"tracked": "true"}')
    await insert_execution(db, task.id, '{"resumed": true}')                             # legacy: invisible
    await insert_execution(db, task.id, '{"tracked": true, "v": 1, "proof": "JOB_EMPTY"}')  # resolved: invisible
    await insert_execution(db, stranger_task.id, '{"tracked": true}', finished=False)   # another project
    block = await block_of(db, project.id)
    assert block.unresolved_ids == (unresolved,) and block.blocked and block.reason_code == "UNRESOLVED_TRACKED_EXECUTION"
    assert [(a.execution_id, a.task_id, a.project_id, a.anomaly_kind, a.sub_reason) for a in block.anomalies] == [
        (bad_marker, task.id, project.id, "OTHER_SCHEMA_INVALID", "tracked_type")]
    assert (await block_of(db, stranger_project.id)).unresolved_ids != ()
    # only anomalies: the reason code is the anomaly one
    async with db.transaction() as s:
        await s.execute(text("UPDATE executions SET finished_at = now(), detail = '{\"tracked\": true, \"proof\": \"JOB_EMPTY\", \"v\": 1}'"
                             " WHERE id = :i"), {"i": unresolved})
    assert (await block_of(db, project.id)).reason_code == "TRACKED_EXECUTION_ANOMALY"


async def test_the_entry_point_refuses_none_and_is_bounded(db):
    async with db.transaction() as s:
        with pytest.raises(ValueError):
            await has_unresolved_or_anomalous_for_project(s, None)
    project, task = await make_project_task(db)
    cap = P.DIAGNOSTIC_MAX_ANOMALIES
    for _ in range(cap + 1):
        await insert_execution(db, task.id, '{"tracked": true}', finished=False)
        await insert_execution(db, task.id, '{"tracked": false}')
    block = await block_of(db, project.id)
    assert len(block.unresolved_ids) == len(block.anomalies) == cap
    assert block.unresolved_truncated and block.anomalies_truncated and not block.anomalies_total_is_exact
    assert block.anomalies_total == cap + 1


async def test_an_anomaly_item_holds_identifiers_and_fixed_codes_only_and_a_decode_failure_is_classified(db):
    project, task = await make_project_task(db)
    row_id = uuid.uuid4()
    async with db.transaction() as s:
        await s.execute(text("INSERT INTO executions (id, task_id, role, provider, outcome, started_at, finished_at, detail) "
                             "VALUES (:i, :t, 'TESTER', 'LOCAL', 'ERROR', now(), now(), CAST(:d AS jsonb))"),
                        {"i": row_id, "t": task.id, "d": '{"tracked": true, "v": 1, "proof": "BOGUS"}'})
    assert classify_row(row_id, None, None, '{"tracked": true, "v": NaN}').sub_reason == "decode_error"
    block = await block_of(db, project.id)
    assert [(a.anomaly_kind, a.sub_reason) for a in block.anomalies] == [("INVALID_PROOF", None)]
    assert not hasattr(block.anomalies[0], "detail")


async def test_the_continue_guard_is_a_plain_conflict_naming_the_rows(db):
    project, task = await make_project_task(db)
    async with db.transaction() as s:
        await assert_no_unresolved_or_anomalous_tracked(s, project.id)
    row = await insert_execution(db, task.id, '{"tracked": true}', finished=False)
    with pytest.raises(Conflict) as info:
        async with db.transaction() as s:
            await assert_no_unresolved_or_anomalous_tracked(s, project.id)
    assert info.value.details == {"reason_code": "UNRESOLVED_TRACKED_EXECUTION", "execution_ids": [str(row)]}


async def test_task_local_view_is_not_the_project_wide_set(db):
    project, task = await make_project_task(db)
    other = await second_task(db, project)
    mine = await insert_execution(db, task.id, '{"tracked": true}', finished=False)
    await insert_execution(db, other.id, '{"tracked": true}', finished=False)
    async with db.transaction() as s:
        assert [r.id for r in await TaskRepository(s).unresolved_for_task(task.id)] == [mine]


# --- H7: both release paths -----------------------------------------------------------------------------------------------------
async def test_release_on_terminal_never_cleans_while_any_task_of_the_project_has_an_unresolved_execution(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task, other = await new_task(h, project), await new_task(h, project, "another")
    await make_owner(h, project, task, root)
    await insert_execution(db, other.id, '{"tracked": true}', finished=False)        # ANOTHER task's open row
    await fail_with(h, task.id)                                                       # nothing differs: CLEAN before B2
    row = await project_row(h, project.id)
    assert row.write_owner_task_id is None and row.workspace_state == "DIRTY_UNRESOLVED"


async def test_release_on_terminal_ignores_a_valid_or_stale_proof_when_a_tracked_row_blocks(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)
    await insert_execution(db, task.id, '{"tracked": "yes"}')                        # an anomaly, not U
    stale = FingerprintProof(project.id, task.id, 0, "WORKSPACE_FINGERPRINT_V1:" + "0" * 64, "mode")
    async with db.transaction() as s:
        locked = await TaskRepository(s).lock(task.id)
        await release_on_terminal(s, locked, TaskState.COMPLETED, stale)             # no FingerprintProofStale
    row = await project_row(h, project.id)
    assert row.workspace_state == "DIRTY_UNRESOLVED" and row.write_owner_task_id is None
    assert row.baseline_fingerprint != stale.fingerprint


async def test_a_properly_resolved_or_legacy_row_does_not_block_the_release(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)
    await insert_execution(db, task.id, '{"tracked": true, "v": 1, "proof": "JOB_EMPTY"}')
    await insert_execution(db, task.id, '{"resumed": false}')
    await fail_with(h, task.id)
    assert (await project_row(h, project.id)).workspace_state == "CLEAN"


async def test_reconcile_applies_the_same_rule_whatever_the_proof_is(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)
    await insert_execution(db, task.id, '{"tracked": true}', finished=False)
    async with db.transaction() as s:                                                 # the crash window: owner terminal
        await s.execute(update(Task).where(Task.id == task.id).values(status="FAILED"))
    assert await h.locks.reconcile() == 1
    row = await project_row(h, project.id)
    assert row.write_owner_task_id is None and row.workspace_state == "DIRTY_UNRESOLVED"


# --- C5: the write-owner acquisition guard ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("detail,finished", [('{"tracked": true, "v": 1}', False), ('{"tracked": true}', True),
                                             ('{"tracked": "true"}', True), ('{"proof": "JOB_EMPTY"}', True)])
async def test_a_clean_ownerless_project_with_an_old_unresolved_or_anomalous_row_refuses_a_new_owner(db, tmp_path, detail, finished):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    old = await new_task(h, project, "the earlier task")
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == old.id).values(status="FAILED"))
    await insert_execution(db, old.id, detail, finished=finished)
    assert (await project_row(h, project.id)).workspace_state == "CLEAN"
    before_epoch = (await project_row(h, project.id)).write_epoch
    waiting = await new_task(h, project, "wants the lock now")
    assert await h.start(waiting.id) == ["WAITING_APPROVAL"]
    after = await project_row(h, project.id)
    assert after.write_owner_task_id is None and after.workspace_state == "DIRTY_UNRESOLVED"       # mark_dirty, no owner
    assert after.write_epoch > before_epoch
    async with db.transaction() as s:
        assert (await s.execute(select(TaskBaseline).where(TaskBaseline.task_id == waiting.id))).first() is None
    approval = (await h.approvals(waiting.id))[0]
    assert approval.target["reason_code"] == P.RECOVERY_REASON_PROJECT_DIRTY
    assert approval.target["dirty_epoch"] == after.write_epoch                       # the gate carries the NEW epoch


async def test_a_resolved_or_legacy_old_row_lets_the_next_task_acquire(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    old = await new_task(h, project, "the earlier task")
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == old.id).values(status="FAILED"))
    await insert_execution(db, old.id, '{"tracked": true, "v": 1, "proof": "NEVER_RAN"}')
    await insert_execution(db, old.id, '{"abandoned_by_takeover": true}')
    task = await new_task(h, project)
    lease = (await h.leases.acquire(task.id)).lease
    assert await h.locks.ensure_for_step(lease, task.id, "DEVELOP", h.gates) is StepGate.READY
    assert (await project_row(h, project.id)).write_owner_task_id == task.id


async def test_the_guard_runs_for_the_acquiring_task_only_after_the_owned_and_dirty_branches(db, tmp_path):
    """An owner that already holds the lock re-enters (READY) even with an unresolved row of its OWN task: the guard sits on the
    ACQUISITION path only; a non-CLEAN project keeps today's gate and does not mark_dirty again."""
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    lease = (await h.leases.acquire(task.id)).lease
    assert await h.locks.ensure_for_step(lease, task.id, "DEVELOP", h.gates) is StepGate.READY
    await insert_execution(db, task.id, '{"tracked": true}', finished=False)
    assert await h.locks.ensure_for_step(lease, task.id, "DEVELOP", h.gates) is StepGate.READY   # owned: re-entry
    assert (await project_row(h, project.id)).write_owner_task_id == task.id


async def test_continue_cannot_accept_a_baseline_while_a_tracked_row_is_unresolved_or_anomalous(db, tmp_path):
    """The DIRTY clause of the PROJECT_DIRTY `continue` (design 7.4, 'After DIRTY_UNRESOLVED'): a plain Conflict, the whole
    decision rolls back, the project stays DIRTY and nothing resumes. Resolving the row lets the same `continue` through."""
    from tests.integration_pg.test_project_lock import dirty_project, project_row as pr, requests_of
    h = Harness(db, tmp_path / "ws")
    project, root = await dirty_project(h, tmp_path)
    waiting = await new_task(h, project, "wants the lock now")
    await h.start(waiting.id)
    approval = (await h.approvals(waiting.id))[0]
    stuck = await insert_execution(db, waiting.id, '{"tracked": true}', finished=False)
    with pytest.raises(Conflict) as info:
        await h.decide(approval.id, "APPROVED", "continue", key="k1")
    # B2 phase 5b: the offering condition of `continue` refuses first (409 OPTION_NOT_AVAILABLE, a Conflict); the guard of
    # assert_no_unresolved_or_anomalous_tracked stays behind it as the defence
    assert info.value.code == "OPTION_NOT_AVAILABLE" and info.value.details["reason_code"] == "UNRESOLVED_ROWS_PRESENT"
    assert (await pr(h, project.id)).workspace_state == "DIRTY_UNRESOLVED"
    assert [r.kind for r in await requests_of(h, waiting.id)] == ["START"]
    async with db.transaction() as s:           # an anomaly blocks as well
        await s.execute(text("UPDATE executions SET detail = CAST('{\"tracked\": \"true\"}' AS jsonb), finished_at = now() "
                             "WHERE id = :i"), {"i": stuck})
    with pytest.raises(Conflict) as info:
        await h.decide(approval.id, "APPROVED", "continue", key="k2")
    assert info.value.code == "OPTION_NOT_AVAILABLE" and info.value.details["reason_code"] == "TRACKED_ANOMALY_PRESENT"
    async with db.transaction() as s:
        await s.execute(text("UPDATE executions SET detail = CAST('{\"tracked\": true, \"v\": 1, \"proof\": \"JOB_EMPTY\"}' AS jsonb)"
                             " WHERE id = :i"), {"i": stuck})
    assert (await h.decide(approval.id, "APPROVED", "continue", key="k3")).created
    assert (await pr(h, project.id)).workspace_state == "CLEAN"


# The `<KIND6_SUBREASON>` block of design 7.4 (the doc lists it among the named blocks but `tracked_sql` has no constant for it, a
# gap of the design/phase 1: copied here so that the sub-reason of kind 6 has a parity test too). `decode_error` is Python-only.
KIND6_SUBREASON_SQL = """CASE
  WHEN jsonb_exists(detail, 'tracked') AND ((detail -> 'tracked') = 'true'::jsonb) IS NOT TRUE THEN 'tracked_type'
  WHEN NOT jsonb_exists(detail, 'tracked') THEN 'tracked_missing'
  WHEN finished_at IS NULL AND ((detail ->> 'proof') IS NOT NULL OR (detail ->> 'disposition') IS NOT NULL) THEN 'finished_at_missing'
  ELSE 'other'
END"""


async def test_the_kind6_sub_reason_of_the_python_classifier_equals_the_sql_block(db):
    _, task = await make_project_task(db)
    await load_cases(db, task.id, only_class=ANOMALY)
    async with db.transaction() as s:
        rows = (await s.execute(text(
            f"SELECT {S.TRACKED_ROW_COLUMNS_SQL}, {S.ANOMALY_KIND_SQL} AS kind, {KIND6_SUBREASON_SQL} AS sub "
            f"FROM executions WHERE task_id = :t AND ({S.ANOMALY_SQL})"), {"t": task.id})).all()
    checked = 0
    for row_id, _, pid, _, finished_at, detail_text, kind, sub in rows:
        got = classify_row(row_id, pid, finished_at, detail_text)
        assert got.anomaly_kind == kind
        if kind == "OTHER_SCHEMA_INVALID":
            assert got.sub_reason == sub, (detail_text, got, sub)
            checked += 1
    assert checked >= 10
