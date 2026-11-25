"""B2 phase 1 on the embedded PostgreSQL: migration 0005 (up/down/up, `alembic check`), the two tables, their CHECK constraints
and partial indexes, the canonical tracked-row predicates, and the minimal repositories. Real database, no mocks, no CLI."""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from alembic import command
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from app.models.tables import Approval, Project, Task
from app.repositories import tracked_sql as S
from app.repositories.process_risk_repository import ProcessRiskRequestRepository
from app.repositories.runtime_capability_repository import RuntimeCapabilityRepository
from tests.conftest import alembic_config
from tests.helpers import make_task

UTC_NOW = lambda: datetime.now(UTC)  # noqa: E731


# --- fixtures and helpers ---------------------------------------------------------------------------------------------------
async def make_project_task(db):
    task = await make_task(db)
    async with db.transaction() as s:
        key = f"/w/{uuid.uuid4().hex}"
        project = Project(id=uuid.uuid4(), name="p", workspace_path=key, workspace_key=key)
        s.add(project)
        await s.flush()
        await s.execute(update(Task).where(Task.id == task.id).values(project_id=project.id))
    return project, task


async def make_approval(db, task, target=None) -> uuid.UUID:
    async with db.transaction() as s:
        approval = Approval(id=uuid.uuid4(), task_id=task.id, type="RECOVERY", target=target or {}, scope="TASK",
                            request_payload={}, basis={}, checkpoint_step_key="DEVELOP",
                            valid_until=UTC_NOW() + timedelta(days=1))
        s.add(approval)
        await s.flush()
        return approval.id


async def new_request(db, *, kind="ACCEPT", epoch=1, targets=None, rationale="a reason", requested_by=None,
                      project=None, task=None, **kw):
    if project is None:
        project, task = await make_project_task(db)
    approval_id = await make_approval(db, task)
    async with db.transaction() as s:
        row = await ProcessRiskRequestRepository(s).insert(
            approval_id=approval_id, kind=kind, task_id=task.id, project_id=project.id, dirty_epoch=epoch,
            target_execution_ids=targets or [uuid.uuid4()], rationale=rationale, requested_by=requested_by, **kw)
        return row.approval_id, project, task


async def insert_execution(db, task_id, detail_json: str | None, *, finished=True, pid: int | None = 4242,
                           row_id: uuid.UUID | None = None) -> uuid.UUID:
    """Raw insert so the exact JSON text (SQL NULL, JSON null, array, scalar, odd types) reaches the column."""
    row_id = row_id or uuid.uuid4()
    async with db.transaction() as s:
        await s.execute(text(
            "INSERT INTO executions (id, task_id, role, provider, outcome, pid, finished_at, started_at, detail) "
            "VALUES (:id, :t, 'DEVELOPER', 'CLAUDE', 'STARTING', :pid, :fin, now(), CAST(:d AS jsonb))"),
            {"id": row_id, "t": task_id, "pid": pid, "fin": UTC_NOW() if finished else None, "d": detail_json})
    return row_id


async def ids_where(db, predicate: str, extra: str = "") -> set[uuid.UUID]:
    async with db.transaction() as s:
        rows = await s.execute(text(f"SELECT id FROM executions WHERE ({predicate}) {extra}"))
        return {r[0] for r in rows}


def risk_record(row_id, **override) -> dict:
    record = {"approval_id": str(uuid.uuid4()), "execution_id": str(row_id), "pid": 4242, "create_time": 1700000000.5,
              "last_verdict": "UNCLEAR", "requested_at": "2026-11-24T10:00:00Z", "rationale": "the process is gone, I checked",
              "reverify_result": "UNKNOWN", "applied_at": "2026-11-24T10:00:05.123456Z"}
    for key, value in override.items():
        if value is KeyError:
            record.pop(key, None)
        else:
            record[key] = value
    return record


def risk_detail(row_id, record=None, **top) -> str:
    detail = {"tracked": True, "v": 1, "disposition": "OPERATOR_RISK_ACCEPTED", "reason_code": "OPERATOR_RISK_ACCEPTED",
              "risk_acceptance": record if record is not None else risk_record(row_id)}
    for key, value in top.items():
        if value is KeyError:
            detail.pop(key, None)
        else:
            detail[key] = value
    return json.dumps(detail)


