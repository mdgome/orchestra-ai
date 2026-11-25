"""TRACKED_EXECUTIONS diagnostics: the causes of design 14.1 for one project.

Two independent evaluations whose results are merged by `diagnostics_service.merge_causes`:

* row causes: the tracked rows of the project (set U and set A, read ONLY through `has_unresolved_or_anomalous_for_project`)
  with their Task, run, lease and tester step, loaded by separate queries by id (never a join of `tasks` and `executions`);
* request causes: the project's open `process_risk_requests` and the capability state, evaluated even when U is empty.

`evaluate` is pure (no I/O, no clock): the reader loads the facts and hands them over. There is no per-execution heartbeat
column; the lease of the owning run is the heartbeat evidence. A tracked execution is running NORMALLY only if all seven
conditions of 14.1 hold; normal progress is information (`TrackedVerdict.running`), never a cause.
"""
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.db import Database
from app.models.states import STEP_TEST, TERMINAL_TASK_STATES, StepState, TaskState
from app.repositories.process_risk_repository import ProcessRiskRequestRepository
from app.repositories.task_repository import TaskRepository
from app.services.diagnostics_service import DiagnosticCause, Status, TrackedVerdict
from app.services.project_terminal import TrackedBlock, has_unresolved_or_anomalous_for_project
from app.validation import policies as P

_NOT_EXECUTING_STEPS = frozenset({StepState.INTERRUPTED.value, StepState.NEEDS_MANUAL_RECOVERY.value,
                                  StepState.WAITING_APPROVAL.value})
_CAPABILITY_MISMATCH_STATES = frozenset({"UNAVAILABLE", "UNSUPPORTED_PLATFORM"})
_RUN_RUNNING = "RUNNING"

# reason_code -> (severity, cause text, recommendation); the table of 14.1
_TEXTS = {
    "TRACKED_EXECUTION_ANOMALY": (
        Status.FAIL, "tracked executions are anomalous: an invalid proof or disposition, a wrong tracked marker or an "
                     "undecodable detail (decode_error); the kinds and sub-reasons are named in the anomaly list",
        "repair is a manual, audited correction outside B2 (H11); a decode_error row has no automatic exit; the project stays "
        "DIRTY_UNRESOLVED"),
    "TASK_TERMINAL_WITH_UNRESOLVED": (
        Status.WARN, "the Task ended while its executions are not proven terminated",
        "wait for the sweeper, or decide at the PROJECT_DIRTY gate"),
    "NO_OWNING_RUN": (Status.WARN, "no live run owns the execution", "wait for the sweeper or recover the Task"),
    "TASK_NOT_EXECUTING": (
        Status.WARN, "the Task is parked or stopped while an execution is not proven terminated",
        "decide at the RECOVERY gate, or wait for the sweeper or the registry re-check to prove the execution"),
    "LEASE_EXPIRED": (Status.WARN, "the owning run's lease expired", "wait for takeover or recovery"),
    "RUNNER_MISMATCH": (Status.WARN, "the execution belongs to another Runner incarnation", "wait for takeover or recovery"),
    "EXECUTION_TOO_OLD": (Status.WARN, "the execution runs longer than any command may", "cancel the Task and check the host"),
    "CLOSED_UNPROVEN": (Status.WARN, "a closing write was lost", "wait for the sweeper"),
    "REQUEST_WITHOUT_WORKER": (Status.WARN, "no worker has claimed the request", "check the RUNNER_STATE item"),
    "REQUEST_CLAIM_TIMEOUT": (Status.WARN, "a verification exceeds its claim",
                              "the request is reclaimed; check the Runner"),
    "REQUEST_RETRY_STUCK": (Status.WARN, "the automatic retries are exhausted or nearly so", "decide again at the new gate"),
    "CAPABILITY_MISMATCH": (Status.WARN, "the tracking capability of the host changed after the row was written",
                            "fix the host, then recheck"),
}


