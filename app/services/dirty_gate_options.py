"""The dynamic options of the PROJECT_DIRTY gate (design 7.5 item 1 and 2, the one priority table).

ONE computation, `compute_gate_options`, serves the decision (validating a submitted option) and the reads (the options provider),
so both always agree. State is loaded in this order, stopping as soon as the options are decided: the Task/project link, whether
the project is CLEAN, and only for a DIRTY project the gate's epoch, U and A (stage 1), alive-blocked, cooldown and window, and
last the target scan of the option that reaches rank 8. Every hidden option carries exactly one reason, the first row of the
priority table that holds; an option that does not apply is in neither list.
"""
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.models.tables import Approval, Project, Task
from app.repositories.process_risk_repository import ProcessRiskRequestRepository
from app.repositories.task_repository import RiskScan, TaskRepository
from app.services.process_risk_worker import recheck_cooldown_seconds
from app.services.project_terminal import has_unresolved_or_anomalous_for_project
from app.services.tracked_classifier import classify_row, risk_acceptance_pid_valid
from app.validation import policies as P

ALL_OPTIONS = ("continue", "abort", "accept_unproven_risk", "recheck_processes", "refresh_gate")
RISK_OPTION_KIND = {"accept_unproven_risk": "ACCEPT", "recheck_processes": "RECHECK"}
SCAN_REASON = {("SCAN_LIMIT_REACHED", "ROW"): ("TARGET_SCAN_ROW_LIMIT_REACHED", True),
               ("SCAN_LIMIT_REACHED", "TIME"): ("TARGET_SCAN_TIME_LIMIT_REACHED", True),
               ("QUERY_FAILED", "PAGE_TIMEOUT"): ("TARGET_PAGE_TIMEOUT", True),
               ("QUERY_FAILED", "TRANSIENT"): ("TARGET_QUERY_FAILED", True),
               ("QUERY_FAILED", "PERMANENT"): ("TARGET_CLASSIFICATION_ERROR", False)}


@dataclass(frozen=True)
class HiddenOption:
    option_id: str
    reason_code: str
    retryable: bool
    next_available_at: datetime | None = None
    basis_request_id: uuid.UUID | None = None
    message: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    """`options` is the list of offered options (`{"id": ..., "risk": ...}`), `options_hidden` the explained hidden ones."""
    options: list[dict]
    options_hidden: list[HiddenOption]
    candidates: dict[str, tuple[uuid.UUID, ...]] = field(default_factory=dict, compare=False)     # risk option -> batch ids
    scan_failed: bool = False

    @property
    def offered_ids(self) -> list[str]:
        return [o["id"] for o in self.options]

    def hidden_for(self, option: str) -> HiddenOption | None:
        return next((h for h in self.options_hidden if h.option_id == option), None)


def _offered(option: str) -> dict:
    return {"id": option, "risk": "HIGH"} if option in RISK_OPTION_KIND else {"id": option}


def _scan_hidden(option: str, scan: RiskScan) -> HiddenOption | None:
    if scan.state == "TARGETS_READY":
        return None
    if scan.state == "NO_ELIGIBLE_TARGETS":
        return HiddenOption(option, "NO_ELIGIBLE_TARGETS", False)
    reason, retryable = SCAN_REASON[(scan.state, scan.cap if scan.state == "SCAN_LIMIT_REACHED" else scan.failure)]
    return HiddenOption(option, reason, retryable)


def accept_pid_hidden(scan: RiskScan) -> HiddenOption | None:
    """The ONE eligibility check of `accept_unproven_risk` for a ready target set (single gate, batch read and decision alike):
    a valid `risk_acceptance` record names the row's pid, so a set with any pid-less target is not acceptable as a whole
    (never a partial accept of the rows that do have a pid)."""
    if scan.state == "TARGETS_READY" and not all(risk_acceptance_pid_valid(p) for p in scan.pids):
        return HiddenOption("accept_unproven_risk", "TARGET_WITHOUT_PID", False)
    return None