# --- repositories -----------------------------------------------------------------------------------------------------------
async def test_runtime_capability_get_and_upsert_with_the_incarnation_guard(db):
    key = "process_tracking"
    async with db.transaction() as s:
        repo = RuntimeCapabilityRepository(s)
        assert await repo.get(key) is None
        assert await repo.upsert(key, {"state": "UNKNOWN", "incarnation_id": "A"}) is True        # insert
        assert (await repo.get(key)).value == {"state": "UNKNOWN", "incarnation_id": "A"}
        assert await repo.upsert(key, {"state": "SUPPORTED", "incarnation_id": "A"}, expected_incarnation_id="A") is True
        assert (await repo.get(key)).value["state"] == "SUPPORTED"
        # a late write of another (older) incarnation never overwrites
        assert await repo.upsert(key, {"state": "UNAVAILABLE", "incarnation_id": "OLD"}, expected_incarnation_id="OLD") is False
        assert (await repo.get(key)).value == {"state": "SUPPORTED", "incarnation_id": "A"}
        # the new incarnation takes over unconditionally (the mandatory UNKNOWN upsert) ...
        assert await repo.upsert(key, {"state": "UNKNOWN", "incarnation_id": "B"}) is True
        # ... and from then on the first one is the stale writer
        assert await repo.upsert(key, {"state": "SUPPORTED", "incarnation_id": "A"}, expected_incarnation_id="A") is False
        assert (await repo.get(key)).value == {"state": "UNKNOWN", "incarnation_id": "B"}


async def test_runtime_capability_guarded_write_inserts_a_missing_row_and_stamps_updated_at(db):
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    async with db.transaction() as s:
        repo = RuntimeCapabilityRepository(s)
        assert await repo.upsert("k", {"incarnation_id": "A"}, expected_incarnation_id="A", now=stamp) is True
        assert (await repo.get("k")).updated_at == stamp
        before = (await repo.get("k")).updated_at
        assert await repo.upsert("k", {"incarnation_id": "A", "x": 1}, expected_incarnation_id="A") is True
        await s.refresh(await repo.get("k"))
        assert (await repo.get("k")).updated_at > before


async def test_process_risk_insert_stores_a_sorted_snapshot_with_the_design_defaults_and_get_reads_it(db):
    ids = [uuid.UUID(int=n) for n in (30, 10, 20)]
    approval_id, project, task = await new_request(db, targets=ids, requested_by="op@example.org")
    async with db.transaction() as s:
        row = await ProcessRiskRequestRepository(s).get(approval_id)
        assert row.target_execution_ids == sorted(ids) == [uuid.UUID(int=10), uuid.UUID(int=20), uuid.UUID(int=30)]
        assert (row.kind, row.status, row.attempts, row.claim_generation, row.result) == ("ACCEPT", "REQUESTED", 0, 0, {})
        assert row.claimed_by_incarnation is None and row.claimed_at is None and row.finished_at is None
        assert row.last_error is None and row.reason is None and row.requested_by == "op@example.org"
        assert (row.task_id, row.project_id, row.dirty_epoch) == (task.id, project.id, 1)
        assert row.created_at is not None and row.updated_at is not None and row.next_attempt_at is not None
        assert await ProcessRiskRequestRepository(s).get(uuid.uuid4()) is None


async def test_a_second_request_for_the_same_approval_violates_the_primary_key(db):
    approval_id, project, task = await new_request(db)
    with pytest.raises(IntegrityError, match="process_risk_requests_pkey"):
        async with db.transaction() as s:
            await ProcessRiskRequestRepository(s).insert(
                approval_id=approval_id, kind="ACCEPT", task_id=task.id, project_id=project.id, dirty_epoch=1,
                target_execution_ids=[uuid.uuid4()], rationale="again")


async def test_foreign_keys_reject_an_unknown_approval_task_or_project(db):
    project, task = await make_project_task(db)
    approval_id = await make_approval(db, task)
    for bad in ({"approval_id": uuid.uuid4()}, {"task_id": uuid.uuid4()}, {"project_id": uuid.uuid4()}):
        values = dict(approval_id=approval_id, kind="ACCEPT", task_id=task.id, project_id=project.id, dirty_epoch=1,
                      target_execution_ids=[uuid.uuid4()], rationale="r") | bad
        with pytest.raises(IntegrityError, match="fk_process_risk_requests"):
            async with db.transaction() as s:
                await ProcessRiskRequestRepository(s).insert(**values)