def _cause(code: str, executions=(), requests=(), **extra) -> DiagnosticCause:
    severity, text, recommendation = _TEXTS[code]
    cap = P.DIAGNOSTIC_MAX_RELATED_IDS
    return DiagnosticCause(code, severity, False, text, recommendation,
                           related_execution_ids=tuple(sorted(set(executions))[:cap]),
                           related_request_ids=tuple(sorted(set(requests))[:cap]), **extra)


def _task_executing(task_status: str | None, test_step_status: str | None) -> bool:
    """Condition (1) and (2) of 14.1: a TESTING Task whose tester step is not INTERRUPTED, NEEDS_MANUAL_RECOVERY or
    WAITING_APPROVAL (no step at all is accepted: `ids_before` runs before the step of the iteration exists)."""
    return task_status == TaskState.TESTING.value and test_step_status not in _NOT_EXECUTING_STEPS


def row_causes(block: TrackedBlock, facts: dict, task_statuses: dict, runs: dict, test_steps: dict,
               capability_state: str | None, now: datetime) -> tuple[list[DiagnosticCause], int]:
    """(row causes, number of executions running normally). `facts` is by execution id, `runs` by run id, `test_steps` by
    task id (the status of the latest TEST step or None)."""
    causes: list[DiagnosticCause] = []
    running = 0
    terminal_ids: list[uuid.UUID] = []
    by_code: dict[str, list[uuid.UUID]] = {}
    open_ids: list[uuid.UUID] = []
    max_age = timedelta(seconds=P.TRACKED_EXECUTION_MAX_AGE_SECONDS)
    anomaly_free = not block.anomalies
    for a in block.anomalies:
        if task_statuses.get(a.task_id) in TERMINAL_TASK_STATES:
            terminal_ids.append(a.execution_id)
    for eid in block.unresolved_ids:
        fact = facts.get(eid)
        if fact is None:
            continue
        status = task_statuses.get(fact.task_id)
        if status in TERMINAL_TASK_STATES:
            terminal_ids.append(eid)           # the only row cause of a terminal Task (the others repeat the same fact)
            continue
        if fact.finished_at is not None:
            by_code.setdefault("CLOSED_UNPROVEN", []).append(eid)
            continue
        open_ids.append(eid)
        found: list[str] = []
        run = runs.get(fact.run_id) if fact.run_id is not None else None
        if run is None or run.status != _RUN_RUNNING:
            found.append("NO_OWNING_RUN")
        else:
            if run.lease_expires_at <= now:
                found.append("LEASE_EXPIRED")
            if fact.runner_id != run.runner_id:
                found.append("RUNNER_MISMATCH")
        if not _task_executing(status, test_steps.get(fact.task_id)):
            found.append("TASK_NOT_EXECUTING")
        if now - fact.started_at > max_age:
            found.append("EXECUTION_TOO_OLD")
        for code in found:
            by_code.setdefault(code, []).append(eid)
        if not found and anomaly_free:
            running += 1
    if block.anomalies:
        shown = tuple(block.anomalies)
        causes.append(_cause("TRACKED_EXECUTION_ANOMALY", executions=[a.execution_id for a in shown], anomalies=shown,
                             anomalies_returned=len(shown), anomalies_truncated=block.anomalies_truncated,
                             anomalies_total=block.anomalies_total if block.anomalies_total_is_exact else None,
                             anomalies_total_is_exact=block.anomalies_total_is_exact,
                             may_be_permanently_stuck=any(a.sub_reason == "decode_error" for a in shown)))
    if terminal_ids:
        causes.append(_cause("TASK_TERMINAL_WITH_UNRESOLVED", executions=terminal_ids))
    causes.extend(_cause(code, executions=ids) for code, ids in by_code.items())
    if open_ids and capability_state in _CAPABILITY_MISMATCH_STATES:
        causes.append(_cause("CAPABILITY_MISMATCH", executions=open_ids))
    return causes, running


