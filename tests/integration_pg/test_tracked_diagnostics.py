"""B2 phase 6 on the embedded PostgreSQL (design 14.1, 6.2): the TRACKED_EXECUTIONS causes over real tasks, runs, steps, executions
and process-risk requests, and the reader side of the process-tracking capability against the real `pg_locks`. Real database,
no mocks of the SQL, no CLI."""
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text, update

from app.models.tables import Task, TaskRun
from app.repositories.runtime_capability_repository import RuntimeCapabilityRepository
from app.services.capability_reader import CAPABILITY_KEY, CapabilityReader, advisory_key_parts
from app.services.diagnostics_service import DiagnosticsService, Status, merge_causes
from app.services.tracked_diagnostics import TrackedDiagnosticsReader
from app.validation import policies as P
from tests.helpers import make_step, make_task
from tests.integration_pg.test_process_schema import make_project_task, new_request
from tests.unit.test_diagnostics_service import GOOD_PREFLIGHT, READY, Clock, FakeExecutor, FakeProjects, by_code, workspace

RUNNER = "runner-A"
KEY = 748_201_001


def now():
    return datetime.now(UTC)


async def add_run(db, task, *, status="RUNNING", runner=RUNNER, lease=3600) -> uuid.UUID:
    async with db.transaction() as s:
        run = TaskRun(id=uuid.uuid4(), task_id=task.id, runner_id=runner, status=status,
                      lease_expires_at=now() + timedelta(seconds=lease), heartbeat_at=now(), row_version=0,
                      finished_at=None if status == "RUNNING" else now())
        s.add(run)
        await s.flush()
        return run.id


async def add_row(db, task, run_id, *, detail='{"tracked": true}', runner=RUNNER, age=30, finished=False) -> uuid.UUID:
    row_id = uuid.uuid4()
    async with db.transaction() as s:
        await s.execute(text(
            "INSERT INTO executions (id, task_id, run_id, role, provider, outcome, pid, runner_id, started_at, finished_at, detail) "
            "VALUES (:id, :t, :r, 'TESTER', 'CLAUDE', 'STARTING', 4242, :runner, :started, :fin, CAST(:d AS jsonb))"),
            {"id": row_id, "t": task.id, "r": run_id, "runner": runner, "started": now() - timedelta(seconds=age),
             "fin": now() if finished else None, "d": detail})
    return row_id


async def set_status(db, task, status):
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == task.id).values(status=status))


async def scenario(db, *, task_status="TESTING", step="RUNNING", run="RUNNING", lease=3600, runner=RUNNER, age=30,
                   finished=False, detail='{"tracked": true}'):
    project, task = await make_project_task(db)
    await set_status(db, task, task_status)
    if step is not None:
        await make_step(db, task, step_key="TEST", status=step)
    run_id = await add_run(db, task, status=run, lease=lease) if run is not None else None
    row_id = await add_row(db, task, run_id, runner=runner, age=age, finished=finished, detail=detail)
    return project, task, row_id


async def causes_of(db, project, **reader_kw):
    verdict = await TrackedDiagnosticsReader(db, **reader_kw)(project.id)
    return {c.reason_code: c for c in merge_causes(verdict.causes)}, verdict


# --- 'normal and empty' ----------------------------------------------------------------------------------------------------------
async def test_a_normal_running_execution_is_pass_with_no_cause_and_the_count_in_the_information(db):
    project, task, row = await scenario(db)
    causes, verdict = await causes_of(db, project)
    assert causes == {} and verdict.running == 1
    svc = DiagnosticsService(FakeProjects(), FakeExecutor(), protected=(), get_runtime=lambda: READY,
                             get_preflight=lambda: GOOD_PREFLIGHT, clock=Clock(), read_tracked=TrackedDiagnosticsReader(db))
    item = svc._tracked_item(("ok", verdict))
    assert item.status is Status.PASS and "1 tracked executions running" in item.cause


async def test_the_pre_step_call_with_no_step_yet_is_normal_too(db):
    project, *_ = await scenario(db, step=None)
    causes, verdict = await causes_of(db, project)
    assert causes == {} and verdict.running == 1