async def test_list_due_discovers_by_status_and_next_attempt_oldest_first(db):
    now = UTC_NOW()
    project, task = await make_project_task(db)
    early, _, _ = await new_request(db, project=project, task=task, next_attempt_at=now - timedelta(seconds=30))
    late, _, _ = await new_request(db, project=project, task=task, next_attempt_at=now - timedelta(seconds=5))
    future, _, _ = await new_request(db, project=project, task=task, next_attempt_at=now + timedelta(minutes=5))
    retry, _, _ = await new_request(db, project=project, task=task, next_attempt_at=now - timedelta(seconds=60))
    done, _, _ = await new_request(db, project=project, task=task, next_attempt_at=now - timedelta(seconds=90))
    async with db.transaction() as s:
        await s.execute(text("UPDATE process_risk_requests SET status='RETRYABLE' WHERE approval_id=:i"), {"i": retry})
        await s.execute(text("UPDATE process_risk_requests SET status='APPLIED', finished_at=now() WHERE approval_id=:i"),
                        {"i": done})
    async with db.transaction() as s:
        repo = ProcessRiskRequestRepository(s)
        due = await repo.list_due(("REQUESTED", "RETRYABLE"), now, 10)
        assert [r.approval_id for r in due] == [retry, early, late]            # not future, not APPLIED; oldest first
        assert [r.approval_id for r in await repo.list_due(("REQUESTED",), now, 10)] == [early, late]
        assert [r.approval_id for r in await repo.list_due(("REQUESTED", "RETRYABLE"), now, 2)] == [retry, early]
        assert [r.approval_id for r in await repo.list_due(("APPLIED",), now, 10)] == [done]
        assert [r.approval_id for r in await repo.list_due(("REQUESTED",), now + timedelta(minutes=10), 10)] == [
            early, late, future]


# --- CHECK constraints ------------------------------------------------------------------------------------------------------
async def insert_with(db, project, task, **overrides):
    approval_id = await make_approval(db, task)
    values = dict(approval_id=approval_id, kind="ACCEPT", task_id=task.id, project_id=project.id, dirty_epoch=1,
                  target_execution_ids=[uuid.uuid4()], rationale="r", requested_by=None) | overrides
    async with db.transaction() as s:
        await ProcessRiskRequestRepository(s).insert(**values)


@pytest.mark.parametrize("field,value,accepted", [
    ("requested_by", "a" * 128, True), ("requested_by", "a" * 129, False), ("requested_by", "", False),
    ("requested_by", None, True), ("requested_by", "x", True),
    ("rationale", "r" * 2000, True), ("rationale", "r" * 2001, False), ("rationale", "", False),
    ("rationale", "\U0001F600" * 2000, True), ("rationale", "\U0001F600" * 2001, False),    # code points, not bytes
    ("kind", "ACCEPT", True), ("kind", "RECHECK", True), ("kind", "accept", False), ("kind", "OTHER", False),
])
async def test_check_constraints_on_text_columns(db, field, value, accepted):
    project, task = await make_project_task(db)
    if accepted:
        await insert_with(db, project, task, **{field: value})
    else:
        with pytest.raises(IntegrityError, match=f"ck_process_risk_requests_(requested_by|rationale|kind)"):
            await insert_with(db, project, task, **{field: value})


@pytest.mark.parametrize("count,accepted", [(0, False), (1, True), (500, True), (501, False)])
async def test_target_cardinality_check_is_one_to_five_hundred(db, count, accepted):
    project, task = await make_project_task(db)
    targets = [uuid.uuid4() for _ in range(count)]
    if accepted:
        await insert_with(db, project, task, target_execution_ids=targets)
    else:
        with pytest.raises(IntegrityError, match="ck_process_risk_requests_target_count"):
            await insert_with(db, project, task, target_execution_ids=targets)


async def test_status_check_allows_exactly_the_six_statuses(db):
    approval_id, _, _ = await new_request(db)
    for status in ("REQUESTED", "VERIFYING", "APPLIED", "REFUSED_ALIVE", "RETRYABLE", "NEEDS_REDECISION"):
        async with db.transaction() as s:
            await s.execute(text("UPDATE process_risk_requests SET status=:s WHERE approval_id=:i"),
                            {"s": status, "i": approval_id})
    for bad in ("PENDING", "DONE", "applied", ""):
        with pytest.raises(IntegrityError, match="ck_process_risk_requests_status"):
            async with db.transaction() as s:
                await s.execute(text("UPDATE process_risk_requests SET status=:s WHERE approval_id=:i"),
                                {"s": bad, "i": approval_id})