def request_causes(requests, latest_terminal, unresolved_present: bool, now: datetime) -> list[DiagnosticCause]:
    """The request causes (14.1), evaluated independently of U (a request can outlive the rows it was made for)."""
    stale = timedelta(seconds=P.PROCESS_RISK_REQUEST_STALE_SECONDS)
    claim = timedelta(seconds=P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS)
    no_worker, claim_timeout, retry_stuck = [], [], []
    for r in requests:
        if r.status in ("REQUESTED", "RETRYABLE") and now - r.next_attempt_at > stale:
            no_worker.append(r.approval_id)       # due since longer than the stale limit: nobody picked it up
        if r.status == "VERIFYING" and r.claimed_at is not None and now - r.claimed_at > claim:
            claim_timeout.append(r.approval_id)
        if r.status == "RETRYABLE" and r.attempts >= P.PROCESS_RISK_MAX_ATTEMPTS - 1:
            retry_stuck.append(r.approval_id)
    if (latest_terminal is not None and unresolved_present and latest_terminal.status == "NEEDS_REDECISION"
            and latest_terminal.reason == "PROCESS_RECHECK_INCONCLUSIVE"):
        retry_stuck.append(latest_terminal.approval_id)
    out = []
    if no_worker:
        out.append(_cause("REQUEST_WITHOUT_WORKER", requests=no_worker))
    if claim_timeout:
        out.append(_cause("REQUEST_CLAIM_TIMEOUT", requests=claim_timeout))
    if retry_stuck:
        out.append(_cause("REQUEST_RETRY_STUCK", requests=retry_stuck))
    return out


def evaluate(block: TrackedBlock, facts: dict, task_statuses: dict, runs: dict, test_steps: dict, requests, latest_terminal,
             capability_state: str | None, now: datetime) -> TrackedVerdict:
    causes, running = row_causes(block, facts, task_statuses, runs, test_steps, capability_state, now)
    causes += request_causes(requests, latest_terminal, bool(block.unresolved_ids), now)
    return TrackedVerdict(tuple(causes), running)


class TrackedDiagnosticsReader:
    """The injected `read_tracked` callable of `DiagnosticsService`: loads the facts of one project in ONE short read
    transaction (no CLI work, no row lock) and evaluates them."""

    def __init__(self, db: Database, *, read_capability: Callable[[], Awaitable] | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._db, self._read_capability, self._now = db, read_capability, now

    async def __call__(self, project_id: uuid.UUID) -> TrackedVerdict:
        async with self._db.transaction() as s:
            block = await has_unresolved_or_anomalous_for_project(s, project_id)
            tasks_repo = TaskRepository(s)
            ids = [*block.unresolved_ids, *(a.execution_id for a in block.anomalies)]
            facts = {f.id: f for f in await tasks_repo.execution_facts(ids)}
            task_ids = sorted({f.task_id for f in facts.values()} | {a.task_id for a in block.anomalies})
            statuses = await tasks_repo.task_statuses(task_ids)
            runs = await tasks_repo.runs_by_ids(sorted({f.run_id for f in facts.values() if f.run_id is not None}))
            open_tasks = sorted({f.task_id for f in facts.values()
                                 if f.finished_at is None and statuses.get(f.task_id) not in TERMINAL_TASK_STATES})
            test_steps = {}
            for task_id in open_tasks:
                step = await tasks_repo.latest_step(task_id, STEP_TEST)
                test_steps[task_id] = step.status if step is not None else None
            risk = ProcessRiskRequestRepository(s)
            requests = await risk.open_for_project(project_id, P.DIAGNOSTIC_TRACKED_PROJECT_ROWS)
            latest = await risk.latest_terminal(project_id)
        capability = None
        if self._read_capability is not None:
            try:
                capability = getattr(await self._read_capability(), "state", None)
            except Exception:  # noqa: BLE001 - an unreadable capability only skips CAPABILITY_MISMATCH
                capability = None
        return evaluate(block, facts, statuses, runs, test_steps, requests, latest, capability, self._now())