async def test_an_empty_project_is_pass_and_a_stale_request_is_a_warning_although_u_is_empty(db):
    project, task = await make_project_task(db)
    causes, verdict = await causes_of(db, project)
    assert causes == {} and verdict.running == 0
    request_id, _, _ = await new_request(db, project=project, task=task)
    causes, _ = await causes_of(db, project)
    assert causes == {}                                                                # a fresh request is not stale
    async with db.transaction() as s:
        await s.execute(text("UPDATE process_risk_requests SET next_attempt_at = now() - make_interval(secs => :s) "
                             "WHERE approval_id = :id"), {"s": P.PROCESS_RISK_REQUEST_STALE_SECONDS + 30, "id": request_id})
    causes, _ = await causes_of(db, project)
    assert set(causes) == {"REQUEST_WITHOUT_WORKER"} and causes["REQUEST_WITHOUT_WORKER"].related_request_ids == (request_id,)
    assert causes["REQUEST_WITHOUT_WORKER"].severity is Status.WARN


# --- the cause matrix over real rows ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs,expected", [
    (dict(task_status="FAILED", run=None), {"TASK_TERMINAL_WITH_UNRESOLVED"}),
    (dict(task_status="COMPLETED", finished=True, run=None), {"TASK_TERMINAL_WITH_UNRESOLVED"}),
    (dict(task_status="CANCELLED"), {"TASK_TERMINAL_WITH_UNRESOLVED"}),
    (dict(run=None), {"NO_OWNING_RUN"}),
    (dict(run="FINISHED"), {"NO_OWNING_RUN"}),
    (dict(lease=-30), {"LEASE_EXPIRED"}),
    (dict(runner="runner-B"), {"RUNNER_MISMATCH"}),
    (dict(task_status="WAITING_APPROVAL", step="NEEDS_MANUAL_RECOVERY", lease=3000), {"TASK_NOT_EXECUTING"}),
    (dict(task_status="WAITING_APPROVAL", step=None, lease=3000), {"TASK_NOT_EXECUTING"}),
    (dict(step="INTERRUPTED"), {"TASK_NOT_EXECUTING"}),
    (dict(age=P.TRACKED_EXECUTION_MAX_AGE_SECONDS + 60), {"EXECUTION_TOO_OLD"}),
    (dict(age=P.TRACKED_EXECUTION_MAX_AGE_SECONDS + 60, lease=-30), {"EXECUTION_TOO_OLD", "LEASE_EXPIRED"}),
    (dict(age=P.TRACKED_EXECUTION_MAX_AGE_SECONDS - 60), set()),
    (dict(finished=True), {"CLOSED_UNPROVEN"}),
], ids=["terminal-open", "terminal-closed-unproven", "terminal-cancelled", "no-run", "run-not-running", "lease-expired",
        "runner-mismatch", "parked-waiting-approval-valid-lease", "waiting-approval-no-step", "step-interrupted", "too-old",
        "too-old-and-lease-expired", "long-but-within-limit", "closed-unproven"])
async def test_the_row_causes_of_one_tracked_row(db, kwargs, expected):
    project, *_ = await scenario(db, **kwargs)
    causes, verdict = await causes_of(db, project)
    assert set(causes) == expected
    assert all(c.severity is Status.WARN and not c.blocks_execution for c in causes.values())
    assert verdict.running == (1 if not expected else 0)


async def test_a_terminal_task_with_an_anomaly_row_has_the_terminal_cause_and_the_fail_anomaly_cause(db):
    project, task, _ = await scenario(db, task_status="FAILED", run=None, detail='{"tracked": false}', finished=True)
    causes, _ = await causes_of(db, project)
    assert set(causes) == {"TASK_TERMINAL_WITH_UNRESOLVED", "TRACKED_EXECUTION_ANOMALY"}
    anomaly = causes["TRACKED_EXECUTION_ANOMALY"]
    assert anomaly.severity is Status.FAIL and anomaly.blocks_execution is False and anomaly.anomalies_returned == 1
    assert anomaly.anomalies[0].anomaly_kind == "OTHER_SCHEMA_INVALID" and anomaly.anomalies_total == 1
    assert anomaly.anomalies_total_is_exact is True and anomaly.may_be_permanently_stuck is False
    assert merge_causes(causes.values())[0].reason_code == "TRACKED_EXECUTION_ANOMALY"          # FAIL first


async def test_an_anomaly_in_the_project_makes_the_normal_row_of_another_task_not_normal(db):
    project, task, _ = await scenario(db)
    second = await make_task(db)
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == second.id).values(project_id=project.id, status="FAILED"))
    await add_row(db, second, None, detail='{"tracked": false}', finished=True)
    causes, verdict = await causes_of(db, project)
    assert "TRACKED_EXECUTION_ANOMALY" in causes and verdict.running == 0