# --- partial unique indexes -------------------------------------------------------------------------------------------------
async def test_only_one_active_recheck_per_project_and_epoch(db):
    project, task = await make_project_task(db)
    first, _, _ = await new_request(db, kind="RECHECK", epoch=3, project=project, task=task)
    with pytest.raises(IntegrityError, match="uq_process_risk_recheck_active"):
        await new_request(db, kind="RECHECK", epoch=3, project=project, task=task)
    # another epoch, another project and the other kind are not limited
    await new_request(db, kind="RECHECK", epoch=4, project=project, task=task)
    await new_request(db, kind="ACCEPT", epoch=3, project=project, task=task)
    await new_request(db, kind="ACCEPT", epoch=3, project=project, task=task)
    await new_request(db, kind="RECHECK", epoch=3)
    # active statuses all block, terminal ones free the slot
    for status in ("VERIFYING", "RETRYABLE"):
        async with db.transaction() as s:
            await s.execute(text("UPDATE process_risk_requests SET status=:s WHERE approval_id=:i"), {"s": status, "i": first})
        with pytest.raises(IntegrityError, match="uq_process_risk_recheck_active"):
            await new_request(db, kind="RECHECK", epoch=3, project=project, task=task)
    for status in ("APPLIED", "REFUSED_ALIVE", "NEEDS_REDECISION"):
        async with db.transaction() as s:
            await s.execute(text("UPDATE process_risk_requests SET status=:s, finished_at=now() WHERE approval_id=:i"),
                            {"s": status, "i": first})
        again, _, _ = await new_request(db, kind="RECHECK", epoch=3, project=project, task=task)
        async with db.transaction() as s:
            await s.execute(text("UPDATE process_risk_requests SET status='APPLIED', finished_at=now() WHERE approval_id=:i"),
                            {"i": again})


async def test_an_approval_can_have_at_most_one_direct_successor(db):
    project, task = await make_project_task(db)
    parent = await make_approval(db, task)
    await make_approval(db, task, {"supersedes_approval_id": str(parent)})
    with pytest.raises(IntegrityError, match="uq_approvals_lineage_successor"):
        await make_approval(db, task, {"supersedes_approval_id": str(parent)})
    await make_approval(db, task, {"supersedes_approval_id": str(uuid.uuid4())})      # another parent
    await make_approval(db, task, {})                                                  # no lineage: never in the index
    await make_approval(db, task, {})
    await make_approval(db, task, {"supersedes_approval_id": None})                    # a JSON null is not a successor
    await make_approval(db, task, {"supersedes_approval_id": None})


# --- the stored index definitions ---------------------------------------------------------------------------------------------
async def test_indexes_exist_with_the_designed_columns_uniqueness_and_predicates(db):
    async with db.transaction() as s:
        rows = (await s.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public' AND indexname IN "
            "('ix_executions_tracked_unresolved','ix_executions_tracked_anomaly','uq_approvals_lineage_successor',"
            "'uq_process_risk_recheck_active','ix_process_risk_requests_status_next_attempt',"
            "'ix_process_risk_requests_project_finished','process_risk_requests_pkey','runtime_capabilities_pkey')"))).all()
    defs = {n: d for n, d in rows}
    assert set(defs) == {"ix_executions_tracked_unresolved", "ix_executions_tracked_anomaly", "uq_approvals_lineage_successor",
                         "uq_process_risk_recheck_active", "ix_process_risk_requests_status_next_attempt",
                         "ix_process_risk_requests_project_finished", "process_risk_requests_pkey",
                         "runtime_capabilities_pkey"}
    assert "(status, next_attempt_at)" in defs["ix_process_risk_requests_status_next_attempt"]
    assert "WHERE" not in defs["ix_process_risk_requests_status_next_attempt"]
    assert "(project_id, finished_at) WHERE (finished_at IS NOT NULL)" in defs["ix_process_risk_requests_project_finished"]
    assert defs["uq_process_risk_recheck_active"].startswith("CREATE UNIQUE INDEX") and "(project_id, dirty_epoch)" in defs[
        "uq_process_risk_recheck_active"]
    assert "(target ->> 'supersedes_approval_id'::text)" in defs["uq_approvals_lineage_successor"]
    assert defs["uq_approvals_lineage_successor"].startswith("CREATE UNIQUE INDEX")
    assert "(task_id) WHERE" in defs["ix_executions_tracked_unresolved"] and "(task_id) WHERE" in defs["ix_executions_tracked_anomaly"]


async def stored_predicate(s, index_name: str) -> str:
    return (await s.execute(text(
        "SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = :n"), {"n": index_name})).scalar_one()


@pytest.mark.parametrize("index_name,canonical", [("ix_executions_tracked_unresolved", S.U_SQL),
                                                  ("ix_executions_tracked_anomaly", S.ANOMALY_SQL)])
async def test_the_stored_index_predicate_is_what_the_canonical_text_deparses_to(db, index_name, canonical):
    """EXPLAIN-independent: the same canonical text is applied to a scratch copy of `executions` and PostgreSQL's own
    deparsed predicate is compared with the one stored by the migration. (Whether the planner picks the index is a separate,
    unverified question.)"""
    async with db.transaction() as s:
        await s.execute(text("CREATE TEMP TABLE ex_probe (LIKE executions)"))
        await s.execute(text(f"CREATE INDEX ex_probe_idx ON ex_probe (task_id) WHERE {canonical}"))
        probe = await stored_predicate(s, "ex_probe_idx")
        stored = await stored_predicate(s, index_name)
    assert stored == probe and stored


