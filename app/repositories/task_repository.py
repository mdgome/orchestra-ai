"""Tasks, runs (leases), steps, executions, transitions. Repositories never commit."""
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Execution, StateTransition, Task, TaskRun, TaskStep
from app.repositories import tracked_sql as S
from app.validation.policies import CANDIDATE_MAX_SCANNED_ROWS as P_CANDIDATE_MAX_SCANNED_ROWS
from app.validation.policies import CANDIDATE_PAGE_ROWS as P_CANDIDATE_PAGE_ROWS
from app.validation.policies import CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS as P_CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS
from app.validation.policies import CANDIDATE_SCAN_MAX_SECONDS as P_CANDIDATE_SCAN_MAX_SECONDS
from app.validation.policies import DISPOSITION_KIND_RISK_ACCEPTED as P_DISPOSITION
from app.validation.policies import PROCESS_RISK_TARGET_LOCK_SECONDS as P_RISK_LOCK_SECONDS
from app.validation.policies import PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS as P_RISK_LOCK_STATEMENT_SECONDS
from app.validation.policies import PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS as P_RISK_LOCK_WAIT_SECONDS


# A row without a pid may be closed as `NEVER_RAN` only while its `detail` does not record that a spawned process was not proven
# gone (the SQL mirror of `lease_service.spawned_process_unproven`); used by every guarded NEVER_RAN write.
NEVER_RAN_EVIDENCE_GUARD_SQL = (
    "COALESCE(detail ->> 'lifecycle', '') <> 'TERMINATION_UNCONFIRMED' "
    "AND COALESCE(detail ->> 'tree', '') NOT IN ('NOT_EMPTY', 'UNKNOWN') "
    "AND COALESCE(detail ->> 'reason', '') NOT IN ('RESULT_MISSING', 'LIFECYCLE_MISSING')")


@dataclass(frozen=True)
class TrackedRow:
    """One `executions` row as the tracked-row queries select it: `detail` stays TEXT (`detail::text`) and is decoded only by
    the classifier (strict decode, design 7.4)."""
    id: uuid.UUID
    task_id: uuid.UUID
    pid: int | None
    process_create_time: float | None
    finished_at: datetime | None
    detail_text: str | None


@dataclass(frozen=True)
class ExecutionFact:
    """The identifiers and times of one `executions` row for the project diagnostics (14.1): no `detail`, no output."""
    id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID | None
    runner_id: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class ExecutionFact:
    """The identifiers and times of one `executions` row for the project diagnostics (14.1): no `detail`, no output."""
    id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID | None
    runner_id: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class RiskScan:
    """Result of `risk_batch_targets`: the states of design 7.4 (TARGETS_READY, NO_ELIGIBLE_TARGETS, SCAN_LIMIT_REACHED,
    QUERY_FAILED); `cap` is ROW or TIME for a limit, `failure` is PAGE_TIMEOUT, TRANSIENT or PERMANENT for QUERY_FAILED."""
    state: str
    ids: tuple[uuid.UUID, ...] = ()
    cap: str | None = None
    failure: str | None = None
    scanned: int = 0
    pids: tuple[int | None, ...] = field(default=(), compare=False)         # the pid of each id, same order


class RiskLockFailure(Exception):
    """The risk-target lock phase failed; `reason` is the external reason code, `internal` the sub-reason (never an API value)."""

    def __init__(self, reason: str, internal: str):
        super().__init__(reason)
        self.reason, self.internal = reason, internal


class TaskRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def add(self, task: Task) -> Task:
        self.s.add(task)
        await self.s.flush()
        return task

    async def get(self, task_id: uuid.UUID) -> Task | None:
        return (await self.s.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()

    async def lock(self, task_id: uuid.UUID) -> Task | None:
        """SELECT ... FOR UPDATE: the serialisation point for lease acquisition and state changes."""
        return (await self.s.execute(select(Task).where(Task.id == task_id).with_for_update())).scalar_one_or_none()

    async def list(self, *, status: str | None = None, limit: int = 50, offset: int = 0,
                   project_id: uuid.UUID | None = None) -> list[Task]:
        stmt = select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
        if status:
            stmt = stmt.where(Task.status == status)
        if project_id is not None:
            stmt = stmt.where(Task.project_id == project_id)
        return list((await self.s.execute(stmt)).scalars())

    async def tasks_in_states(self, states: tuple[str, ...]) -> list[Task]:
        return list((await self.s.execute(select(Task).where(Task.status.in_(states)))).scalars())

    async def add_transition(self, task_id: uuid.UUID, to_state: str, from_state: str | None = None,
                             reason: str | None = None, event: dict | None = None, kind: str = "TASK_STATE") -> None:
        self.s.add(StateTransition(task_id=task_id, kind=kind, from_state=from_state, to_state=to_state,
                                   reason=reason, event=event))
        await self.s.flush()

    async def list_transitions(self, task_id: uuid.UUID) -> list[StateTransition]:
        rows = await self.s.execute(
            select(StateTransition).where(StateTransition.task_id == task_id).order_by(StateTransition.id))
        return list(rows.scalars())

    # --- runs / lease ---------------------------------------------------------------------
    async def running_run(self, task_id: uuid.UUID) -> TaskRun | None:
        return (await self.s.execute(
            select(TaskRun).where(TaskRun.task_id == task_id, TaskRun.status == "RUNNING"))).scalar_one_or_none()

    async def get_run(self, run_id: uuid.UUID) -> TaskRun | None:
        return (await self.s.execute(select(TaskRun).where(TaskRun.id == run_id))).scalar_one_or_none()

    async def insert_run(self, task_id: uuid.UUID, runner_id: str, now: datetime, ttl_seconds: int) -> TaskRun:
        run = TaskRun(id=uuid.uuid4(), task_id=task_id, runner_id=runner_id, status="RUNNING",
                      lease_expires_at=now + timedelta(seconds=ttl_seconds), heartbeat_at=now, row_version=0)
        self.s.add(run)
        await self.s.flush()
        return run

    async def close_run(self, run_id: uuid.UUID, status: str, reason: str, expected_status: str = "RUNNING") -> bool:
        result = await self.s.execute(
            update(TaskRun).where(TaskRun.id == run_id, TaskRun.status == expected_status)
            .values(status=status, finished_at=func.now(), finish_reason=reason, row_version=TaskRun.row_version + 1))
        return result.rowcount == 1

    async def heartbeat(self, run_id: uuid.UUID, runner_id: str, now: datetime, ttl_seconds: int) -> bool:
        """Extends the lease only for the owner and only while it has not expired."""
        result = await self.s.execute(
            update(TaskRun)
            .where(TaskRun.id == run_id, TaskRun.runner_id == runner_id, TaskRun.status == "RUNNING",
                   TaskRun.lease_expires_at > now)
            .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=ttl_seconds),
                    row_version=TaskRun.row_version + 1))
        return result.rowcount == 1

    async def running_runs(self) -> list[TaskRun]:
        return list((await self.s.execute(select(TaskRun).where(TaskRun.status == "RUNNING"))).scalars())

    # --- steps -----------------------------------------------------------------------------
    async def add_step(self, step: TaskStep) -> TaskStep:
        self.s.add(step)
        await self.s.flush()
        return step

    async def get_step(self, step_id: uuid.UUID) -> TaskStep | None:
        return (await self.s.execute(select(TaskStep).where(TaskStep.id == step_id))).scalar_one_or_none()

    async def has_steps(self, task_id: uuid.UUID, step_keys: tuple[str, ...]) -> bool:
        """Is there any step row of these kinds? (a mutating step begin bumps the project's write epoch)"""
        return (await self.s.execute(select(TaskStep.id).where(
            TaskStep.task_id == task_id, TaskStep.step_key.in_(step_keys)).limit(1))).first() is not None

    async def steps_for_task(self, task_id: uuid.UUID) -> list[TaskStep]:
        rows = await self.s.execute(
            select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.created_at, TaskStep.step_attempt))
        return list(rows.scalars())

    async def latest_step(self, task_id: uuid.UUID, step_key: str, *, plan_revision: int | None = None,
                          workflow_iteration: int | None = None) -> TaskStep | None:
        stmt = select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step_key == step_key)
        if plan_revision is not None:
            stmt = stmt.where(TaskStep.plan_revision == plan_revision)
        if workflow_iteration is not None:
            stmt = stmt.where(TaskStep.workflow_iteration == workflow_iteration)
        stmt = stmt.order_by(TaskStep.created_at.desc(), TaskStep.step_attempt.desc()).limit(1)
        return (await self.s.execute(stmt)).scalar_one_or_none()

    async def max_attempt(self, task_id: uuid.UUID, plan_revision: int, workflow_iteration: int, step_key: str) -> int:
        value = (await self.s.execute(
            select(func.max(TaskStep.step_attempt)).where(
                TaskStep.task_id == task_id, TaskStep.plan_revision == plan_revision,
                TaskStep.workflow_iteration == workflow_iteration, TaskStep.step_key == step_key))).scalar()
        return value or 0

    async def failed_attempts(self, task_id: uuid.UUID, plan_revision: int, workflow_iteration: int,
                              step_key: str) -> int:
        """Technical failures of one (revision, iteration, step): FAILED / INTERRUPTED / NEEDS_MANUAL_RECOVERY.
        Human Gate waits and successful re-runs never consume the technical-retry budget."""
        return (await self.s.execute(
            select(func.count()).select_from(TaskStep).where(
                TaskStep.task_id == task_id, TaskStep.plan_revision == plan_revision,
                TaskStep.workflow_iteration == workflow_iteration, TaskStep.step_key == step_key,
                TaskStep.status.in_(("FAILED", "INTERRUPTED", "NEEDS_MANUAL_RECOVERY"))))).scalar() or 0

    async def count_steps_with_error(self, task_id: uuid.UUID, plan_revision: int, workflow_iteration: int,
                                     step_key: str, error: str) -> int:
        return (await self.s.execute(
            select(func.count()).select_from(TaskStep).where(
                TaskStep.task_id == task_id, TaskStep.plan_revision == plan_revision,
                TaskStep.workflow_iteration == workflow_iteration, TaskStep.step_key == step_key,
                TaskStep.error == error))).scalar() or 0

    # --- executions ------------------------------------------------------------------------
    async def add_execution(self, execution: Execution) -> Execution:
        self.s.add(execution)
        await self.s.flush()
        return execution

    async def get_execution(self, execution_id: uuid.UUID) -> Execution | None:
        return (await self.s.execute(select(Execution).where(Execution.id == execution_id))).scalar_one_or_none()

    async def executions_for_task(self, task_id: uuid.UUID) -> list[Execution]:
        rows = await self.s.execute(
            select(Execution).where(Execution.task_id == task_id).order_by(Execution.started_at, Execution.id))
        return list(rows.scalars())

    async def executions_for_request(self, request_id: uuid.UUID) -> list[Execution]:
        rows = await self.s.execute(select(Execution).where(Execution.request_id == request_id))
        return list(rows.scalars())

    async def unterminated_executions(self, task_id: uuid.UUID) -> list[Execution]:
        """CLI executions that were started but never reported a finish."""
        rows = await self.s.execute(
            select(Execution).where(Execution.task_id == task_id, Execution.finished_at.is_(None)))
        return list(rows.scalars())

    # --- tracked executions (B2, design 6.1, 7.4) -----------------------------------------------------------------
    @staticmethod
    def _tracked_rows(result) -> list[TrackedRow]:
        return [TrackedRow(*row) for row in result.all()]

    async def unresolved_tracked_executions_for_project(self, project_id: uuid.UUID, limit: int) -> list[TrackedRow]:
        """Set U of one project, at most `limit` rows ascending by id. PRIVATE to `has_unresolved_or_anomalous_for_project`:
        no other caller (architecture test); a CLEAN decision always asks both sets through that one function."""
        return self._tracked_rows(await self.s.execute(
            text(S.PROJECT_U_QUERY_SQL + "\nLIMIT :bounded_limit"), {"project_id": project_id, "bounded_limit": limit}))

    async def anomalous_tracked_executions_for_project(self, project_id: uuid.UUID, limit: int) -> list[TrackedRow]:
        """Membership of set A of one project (the caller classifies every row). Same restriction as the U query."""
        return self._tracked_rows(await self.s.execute(
            text(S.PROJECT_A_QUERY_SQL + "\nLIMIT :bounded_limit"), {"project_id": project_id, "bounded_limit": limit}))

    async def execution_facts(self, ids: list[uuid.UUID]) -> list["ExecutionFact"]:
        """The columns the project diagnostics need of the rows it already knows by id (never `detail`)."""
        if not ids:
            return []
        rows = await self.s.execute(select(Execution.id, Execution.task_id, Execution.run_id, Execution.runner_id,
                                           Execution.started_at, Execution.finished_at).where(Execution.id.in_(ids)))
        return [ExecutionFact(*row) for row in rows.all()]

    async def task_statuses(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        rows = await self.s.execute(select(Task.id, Task.status).where(Task.id.in_(ids)))
        return {row[0]: row[1] for row in rows.all()}

    async def runs_by_ids(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, TaskRun]:
        if not ids:
            return {}
        rows = await self.s.execute(select(TaskRun).where(TaskRun.id.in_(ids)))
        return {run.id: run for run in rows.scalars()}

    async def execution_facts(self, ids: list[uuid.UUID]) -> list["ExecutionFact"]:
        """The columns the project diagnostics need of the rows it already knows by id (never `detail`)."""
        if not ids:
            return []
        rows = await self.s.execute(select(Execution.id, Execution.task_id, Execution.run_id, Execution.runner_id,
                                           Execution.started_at, Execution.finished_at).where(Execution.id.in_(ids)))
        return [ExecutionFact(*row) for row in rows.all()]

    async def task_statuses(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        rows = await self.s.execute(select(Task.id, Task.status).where(Task.id.in_(ids)))
        return {row[0]: row[1] for row in rows.all()}

    async def runs_by_ids(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, TaskRun]:
        if not ids:
            return {}
        rows = await self.s.execute(select(TaskRun).where(TaskRun.id.in_(ids)))
        return {run.id: run for run in rows.scalars()}

    async def unresolved_for_task(self, task_id: uuid.UUID) -> list[TrackedRow]:
        """Task-local view (report, diagnostics detail of one Task). Never used by a CLEAN decision."""
        return self._tracked_rows(await self.s.execute(text(S.TASK_U_QUERY_SQL), {"task_id": task_id}))

    async def tracked_row(self, execution_id: uuid.UUID) -> TrackedRow | None:
        rows = self._tracked_rows(await self.s.execute(text(S.TRACKED_ROW_BY_ID_SQL), {"id": execution_id}))
        return rows[0] if rows else None

    async def lock_executions_ordered(self, ids: list[uuid.UUID]) -> list[uuid.UUID]:
        """Locks the rows of `ids` that exist with ONE `ORDER BY id ... FOR UPDATE` statement (the ids are sorted first) and
        returns the locked ids in ascending order. Every multi-row execution writer (the takeover, the worker result
        transaction) goes through here, so all of them take row locks in the same deterministic order (design 6.1, 7.5
        item 15). One of the two methods of this class that issue FOR UPDATE on executions."""
        wanted = sorted(set(ids))
        if not wanted:
            return []
        stmt = text(S.LOCK_EXECUTIONS_ORDERED_SQL).bindparams(bindparam("ids", type_=ARRAY(PG_UUID(as_uuid=True))))
        locked = [row[0] for row in (await self.s.execute(stmt, {"ids": wanted})).all()]
        if locked != sorted(locked):
            raise RuntimeError("execution row locks were not returned in ascending id order")
        return locked

    async def set_tracked_identity(self, execution_id: uuid.UUID, pid: int, create_time: float) -> bool:
        """The fenced identity write of a tracked command: only while the row is still open (design 5.1 step 5)."""
        result = await self.s.execute(
            update(Execution).where(Execution.id == execution_id, Execution.finished_at.is_(None))
            .values(pid=pid, process_create_time=create_time))
        return result.rowcount == 1

    async def write_tracked_result(self, execution_id: uuid.UUID, *, merge: dict, outcome: str | None, exit_code: int | None,
                                   close: bool, terminated: bool = False, stdout_tail: str | None = None,
                                   stderr_tail: str | None = None) -> bool:
        """The caller-`finally` write of design 6.1: ONE UPDATE, lease-independent, no Task lock, `detail` JSONB-MERGED
        (`detail || :merge`) so `tracked`, `v`, `runner_process` and `checkpoint_step_key` survive. `close=True` sets
        `finished_at` together with the merged proof; `close=False` (TERMINATION_UNCONFIRMED, RESULT_MISSING) leaves the row
        open. Guard `finished_at IS NULL`: a rowcount of 0 is never success, the caller re-reads and classifies the row."""
        sets = ["detail = detail || CAST(:merge AS jsonb)"]
        params: dict = {"id": execution_id, "merge": json.dumps(merge)}
        if outcome is not None:
            sets.append("outcome = :outcome")
            params["outcome"] = outcome
        if exit_code is not None:
            sets.append("exit_code = :exit_code")
            params["exit_code"] = exit_code
        if stdout_tail is not None:
            sets.append("stdout_tail = :stdout_tail")
            params["stdout_tail"] = stdout_tail
        if stderr_tail is not None:
            sets.append("stderr_tail = :stderr_tail")
            params["stderr_tail"] = stderr_tail
        if close:
            sets.append("finished_at = now()")
            if terminated:
                sets.append("terminated_at = now()")
        result = await self.s.execute(
            text(f"UPDATE executions SET {', '.join(sets)} WHERE id = :id AND finished_at IS NULL"), params)
        return result.rowcount == 1

    # --- keyset pages (design 7.4 'Candidate scan'); the caller sets the transaction-local statement timeout -------------
    async def sweep_page(self, last_seen_id: uuid.UUID | None, page_size: int) -> list[TrackedRow]:
        """`<SWEEP_PAGE>`: judgeable U rows and all A rows of every project, one ascending id stream."""
        if last_seen_id is None:
            return self._tracked_rows(await self.s.execute(text(S.SWEEP_PAGE_FIRST_SQL), {"page_size": page_size}))
        return self._tracked_rows(await self.s.execute(
            text(S.SWEEP_PAGE_NEXT_SQL), {"page_size": page_size, "last_seen_id": last_seen_id}))

    async def set_local_statement_timeout(self, milliseconds: int) -> None:
        """Transaction-local (`set_config(..., true)`), the mechanism of recovery_transaction.py."""
        await self.s.execute(text("select set_config('statement_timeout', :ms, true)"), {"ms": str(int(milliseconds))})

    # --- sweeper / worker closing writes (single-row, guarded; design 6.1, 7.4 'Orphan sweeper', 7.5 item 7) --------------
    async def write_proof_to_unresolved(self, row: TrackedRow, *, merge: dict, close: bool) -> bool:
        """A positive proof on a U row, JSONB-merged. Guard: the row is still in U, still open (`close`) or still closed
        (not `close`), and its recorded identity is the one that was judged. rowcount 0 = someone else settled it."""
        sets = ["detail = detail || CAST(:merge AS jsonb)"]
        if close:
            sets += ["finished_at = now()", "outcome = 'ERROR'"] + (["terminated_at = now()"] if row.pid is not None else [])
        never_ran_guard = f" AND {NEVER_RAN_EVIDENCE_GUARD_SQL}" if row.pid is None and merge.get("proof") == "NEVER_RAN" else ""
        result = await self.s.execute(text(
            f"UPDATE executions SET {', '.join(sets)} WHERE id = :id AND ({S.U_SQL}) AND finished_at IS "
            f"{'NULL' if close else 'NOT NULL'} AND pid IS NOT DISTINCT FROM :pid "
            "AND process_create_time IS NOT DISTINCT FROM :ct" + never_ran_guard),
            {"id": row.id, "merge": json.dumps(merge), "pid": row.pid, "ct": row.process_create_time})
        return result.rowcount == 1

    async def settle_anomaly(self, row: TrackedRow, *, proof: str, termination: str | None, settlement: dict,
                             original_keys: list[str]) -> bool:
        """The anomaly write of the sweeper: a compare-and-swap on the exact `detail` that was read AND on membership of A.
        ONE UPDATE: `finished_at` if NULL, the positive proof, `v = 1` and a valid marker, the invalid keys MOVED into
        `anomaly_original` (an exact copy) beside `anomaly_settlement`. The recorded identity (`pid`, `process_create_time`) is part
        of the compare-and-swap: a `NEVER_RAN` verdict judged on a row without a pid can never close a row whose owner has
        committed its identity since (the owner's own identity write is what makes a resume possible)."""
        merge = {"tracked": True, "v": 1, "proof": proof, "tree": "EMPTY", "anomaly_settlement": settlement}
        if termination is not None:
            merge["termination"] = termination
        result = await self.s.execute(text(
            "UPDATE executions SET finished_at = COALESCE(finished_at, now()), outcome = COALESCE(outcome, 'ERROR'), "
            "detail = ((detail - CAST(:keys AS text[])) || CAST(:merge AS jsonb) || jsonb_build_object('anomaly_original', "
            "(SELECT COALESCE(jsonb_object_agg(k, v), '{}'::jsonb) FROM jsonb_each(detail) AS e(k, v) "
            "WHERE k = ANY(CAST(:keys AS text[]))))) "
            "WHERE id = :id AND detail = CAST(:seen AS jsonb) AND pid IS NOT DISTINCT FROM :pid "
            f"AND process_create_time IS NOT DISTINCT FROM :ct AND ({S.ANOMALY_SQL})"),
            {"id": row.id, "seen": row.detail_text, "keys": original_keys, "merge": json.dumps(merge),
             "pid": row.pid, "ct": row.process_create_time})
        return result.rowcount == 1

    async def write_disposition(self, row: TrackedRow, record: dict) -> bool:
        """Design 7.5 item 7: `disposition` + `reason_code` + `risk_acceptance`, JSONB-merged. An OPEN row also gets
        `finished_at` and `outcome = ERROR` (never `terminated_at`, never a proof). Guard: neither proof nor disposition (and
        `finished_at IS NULL` for the closing fields). 0 rows = someone else resolved it: the caller re-reads."""
        merge = {"disposition": P_DISPOSITION, "reason_code": P_DISPOSITION, "risk_acceptance": record}
        open_row = row.finished_at is None
        sets = ["detail = detail || CAST(:merge AS jsonb)"] + (["finished_at = now()", "outcome = 'ERROR'"] if open_row else [])
        result = await self.s.execute(text(
            f"UPDATE executions SET {', '.join(sets)} WHERE id = :id AND (detail ->> 'proof') IS NULL "
            f"AND (detail ->> 'disposition') IS NULL" + (" AND finished_at IS NULL" if open_row else "")),
            {"id": row.id, "merge": json.dumps(merge)})
        return result.rowcount == 1

    async def task_ids_of_project(self, project_id: uuid.UUID, task_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        rows = await self.s.execute(select(Task.id).where(Task.project_id == project_id, Task.id.in_(task_ids)))
        return {r[0] for r in rows}

    # --- risk target scans (design 7.4 'Candidate scan', 7.5 items 4 and 5) ----------------------------------------------
    async def _current_timeouts(self) -> tuple[str, str]:
        row = (await self.s.execute(text("select current_setting('statement_timeout'), current_setting('lock_timeout')"))).one()
        return row[0], row[1]

    async def _restore_timeouts(self, previous: tuple[str, str]) -> None:
        for name, value in zip(("statement_timeout", "lock_timeout"), previous):
            await self.s.execute(text("select set_config(:n, :v, true)"), {"n": name, "v": value})

    async def risk_batch_targets(self, project_id: uuid.UUID, task_id: uuid.UUID, limit: int, classify,
                                 clock=time.monotonic) -> "RiskScan":
        """Design 7.5 item 5: ONE procedure for the decision-time predicate, the options read and the worker pre-check. Two-phase
        keyset scan over the U rows only: phase 1 the resumed Task's own rows (`<RISK_PAGE_OWN>`), phase 2 the other rows of the
        project (`<RISK_PAGE_REST>`, first page of its own), each ascending by id with its own cursor. `decode_error` rows and
        rows Python reclassifies as A are skipped (and counted as scanned). The caller's transaction keeps its own statement
        timeout: it is read before the first page and restored after the last. A failed page aborts that transaction (25P02):
        the result is QUERY_FAILED and the caller must roll back. `classify` is the injected Python classifier
        (`tracked_classifier.classify_row`): a repository does not import a service module."""
        deadline = clock() + P_CANDIDATE_SCAN_MAX_SECONDS
        previous = await self._current_timeouts()
        ids: list[uuid.UUID] = []
        pids: list[int | None] = []
        scanned = 0
        try:
            for first, nxt in ((S.RISK_PAGE_OWN_FIRST_SQL, S.RISK_PAGE_OWN_NEXT_SQL),
                               (S.RISK_PAGE_REST_FIRST_SQL, S.RISK_PAGE_REST_NEXT_SQL)):
                cursor = None
                while True:
                    if len(ids) >= limit:
                        await self._restore_timeouts(previous)
                        return RiskScan("TARGETS_READY", tuple(ids), scanned=scanned, pids=tuple(pids))
                    page_size = min(P_CANDIDATE_PAGE_ROWS, P_CANDIDATE_MAX_SCANNED_ROWS - scanned, limit - len(ids))
                    timeout_ms = int(min(P_CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS, deadline - clock()) * 1000)
                    if page_size <= 0 or timeout_ms < 1:
                        await self._restore_timeouts(previous)
                        return RiskScan("TARGETS_READY" if ids else "SCAN_LIMIT_REACHED", tuple(ids),
                                        cap="ROW" if page_size <= 0 else "TIME", scanned=scanned, pids=tuple(pids))
                    await self.set_local_statement_timeout(timeout_ms)
                    params = {"project_id": project_id, "task_id": task_id, "page_size": page_size}
                    if cursor is not None:
                        params["last_seen_id"] = cursor
                    page = self._tracked_rows(await self.s.execute(text(nxt if cursor is not None else first), params))
                    for row in page:
                        cursor = row.id
                        scanned += 1
                        if classify(row.id, row.pid, row.finished_at, row.detail_text).is_unresolved:
                            ids.append(row.id)
                            pids.append(row.pid)
                    if len(page) < page_size:
                        break                                    # the end of this phase's set
        except Exception as exc:  # noqa: BLE001 - mapped to a fixed reason; the caller rolls its transaction back
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            failure = "PAGE_TIMEOUT" if sqlstate == "57014" else "TRANSIENT" if sqlstate else "PERMANENT"
            return RiskScan("QUERY_FAILED", (), failure=failure, scanned=scanned)
        await self._restore_timeouts(previous)
        return RiskScan("TARGETS_READY" if ids else "NO_ELIGIBLE_TARGETS", tuple(ids), scanned=scanned, pids=tuple(pids))

    async def lock_risk_targets(self, project_id: uuid.UUID, target_ids: list[uuid.UUID], clock=time.monotonic
                                ) -> list[TrackedRow]:
        """`<RISK_TARGET_LOCK>`: the final snapshot, locked by ONE ordered statement under its own budget (design 7.4 'Risk-target
        lock phase'). Sorts and de-duplicates the ids, checks the returned order, restores the caller's timeouts before returning.
        Raises RiskLockFailure with the whole transaction to be rolled back."""
        wanted = sorted(set(target_ids))
        if not wanted:
            return []
        deadline = clock() + P_RISK_LOCK_SECONDS
        previous = await self._current_timeouts()
        remaining_ms = int((deadline - clock()) * 1000)
        if remaining_ms < 2:
            raise RiskLockFailure("TARGET_PAGE_TIMEOUT", "TARGET_LOCK_BUDGET_EXHAUSTED")
        statement_ms = min(int(P_RISK_LOCK_STATEMENT_SECONDS * 1000), remaining_ms)
        lock_ms = min(int(P_RISK_LOCK_WAIT_SECONDS * 1000), remaining_ms, statement_ms - 1)
        stmt = text(S.RISK_TARGET_LOCK_SQL).bindparams(bindparam("target_ids", type_=ARRAY(PG_UUID(as_uuid=True))))
        try:
            await self.s.execute(text("select set_config('statement_timeout', :ms, true)"), {"ms": str(statement_ms)})
            await self.s.execute(text("select set_config('lock_timeout', :ms, true)"), {"ms": str(lock_ms)})
            rows = self._tracked_rows(await self.s.execute(stmt, {"project_id": project_id, "target_ids": wanted}))
        except Exception as exc:  # noqa: BLE001
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            if sqlstate == "57014":
                raise RiskLockFailure("TARGET_PAGE_TIMEOUT", "TARGET_PAGE_TIMEOUT") from exc
            raise RiskLockFailure("TARGET_QUERY_FAILED", "TARGET_LOCK_TIMEOUT" if sqlstate == "55P03" else "TARGET_QUERY_FAILED") from exc
        ids = [r.id for r in rows]
        if ids != sorted(set(ids)):
            raise RiskLockFailure("TARGET_QUERY_FAILED", "TARGET_LOCK_ORDER")
        await self._restore_timeouts(previous)
        return rows

    async def project_u_discovery(self, project_id: uuid.UUID, classify, clock=time.monotonic) -> "RiskScan":
        """The bounded keyset discovery of the `continue` option (design 7.5 item 2, decision table of 'continue: anomaly or unresolved
        rows?'): ONE ascending stream over `<PROJECT_U_PAGE>` (no Task partition), stopping at the first `decode_error` row.
        States: `DECODE_ERROR_FOUND` (an anomaly Python alone can see), `DISCOVERY_END` (the whole U set was read, no decode_error),
        `SCAN_LIMIT_REACHED` (cap ROW or TIME), `QUERY_FAILED` (failure PAGE_TIMEOUT, TRANSIENT or PERMANENT). The caller keeps its
        own statement timeout (restored on every non-failing exit); a failed page aborts the transaction, so the caller runs this
        inside a savepoint."""
        deadline = clock() + P_CANDIDATE_SCAN_MAX_SECONDS
        previous = await self._current_timeouts()
        scanned, cursor = 0, None
        try:
            while True:
                page_size = min(P_CANDIDATE_PAGE_ROWS, P_CANDIDATE_MAX_SCANNED_ROWS - scanned)
                timeout_ms = int(min(P_CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS, deadline - clock()) * 1000)
                if page_size <= 0 or timeout_ms < 1:
                    await self._restore_timeouts(previous)
                    return RiskScan("SCAN_LIMIT_REACHED", cap="ROW" if page_size <= 0 else "TIME", scanned=scanned)
                await self.set_local_statement_timeout(timeout_ms)
                params = {"project_id": project_id, "page_size": page_size}
                if cursor is not None:
                    params["last_seen_id"] = cursor
                page = self._tracked_rows(await self.s.execute(
                    text(S.PROJECT_U_PAGE_NEXT_SQL if cursor is not None else S.PROJECT_U_PAGE_FIRST_SQL), params))
                for row in page:
                    cursor = row.id
                    scanned += 1
                    verdict = classify(row.id, row.pid, row.finished_at, row.detail_text)
                    if verdict.is_anomaly and verdict.sub_reason == "decode_error":
                        await self._restore_timeouts(previous)
                        return RiskScan("DECODE_ERROR_FOUND", scanned=scanned)
                if len(page) < page_size:
                    await self._restore_timeouts(previous)
                    return RiskScan("DISCOVERY_END", scanned=scanned)
        except Exception as exc:  # noqa: BLE001
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            failure = "PAGE_TIMEOUT" if sqlstate == "57014" else "TRANSIENT" if sqlstate else "PERMANENT"
            return RiskScan("QUERY_FAILED", failure=failure, scanned=scanned)