async def compute_gate_options(session, approval: Approval, task: Task | None, project: Project | None, *, now: datetime,
                               only: str | None = None, stage1_cache: dict | None = None) -> ProviderResult:
    """`only`: the decision validates one submitted option, so only that option's scan may run (at most one scan under the
    decision locks); a read passes None. `stage1_cache` shares the stage-1 U/A read of one project between the gates of one batch."""
    options: list[dict] = []
    hidden: list[HiddenOption] = []
    if project is None or task is None or task.project_id != project.id:                # rank 1: the link first
        return ProviderResult([], [HiddenOption(o, "TASK_PROJECT_LINK_BROKEN", False) for o in ALL_OPTIONS])
    if project.workspace_state != "DIRTY_UNRESOLVED":                                  # CLEAN (rank 2): nothing else is read
        return ProviderResult([_offered("continue"), _offered("abort")], [])
    if project.write_epoch != (approval.target or {}).get("dirty_epoch"):               # a stale gate of a DIRTY project
        return ProviderResult([_offered("refresh_gate"), _offered("abort")], [])
    if only == "abort":                                                                 # nothing to read for an abort
        return ProviderResult([_offered("abort")], [])
    cache = stage1_cache if stage1_cache is not None else {}
    block = cache.get(project.id)
    if block is None:
        block = cache[project.id] = await has_unresolved_or_anomalous_for_project(session, project.id)
    requests = ProcessRiskRequestRepository(session)
    tasks = TaskRepository(session)
    wanted = lambda o: only is None or only == o  # noqa: E731
    candidates: dict[str, tuple[uuid.UUID, ...]] = {}
    scan_failed = False

    # alive-blocked: the process-observation request with the greatest finished_at, among those overlapping U
    observation = await requests.latest_observation_overlapping(project.id, list(block.unresolved_ids))
    alive_basis = observation.approval_id if observation is not None and observation.status == "REFUSED_ALIVE" else None

    # continue: the DIRTY clause of the one offering condition (item 1(f))
    if wanted("continue") or only is None:
        if block.anomalies:
            hidden.append(HiddenOption("continue", "TRACKED_ANOMALY_PRESENT", False))
        elif block.unresolved_ids:
            hidden.append(await _continue_discovery(session, project, tasks))
        else:
            options.append(_offered("continue"))
    options.append(_offered("abort"))

    # accept_unproven_risk
    if wanted("accept_unproven_risk"):
        if alive_basis is not None:
            hidden.append(HiddenOption("accept_unproven_risk", "ALIVE_BLOCKED", True, basis_request_id=alive_basis))
        elif not block.unresolved_ids:
            hidden.append(HiddenOption("accept_unproven_risk", "NO_UNRESOLVED_ROWS", False))
        else:
            scan = await _scan_in_savepoint(session, tasks.risk_batch_targets(project.id, task.id, P.PROCESS_RISK_MAX_TARGETS, classify_row))
            scan_failed = scan_failed or scan.state == "QUERY_FAILED"
            reason = _scan_hidden("accept_unproven_risk", scan) or accept_pid_hidden(scan)
            if reason is None:
                options.append(_offered("accept_unproven_risk"))
                candidates["accept_unproven_risk"] = scan.ids
            else:
                hidden.append(reason)

    # recheck_processes: applies only while alive-blocked
    if alive_basis is not None and wanted("recheck_processes"):
        epoch = project.write_epoch
        reason = None
        if await requests.active_recheck_count(project.id, epoch) > 0:                    # rank 3
            active = await _active_recheck(session, project.id, epoch)
            reason = HiddenOption("recheck_processes", "ACTIVE_RECHECK", True, basis_request_id=active)
        if reason is None:
            latest = await requests.latest_terminal(project.id, kind="RECHECK", dirty_epoch=epoch)
            if latest is not None and latest.finished_at is not None:                    # rank 5: the first recheck has none
                streak = await requests.recheck_refused_streak(project.id, epoch, 64)
                length = recheck_cooldown_seconds(max(streak - 1, 0))
                ends = latest.finished_at + timedelta(seconds=length)
                if now < ends:
                    reason = HiddenOption("recheck_processes", "COOLDOWN", True, next_available_at=ends,
                                          basis_request_id=latest.approval_id)
        if reason is None:                                                               # rank 6
            count, oldest = await requests.recheck_window(project.id, epoch, P.PROCESS_RECHECK_WINDOW_SECONDS)
            if count >= P.PROCESS_RECHECK_MAX_PER_WINDOW and oldest is not None:
                reason = HiddenOption("recheck_processes", "RATE_LIMIT_WINDOW", True,
                                      next_available_at=oldest.created_at + timedelta(seconds=P.PROCESS_RECHECK_WINDOW_SECONDS),
                                      basis_request_id=oldest.approval_id)
        if reason is None:                                                               # rank 8/9
            scan = await _scan_in_savepoint(session, tasks.risk_batch_targets(project.id, task.id, P.PROCESS_RISK_MAX_TARGETS, classify_row))
            scan_failed = scan_failed or scan.state == "QUERY_FAILED"
            reason = _scan_hidden("recheck_processes", scan)
            if reason is None:
                options.append(_offered("recheck_processes"))
                candidates["recheck_processes"] = scan.ids
        if reason is not None:
            hidden.append(reason)
    return ProviderResult(options, hidden, candidates, scan_failed)


async def _scan_in_savepoint(session, scan_coro):
    """A scan swallows its database error into QUERY_FAILED, but that error has aborted the transaction (25P02): the savepoint is
    rolled back so the rest of the computation (and the caller's transaction) stays usable; on success it is released."""
    nested = await session.begin_nested()
    try:
        result = await scan_coro
    except BaseException:
        await nested.rollback()
        raise
    if result.state == "QUERY_FAILED":
        await nested.rollback()
    else:
        await nested.commit()
    return result


async def _continue_discovery(session, project: Project, tasks: TaskRepository) -> HiddenOption:
    """SQL A is empty and SQL U is not: the bounded keyset discovery decides the reason of a hidden `continue` (decision table of
    'continue: anomaly or unresolved rows?', rows 3 to 9). Run in a savepoint: a failed page must not poison the rest."""
    found = await _scan_in_savepoint(session, tasks.project_u_discovery(project.id, classify_row))
    if found.state == "DECODE_ERROR_FOUND":
        return HiddenOption("continue", "TRACKED_ANOMALY_PRESENT", False)
    if found.state == "DISCOVERY_END":
        return HiddenOption("continue", "UNRESOLVED_ROWS_PRESENT", True)
    reason, retryable = SCAN_REASON[(found.state, found.cap if found.state == "SCAN_LIMIT_REACHED" else found.failure)]
    return HiddenOption("continue", reason, retryable)


async def _active_recheck(session, project_id: uuid.UUID, epoch: int) -> uuid.UUID | None:
    from sqlalchemy import select

    from app.models.tables import ProcessRiskRequest as R
    row = (await session.execute(select(R.approval_id).where(
        R.project_id == project_id, R.dirty_epoch == epoch, R.kind == "RECHECK",
        R.status.in_(("REQUESTED", "VERIFYING", "RETRYABLE"))).limit(1))).first()
    return row[0] if row else None


def utcnow() -> datetime:
    return datetime.now(UTC)