async def test_the_causes_of_another_project_never_appear(db):
    project, *_ = await scenario(db, run=None)
    stranger, *_ = await scenario(db, run=None)
    causes, _ = await causes_of(db, project)
    assert set(causes) == {"NO_OWNING_RUN"}
    assert causes["NO_OWNING_RUN"].related_execution_ids != ()
    other, _ = await causes_of(db, stranger)
    assert set(other["NO_OWNING_RUN"].related_execution_ids).isdisjoint(causes["NO_OWNING_RUN"].related_execution_ids)


# --- request causes over real requests ----------------------------------------------------------------------------------------
async def make_request(db, project, task, **set_sql):
    request_id, _, _ = await new_request(db, project=project, task=task)
    if set_sql:
        assigns = ", ".join(f"{k} = {v}" for k, v in set_sql.items())
        async with db.transaction() as s:
            await s.execute(text(f"UPDATE process_risk_requests SET {assigns} WHERE approval_id = :id"), {"id": request_id})
    return request_id


async def test_a_verifying_request_claimed_too_long_ago_is_a_claim_timeout(db):
    project, task = await make_project_task(db)
    secs = P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS + 30
    request_id = await make_request(db, project, task, status="'VERIFYING'", claim_generation="1",
                                    claimed_by_incarnation=f"'{uuid.uuid4()}'", claimed_at=f"now() - interval '{secs} seconds'")
    causes, _ = await causes_of(db, project)
    assert set(causes) == {"REQUEST_CLAIM_TIMEOUT"} and causes["REQUEST_CLAIM_TIMEOUT"].related_request_ids == (request_id,)


async def test_retryable_at_the_attempt_limit_minus_one_is_retry_stuck_and_below_it_is_not(db):
    project, task = await make_project_task(db)
    await make_request(db, project, task, status="'RETRYABLE'", attempts=str(P.PROCESS_RISK_MAX_ATTEMPTS - 2))
    assert (await causes_of(db, project))[0] == {}
    project2, task2 = await make_project_task(db)
    request_id = await make_request(db, project2, task2, status="'RETRYABLE'", attempts=str(P.PROCESS_RISK_MAX_ATTEMPTS - 1))
    causes, _ = await causes_of(db, project2)
    assert set(causes) == {"REQUEST_RETRY_STUCK"} and causes["REQUEST_RETRY_STUCK"].related_request_ids == (request_id,)


async def test_an_inconclusive_latest_request_is_retry_stuck_while_u_is_non_empty_only(db):
    project, task, _ = await scenario(db, task_status="FAILED", run=None)
    request_id = await make_request(db, project, task, status="'NEEDS_REDECISION'", reason="'PROCESS_RECHECK_INCONCLUSIVE'",
                                    finished_at="now()")
    causes, _ = await causes_of(db, project)
    assert {"REQUEST_RETRY_STUCK", "TASK_TERMINAL_WITH_UNRESOLVED"} == set(causes)
    assert causes["REQUEST_RETRY_STUCK"].related_request_ids == (request_id,)
    project2, task2 = await make_project_task(db)
    await make_request(db, project2, task2, status="'NEEDS_REDECISION'", reason="'PROCESS_RECHECK_INCONCLUSIVE'",
                       finished_at="now()")
    assert (await causes_of(db, project2))[0] == {}                                  # U is empty: no stuck retry to report


async def test_several_causes_at_once_are_each_listed_once_ordered_by_severity_then_code(db):
    project, task, _ = await scenario(db, run=None)
    second = await add_row(db, task, None, finished=True)                            # CLOSED_UNPROVEN on the same task
    anomaly = await add_row(db, task, None, detail='{"tracked": false}', finished=True)
    request_id = await make_request(db, project, task, next_attempt_at=f"now() - interval '{P.PROCESS_RISK_REQUEST_STALE_SECONDS + 60} seconds'")
    verdict = await TrackedDiagnosticsReader(db)(project.id)
    merged = merge_causes(verdict.causes)
    assert [c.reason_code for c in merged] == ["TRACKED_EXECUTION_ANOMALY", "CLOSED_UNPROVEN", "NO_OWNING_RUN",
                                               "REQUEST_WITHOUT_WORKER"]
    assert merged[1].related_execution_ids == (second,) and merged[0].related_execution_ids == (anomaly,)
    assert merged[3].related_request_ids == (request_id,)


@pytest.mark.parametrize("state,expected", [("UNAVAILABLE", True), ("UNSUPPORTED_PLATFORM", True), ("SUPPORTED", False),
                                            ("STATUS_UNKNOWN", False)])
async def test_an_open_row_with_a_non_supporting_capability_is_a_mismatch(db, state, expected):
    project, *_ = await scenario(db)

    async def reader():
        return SimpleNamespace(state=state)
    causes, _ = await causes_of(db, project, read_capability=reader)
    assert ("CAPABILITY_MISMATCH" in causes) is expected