# --- the canonical predicates against real rows ---------------------------------------------------------------------------------
LEGACY, UNRESOLVED, RESOLVED, ANOMALY = "legacy", "U", "resolved", "A"


def tracked_cases(row_id: uuid.UUID) -> list[tuple[str, str | None, str, bool, str | None]]:
    """(label, detail JSON text or None for SQL NULL, expected class, finished, expected anomaly kind)."""
    rec = lambda **o: risk_record(row_id, **o)  # noqa: E731
    rd = lambda record=None, **top: risk_detail(row_id, record, **top)  # noqa: E731
    long_rationale = "r" * 2000
    return [
        # legacy rows: in neither set
        ("sql-null", None, LEGACY, True, None), ("empty-object", "{}", LEGACY, True, None),
        ("legacy-keys", '{"resumed": true, "termination": "x", "permission_requests": 1}', LEGACY, True, None),
        ("json-null", "null", LEGACY, True, None), ("array", "[]", LEGACY, True, None), ("string", '"x"', LEGACY, True, None),
        ("number", "1", LEGACY, True, None), ("abandoned", '{"abandoned_by_takeover": true}', LEGACY, True, None),
        ("tree-only", '{"tree": "EMPTY", "reason": "x", "lifecycle": "y"}', LEGACY, True, None),
        # normal unresolved
        ("tracked-open", '{"tracked": true}', UNRESOLVED, False, None),
        ("tracked-closed-unproven", '{"tracked": true, "v": 1}', UNRESOLVED, True, None),
        ("tracked-unknown-v-no-proof", '{"tracked": true, "v": 7}', UNRESOLVED, False, None),
        ("tracked-null-proof", '{"tracked": true, "v": 1, "proof": null, "disposition": null}', UNRESOLVED, False, None),
        ("tracked-with-descriptive", '{"tracked": true, "v": 1, "runner_process": {"pid": 1}}', UNRESOLVED, False, None),
        # properly resolved by a proof
        *[(f"proof-{p}", json.dumps({"tracked": True, "v": 1, "proof": p}), RESOLVED, True, None)
          for p in ("JOB_EMPTY", "JOB_KILL_ON_CLOSE_ASSUMED", "NEVER_RAN")],
        ("proof-v-float-one", '{"tracked": true, "v": 1.0, "proof": "JOB_EMPTY"}', RESOLVED, True, None),
        # properly resolved by a valid risk acceptance
        ("risk-valid", rd(), RESOLVED, True, None),
        ("risk-valid-open-row-is-A", rd(), ANOMALY, False, "OTHER_SCHEMA_INVALID"),
        ("risk-requested-by", rd(rec(requested_by="op@example.org")), RESOLVED, True, None),
        ("risk-pid-float", rd(rec(pid=4242.0)), RESOLVED, True, None),
        ("risk-create-time-int", rd(rec(create_time=1700000000)), RESOLVED, True, None),
        ("risk-create-time-zero", rd(rec(create_time=0)), RESOLVED, True, None),
        ("risk-extra-key-ignored", rd(rec(unknown_extra="x")), RESOLVED, True, None),
        ("risk-rationale-2000", rd(rec(rationale=long_rationale)), RESOLVED, True, None),
        ("risk-rationale-newline-tab", rd(rec(rationale="line one\n\tline two")), RESOLVED, True, None),
        ("risk-rationale-emoji", rd(rec(rationale="\U0001F600 gone")), RESOLVED, True, None),
        *[(f"risk-reverify-{v}", rd(rec(reverify_result=v)), RESOLVED, True, None)
          for v in ("UNKNOWN", "ACCESS_DENIED", "OTHER_HOST", "H3_DISABLED", "IDENTITY_RECORD_MISSING")],
        ("risk-ts-no-fraction", rd(rec(requested_at="2026-01-01T00:00:00Z")), RESOLVED, True, None),
        # anomalies: marker defects
        ("tracked-string", '{"tracked": "true"}', ANOMALY, True, "OTHER_SCHEMA_INVALID"),
        ("tracked-false", '{"tracked": false}', ANOMALY, True, "OTHER_SCHEMA_INVALID"),
        ("tracked-one", '{"tracked": 1}', ANOMALY, True, "OTHER_SCHEMA_INVALID"),
        ("tracked-null", '{"tracked": null}', ANOMALY, True, "OTHER_SCHEMA_INVALID"),
        ("tracked-array", '{"tracked": [true]}', ANOMALY, True, "OTHER_SCHEMA_INVALID"),
        ("tracked-object", '{"tracked": {}}', ANOMALY, True, "OTHER_SCHEMA_INVALID"),
        # a tracked-only key without the marker; a proof or disposition under a missing `v` is kind 1 (first match wins)
        *[(f"tracked-missing-{k}", json.dumps({k: "x"}), ANOMALY, True,
           "UNKNOWN_SCHEMA_VERSION" if k in ("proof", "disposition") else "OTHER_SCHEMA_INVALID")
          for k in ("v", "runner_process", "checkpoint_step_key", "proof", "disposition", "reason_code", "risk_acceptance",
                    "anomaly_original", "anomaly_settlement")],
        # anomalies: proof / disposition defects, with the first-match kind
        ("proof-unknown-v", '{"tracked": true, "v": 2, "proof": "JOB_EMPTY"}', ANOMALY, True, "UNKNOWN_SCHEMA_VERSION"),
        ("proof-no-v", '{"tracked": true, "proof": "JOB_EMPTY"}', ANOMALY, True, "UNKNOWN_SCHEMA_VERSION"),
        ("proof-v-string", '{"tracked": true, "v": "1", "proof": "JOB_EMPTY"}', ANOMALY, True, "UNKNOWN_SCHEMA_VERSION"),
        ("proof-v-bool", '{"tracked": true, "v": true, "proof": "JOB_EMPTY"}', ANOMALY, True, "UNKNOWN_SCHEMA_VERSION"),
        ("proof-and-disposition", '{"tracked": true, "v": 1, "proof": "JOB_EMPTY", "disposition": "OPERATOR_RISK_ACCEPTED"}',
         ANOMALY, True, "CONTRADICTORY_PROOF_DISPOSITION"),
        ("proof-bogus", '{"tracked": true, "v": 1, "proof": "TREE_EMPTY"}', ANOMALY, True, "INVALID_PROOF"),
        ("proof-number", '{"tracked": true, "v": 1, "proof": 5}', ANOMALY, True, "INVALID_PROOF"),
        ("proof-without-finished-at", '{"tracked": true, "v": 1, "proof": "JOB_EMPTY"}', ANOMALY, False, "OTHER_SCHEMA_INVALID"),
        ("disposition-bogus", '{"tracked": true, "v": 1, "disposition": "OPERATOR_IGNORED"}', ANOMALY, True,
         "INVALID_DISPOSITION_KIND"),
        ("disposition-object", '{"tracked": true, "v": 1, "disposition": {}}', ANOMALY, True, "INVALID_DISPOSITION_KIND"),
        ("disposition-without-risk-record", '{"tracked": true, "v": 1, "disposition": "OPERATOR_RISK_ACCEPTED"}', ANOMALY, True,
         "INCOMPLETE_RISK_ACCEPTANCE"),
        # anomalies: the risk record and reason_code
        ("risk-reason-code-missing", rd(reason_code=KeyError), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-reason-code-null", rd(reason_code=None), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-reason-code-other", rd(reason_code="OTHER"), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-reason-code-number", rd(reason_code=1), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-not-object", rd(record=[1]), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-null", '{"tracked": true, "v": 1, "disposition": "OPERATOR_RISK_ACCEPTED", "reason_code": '
                      '"OPERATOR_RISK_ACCEPTED", "risk_acceptance": null}', ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        *[(f"risk-missing-{k}", rd(rec(**{k: KeyError})), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE")
          for k in ("approval_id", "execution_id", "pid", "create_time", "last_verdict", "requested_at", "rationale",
                    "reverify_result", "applied_at")],
        *[(f"risk-null-{k}", rd(rec(**{k: None})), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE")
          for k in ("approval_id", "execution_id", "pid", "create_time", "last_verdict", "requested_at", "rationale",
                    "reverify_result", "applied_at", "requested_by")],
        ("risk-approval-id-uppercase", rd(rec(approval_id=str(uuid.uuid4()).upper())), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-approval-id-number", rd(rec(approval_id=7)), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-execution-id-other-row", rd(rec(execution_id=str(uuid.uuid4()))), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-pid-other", rd(rec(pid=4243)), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-pid-string", rd(rec(pid="4242")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-create-time-negative", rd(rec(create_time=-1)), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-create-time-string", rd(rec(create_time="1700000000")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-verdict-other", rd(rec(last_verdict="ALIVE")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-ts-no-z", rd(rec(requested_at="2026-11-24T10:00:00")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-ts-7-digits", rd(rec(applied_at="2026-11-24T10:00:00.1234567Z")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-ts-space", rd(rec(applied_at="2026-11-24 10:00:00Z")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-rationale-empty", rd(rec(rationale="")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-rationale-blank", rd(rec(rationale="  \t\n ")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-rationale-2001", rd(rec(rationale="r" * 2001)), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        *[(f"risk-rationale-forbidden-U+{c:04X}", rd(rec(rationale=f"gone{chr(c)}x")), ANOMALY, True,
           "INCOMPLETE_RISK_ACCEPTANCE")
          for c in (0x01, 0x07, 0x08, 0x0B, 0x0C, 0x1F, 0x7F, 0x85, 0x9F, 0x2028, 0x2029, 0x202A, 0x202E, 0x2066, 0x2069)],
        ("risk-rationale-nbsp-ok", rd(rec(rationale="gone x")), RESOLVED, True, None),
        ("risk-rationale-bidi-boundary-ok", rd(rec(rationale="gone ⁥⁪")), RESOLVED, True, None),
        ("risk-reverify-other", rd(rec(reverify_result="NOPE")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-requested-by-space", rd(rec(requested_by="a b")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-requested-by-empty", rd(rec(requested_by="")), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-requested-by-129", rd(rec(requested_by="a" * 129)), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-requested-by-128", rd(rec(requested_by="a" * 128)), RESOLVED, True, None),
        ("risk-requested-by-number", rd(rec(requested_by=5)), ANOMALY, True, "INCOMPLETE_RISK_ACCEPTANCE"),
        ("risk-with-proof-too", rd(proof="JOB_EMPTY"), ANOMALY, True, "CONTRADICTORY_PROOF_DISPOSITION"),
    ]


async def load_cases(db, task_id, *, only_class: str | None = None) -> dict[uuid.UUID, tuple[str, str, str | None]]:
    """Inserts every case as one execution row (the risk record names its own row id) and returns id -> (label, class, kind)."""
    loaded = {}
    for index in range(len(tracked_cases(uuid.UUID(int=0)))):
        row_id = uuid.uuid4()
        label, detail, klass, finished, kind = tracked_cases(row_id)[index]
        if only_class is not None and klass != only_class:
            continue
        await insert_execution(db, task_id, detail, finished=finished, row_id=row_id)
        loaded[row_id] = (label, klass, kind)
    return loaded


async def test_the_canonical_predicates_classify_every_row_class_exactly_once(db):
    _, task = await make_project_task(db)
    expected = await load_cases(db, task.id)
    assert len(expected) > 120 and len({label for label, _, _ in expected.values()}) == len(expected)
    in_u = await ids_where(db, S.U_SQL)
    in_resolved = await ids_where(db, S.RESOLVED_VALID_SQL)
    in_a = await ids_where(db, S.ANOMALY_SQL)
    in_ish = await ids_where(db, S.TRACKED_ISH_SQL)
    wrong = []
    for row_id, (label, klass, _) in expected.items():
        got = (row_id in in_u, row_id in in_resolved, row_id in in_a, row_id in in_ish)
        want = {LEGACY: (False, False, False, False), UNRESOLVED: (True, False, False, True),
                RESOLVED: (False, True, False, True), ANOMALY: (False, False, True, True)}[klass]
        if got != want:
            wrong.append((label, klass, got))
    assert not wrong, wrong
    # the partition: U, resolved and A are pairwise disjoint and every tracked-ish row is in exactly one of them
    assert not (in_u & in_resolved) and not (in_u & in_a) and not (in_resolved & in_a)
    assert in_u | in_resolved | in_a == in_ish


async def test_the_anomaly_case_assigns_the_first_matching_kind(db):
    _, task = await make_project_task(db)
    expected = await load_cases(db, task.id, only_class=ANOMALY)
    async with db.transaction() as s:
        rows = (await s.execute(text(f"SELECT id, {S.ANOMALY_KIND_SQL} AS kind FROM executions WHERE ({S.ANOMALY_SQL})"))).all()
    got = {r[0]: r[1] for r in rows}
    assert set(got) == set(expected)
    assert [(label, kind, got[i]) for i, (label, _, kind) in expected.items() if got[i] != kind] == []


async def test_result_sets_do_not_depend_on_whether_the_partial_indexes_are_used(db):
    """Not an EXPLAIN test: the same predicates must return the same rows with sequential scans forbidden (index plans) and
    allowed. Which plan the planner picks, and its cost, remain unverified."""
    _, task = await make_project_task(db)
    await load_cases(db, task.id)
    results = {}
    for scans in ("on", "off"):
        async with db.transaction() as s:
            await s.execute(text(f"SET LOCAL enable_seqscan = {scans}"))
            for name, predicate in (("U", S.U_SQL), ("A", S.ANOMALY_SQL)):
                rows = await s.execute(text(f"SELECT id FROM executions WHERE task_id = :t AND ({predicate})"), {"t": task.id})
                results[(scans, name)] = {r[0] for r in rows}
    assert results[("on", "U")] == results[("off", "U")] and results[("on", "A")] == results[("off", "A")]
    assert results[("on", "U")] and results[("on", "A")]


async def test_project_scope_block_finds_rows_through_the_task_subquery(db):
    project, task = await make_project_task(db)
    _, other_task = await make_project_task(db)
    mine = await insert_execution(db, task.id, '{"tracked": true}', finished=False)
    await insert_execution(db, other_task.id, '{"tracked": true}', finished=False)
    async with db.transaction() as s:
        rows = await s.execute(text(f"SELECT id FROM executions WHERE {S.PROJECT_SCOPE_SQL} AND ({S.U_SQL}) ORDER BY id"),
                               {"project_id": project.id})
        assert [r[0] for r in rows] == [mine]


# --- migration up / down / up and `alembic check` --------------------------------------------------------------------------------
PHASE1_INDEXES = ("ix_executions_tracked_unresolved", "ix_executions_tracked_anomaly", "uq_approvals_lineage_successor",
                  "uq_process_risk_recheck_active", "ix_process_risk_requests_status_next_attempt",
                  "ix_process_risk_requests_project_finished")


async def schema_state(url: str) -> dict:
    conn = await asyncpg.connect(url.replace("postgresql+asyncpg", "postgresql"))
    try:
        tables = {r[0] for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        indexes = {r[0] for r in await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname='public'")}
        version = await conn.fetchval("SELECT version_num FROM alembic_version")
        return {"tables": tables, "indexes": indexes, "version": version}
    finally:
        await conn.close()


async def test_migration_0004_to_0005_down_and_up_again_and_alembic_check(pg):
    name = f"mig5_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg.sync_uri)
    await admin.execute(f"CREATE DATABASE {name}")
    await admin.close()
    url = pg.async_url(name)
    cfg = alembic_config(url)
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0004")
        before = await schema_state(url)
        assert before["version"] == "0004" and "task_baselines" in before["tables"]
        assert not {"runtime_capabilities", "process_risk_requests"} & before["tables"]
        assert not set(PHASE1_INDEXES) & before["indexes"]

        await asyncio.to_thread(command.upgrade, cfg, "0005")
        after = await schema_state(url)
        assert after["version"] == "0005"
        assert {"runtime_capabilities", "process_risk_requests"} <= after["tables"]
        assert set(PHASE1_INDEXES) <= after["indexes"]
        assert after["tables"] - {"runtime_capabilities", "process_risk_requests"} == before["tables"]
        await asyncio.to_thread(command.check, cfg)                       # models and migration agree after the first upgrade

        await asyncio.to_thread(command.downgrade, cfg, "0004")
        down = await schema_state(url)
        assert down == before                                             # tables, indexes and version: exactly as at 0004

        await asyncio.to_thread(command.upgrade, cfg, "head")
        again = await schema_state(url)
        assert again == after
        await asyncio.to_thread(command.check, cfg)                       # and after up/down/up
    finally:
        admin = await asyncpg.connect(pg.sync_uri)
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()


async def test_migration_downgrade_discards_the_new_tables_even_with_rows_and_keeps_old_data(pg):
    name = f"mig5d_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg.sync_uri)
    await admin.execute(f"CREATE DATABASE {name}")
    await admin.close()
    url = pg.async_url(name)
    cfg = alembic_config(url)
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
        conn = await asyncpg.connect(url.replace("postgresql+asyncpg", "postgresql"))
        await conn.execute("INSERT INTO runtime_capabilities (key, value, updated_at) VALUES ('process_tracking', '{}', now())")
        conversation = uuid.uuid4()
        await conn.execute("INSERT INTO conversations (id, status) VALUES ($1, 'ACTIVE')", conversation)
        await conn.close()
        await asyncio.to_thread(command.downgrade, cfg, "0004")
        state = await schema_state(url)
        assert "runtime_capabilities" not in state["tables"] and state["version"] == "0004"
        conn = await asyncpg.connect(url.replace("postgresql+asyncpg", "postgresql"))
        assert await conn.fetchval("SELECT count(*) FROM conversations WHERE id=$1", conversation) == 1
        await conn.close()
    finally:
        admin = await asyncpg.connect(pg.sync_uri)
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()


async def test_alembic_check_on_the_template_database_clone(pg, db):
    """The `db` fixture is a clone of the database that Alembic built from `head`: the models must still match it."""
    cfg = alembic_config(str(db.engine.url.render_as_string(hide_password=False)))
    await asyncio.to_thread(command.check, cfg)