async def test_a_failing_capability_reader_only_skips_the_mismatch_cause(db):
    project, *_ = await scenario(db)

    async def broken():
        raise RuntimeError("boom")
    causes, verdict = await causes_of(db, project, read_capability=broken)
    assert causes == {} and verdict.running == 1


# --- the reader of the capability (6.2) against the real pg_locks ---------------------------------------------------------------
class Held:
    """A session advisory lock taken with the REAL key on its own connection (what the Runner's `_lock_conn` does)."""

    def __init__(self, db, key=KEY):
        self.db, self.key = db, key

    async def __aenter__(self):
        self.conn = await self.db.engine.connect()
        self.pid = (await self.conn.execute(text("select pg_backend_pid()"))).scalar()
        assert (await self.conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": self.key})).scalar()
        return self

    async def release(self):
        await self.conn.execute(text("select pg_advisory_unlock(:k)"), {"k": self.key})

    async def __aexit__(self, *exc):
        await self.conn.close()


async def publish(db, *, state="SUPPORTED", pid=None, reason=None, age=0.0):
    value = {"incarnation_id": uuid.uuid4().hex, "lock_backend_pid": pid, "host": "h", "pid": 1, "reason_code": reason,
             "state": state, "checked_at": now().isoformat()}
    async with db.transaction() as s:
        await RuntimeCapabilityRepository(s).upsert(CAPABILITY_KEY, value, now=now() - timedelta(seconds=age))


def reader_for(db, **kw):
    return CapabilityReader(db, KEY, platform_supported=lambda: True, **kw)


async def test_the_session_advisory_lock_appears_in_pg_locks_with_the_documented_mapping(db):
    """The `pg_locks` mapping the design marks as unverified: classid = key >> 32, objid = key & 0xFFFFFFFF, objsubid = 1,
    granted, in the current database, held by the backend that took it."""
    classid, objid = advisory_key_parts(KEY)
    assert (classid, objid) == (KEY >> 32, KEY & 0xFFFFFFFF)
    async with Held(db) as held:
        async with db.transaction() as s:
            rows = (await s.execute(text(
                "SELECT classid::bigint, objid::bigint, objsubid, granted, pid, "
                "database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "FROM pg_locks WHERE locktype = 'advisory' AND pid = :pid"), {"pid": held.pid})).all()
    assert [tuple(r) for r in rows] == [(classid, objid, 1, True, held.pid, True)]


@pytest.mark.parametrize("key", [748_201_001, 2**40 + 5, -3, 2**63 - 1])
async def test_the_mapping_holds_for_a_key_above_32_bits_and_for_a_negative_key(db, key):
    classid, objid = advisory_key_parts(key)
    async with Held(db, key) as held:
        async with db.transaction() as s:
            found = (await s.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND objsubid = 1 "
                "AND classid::bigint = :c AND objid::bigint = :o AND pid = :pid"), {"c": classid, "o": objid, "pid": held.pid})).scalar()
    assert found == 1


async def test_a_fresh_row_of_the_lock_holder_is_trusted_and_carries_its_reason(db):
    async with Held(db) as held:
        await publish(db, state="SUPPORTED", pid=held.pid)
        reading = await reader_for(db)()
        assert (reading.state, reading.source) == ("SUPPORTED", "DATABASE")
        await publish(db, state="UNAVAILABLE", pid=held.pid, reason="PROCESS_TRACKING_UNAVAILABLE")
        reading = await reader_for(db)()
        assert (reading.state, reading.reason_code) == ("UNAVAILABLE", "PROCESS_TRACKING_UNAVAILABLE")


async def test_the_stored_unknown_state_is_never_trusted(db):
    async with Held(db) as held:
        await publish(db, state="UNKNOWN", pid=held.pid)
        assert (await reader_for(db)()).state == "STATUS_UNKNOWN"


async def test_a_row_of_a_dead_incarnation_is_not_trusted_while_nobody_or_somebody_else_holds_the_lock(db):
    await publish(db, state="SUPPORTED", pid=12345)
    assert (await reader_for(db)()).state == "STATUS_UNKNOWN"                     # nobody holds the lock
    async with Held(db) as held:
        await publish(db, state="SUPPORTED", pid=held.pid + 1)
        assert (await reader_for(db)()).state == "STATUS_UNKNOWN"                 # the holder is not the one the row names
        await publish(db, state="SUPPORTED", pid=held.pid)
        assert (await reader_for(db)()).state == "SUPPORTED"
        await held.release()
        assert (await reader_for(db)()).state == "STATUS_UNKNOWN"                 # the Runner is gone: a TTL-fresh row is not enough


async def test_a_stale_row_is_not_trusted_and_the_ttl_boundary_is_inclusive(db):
    async with Held(db) as held:
        await publish(db, state="SUPPORTED", pid=held.pid, age=P.CAPABILITY_TTL_SECONDS + 5)
        assert (await reader_for(db)()).state == "STATUS_UNKNOWN"
        await publish(db, state="SUPPORTED", pid=held.pid, age=P.CAPABILITY_TTL_SECONDS - 5)
        assert (await reader_for(db)()).state == "SUPPORTED"
        short = CapabilityReader(db, KEY, platform_supported=lambda: True, ttl_seconds=1)
        await publish(db, state="SUPPORTED", pid=held.pid, age=3)
        assert (await short()).state == "STATUS_UNKNOWN"


async def test_no_row_and_a_corrupt_value_are_status_unknown(db):
    assert (await reader_for(db)()).state == "STATUS_UNKNOWN"
    async with Held(db) as held:
        async with db.transaction() as s:
            await RuntimeCapabilityRepository(s).upsert(CAPABILITY_KEY, {"state": "SUPPORTED", "lock_backend_pid": "x"})
        assert (await reader_for(db)()).state == "STATUS_UNKNOWN"
        async with db.transaction() as s:
            await RuntimeCapabilityRepository(s).upsert(CAPABILITY_KEY, {"state": "WEIRD", "lock_backend_pid": held.pid})
        assert (await reader_for(db)()).state == "STATUS_UNKNOWN"


async def test_the_unsupported_platform_is_decided_locally_with_no_database_and_a_running_runner_reads_its_own_memory(db):
    class NoDb:
        def transaction(self):
            raise AssertionError("the local decision must not touch the database")
    local = CapabilityReader(NoDb(), KEY, platform_supported=lambda: False)
    assert (await local()).state == "UNSUPPORTED_PLATFORM" and (await local()).source == "LOCAL_PLATFORM"
    own = CapabilityReader(NoDb(), KEY, own_state=lambda: "UNAVAILABLE", platform_supported=lambda: True)
    assert (await own()).state == "UNAVAILABLE" and (await own()).source == "OWN_MEMORY"
    unknown = CapabilityReader(NoDb(), KEY, own_state=lambda: "UNKNOWN")
    assert (await unknown()).state == "STATUS_UNKNOWN"


async def test_a_failing_database_is_status_unknown_never_a_trusted_old_value(db):
    class Broken:
        def transaction(self):
            raise RuntimeError("connection lost")
    assert (await CapabilityReader(Broken(), KEY, platform_supported=lambda: True)()).state == "STATUS_UNKNOWN"


async def test_the_reader_never_writes_the_capability_row(db):
    async with Held(db) as held:
        await publish(db, state="SUPPORTED", pid=held.pid)
        async with db.transaction() as s:
            before = (await s.execute(text("SELECT value::text, updated_at FROM runtime_capabilities"))).all()
        await reader_for(db)()
        async with db.transaction() as s:
            assert (await s.execute(text("SELECT value::text, updated_at FROM runtime_capabilities"))).all() == before


# --- the whole diagnosis with the real readers ------------------------------------------------------------------------------------
async def test_the_diagnosis_over_the_real_readers_gives_the_process_tracking_and_tracked_items(db, tmp_path):
    project, *_ = await scenario(db, run=None)
    projects = FakeProjects()
    row = projects.add(workspace(tmp_path))
    async with Held(db) as held:
        await publish(db, state="UNAVAILABLE", pid=held.pid, reason="PROCESS_TRACKING_UNAVAILABLE")
        capability = reader_for(db)
        svc = DiagnosticsService(projects, FakeExecutor(), protected=(), get_runtime=lambda: READY,
                                 get_preflight=lambda: GOOD_PREFLIGHT, clock=Clock(), read_capability=capability,
                                 read_tracked=TrackedDiagnosticsReader(db, read_capability=capability))
        report = await svc.diagnose(row.id)
    items = by_code(report)
    assert items["PROCESS_TRACKING"].status is Status.FAIL and items["PROCESS_TRACKING"].blocks_features == ("TEST_RUN",)
    assert not report.can_start and "PROCESS_TRACKING" in report.blockers
    assert items["TRACKED_EXECUTIONS"].status is Status.PASS       # a different project: nothing of the scenario project
    assert project.id != row.id
