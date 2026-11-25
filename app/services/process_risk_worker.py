"""The process-risk request worker (design 7.5 items 5 to 8): claim, verify outside any transaction, one fenced result transaction.

`ProcessRiskWorker` is the injectable `risk_pass()` of the Runner. One pass claims at most PROCESS_RISK_BATCH_REQUESTS requests
(PROCESS_RISK_BATCH_ROWS target rows in total) and stops claiming when less than PROCESS_RISK_REQUEST_TIMEOUT_SECONDS of the
pass budget is left. A request is processed in three steps: (1) a short read of the context and the targets, (2) the OS
verification of every unresolved target, serial, in a thread, with no database section open, (3) one result transaction that locks
Task -> Project -> the request row (fencing) -> the target rows in ascending id, evaluates the codes in the tier order of item 6
and writes the status. A worker that lost its claim (0 rows on the fenced lock) stops without writing anything.

The follow-up gate of a refused or redecided request (item 11) is a seam: `follow_up(session, task, request, code)` runs in the
result transaction when given; without it the request status alone records the outcome.
"""
import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db import Database
from app.models.tables import ProcessRiskRequest
from app.repositories.approval_repository import ApprovalRepository, ExecutionRequestRepository
from app.repositories.process_risk_repository import ProcessRiskRequestRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository, TrackedRow
from app.services.lease_service import (RetainedPresent, RetainedSettler, StaleExecution, TrackedVerdict, TrackedVerdictFn,
                                          default_tracked_verdict, retained_job_verdict)
from app.services.tracked_classifier import RowClass, classify_row, decode_detail
from app.validation import policies as P

log = logging.getLogger(__name__)

OPTION_FOR_KIND = {"ACCEPT": "accept_unproven_risk", "RECHECK": "recheck_processes"}
RESOLVED, ALIVE, PROVABLE, UNCLEAR, ERROR = "RESOLVED", "ALIVE", "PROVABLE", "UNCLEAR", "ERROR"
DEFENSIVE_CODES = frozenset({"APPROVAL_NOT_APPLICABLE", "TASK_PROJECT_MISMATCH"})
_UNCLEAR_REASONS = frozenset({"IDENTITY_RECORD_MISSING", "H3_DISABLED", "OTHER_HOST"})


def iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def retry_delay_seconds(attempts: int) -> float:
    """Capped exponential backoff of item 5: BASE * 2^(attempts - 1), at most CAP (attempts counts this failure)."""
    return min(float(P.PROCESS_RISK_BACKOFF_CAP_SECONDS), P.PROCESS_RISK_BACKOFF_BASE_SECONDS * 2.0 ** max(0, attempts - 1))


def recheck_cooldown_seconds(refused_streak: int) -> float:
    """Item 8 (a): min(CAP, BASE * 2^k), k = consecutive REFUSED_ALIVE rechecks; only meaningful after a first recheck."""
    return min(float(P.PROCESS_RECHECK_COOLDOWN_CAP_SECONDS), P.PROCESS_RECHECK_COOLDOWN_BASE_SECONDS * 2.0 ** refused_streak)


def map_verdict(verdict: TrackedVerdict) -> tuple[str, str | None]:
    """The tracked verdict as a worker verdict: (PROVABLE | ALIVE | UNCLEAR, the `reverify_result` of an UNCLEAR row)."""
    if verdict.confirmed:
        return PROVABLE, None
    if verdict.termination == "STILL_ALIVE":
        return ALIVE, None
    if verdict.reason in _UNCLEAR_REASONS:
        return UNCLEAR, verdict.reason
    return UNCLEAR, "ACCESS_DENIED" if verdict.termination == "ACCESS_DENIED" else "UNKNOWN"


@dataclass
class Target:
    row: TrackedRow
    verdict: str = ERROR
    tracked: TrackedVerdict | None = None
    reverify: str | None = None


class ProcessRiskWorker:
    def __init__(self, db: Database, db_section: Callable[[], object], *, incarnation: Callable[[], uuid.UUID | None] = lambda: None,
                 notify: Callable[[], None] = lambda: None, tracked_verdict: TrackedVerdictFn = default_tracked_verdict,
                 retained_settler: RetainedSettler | None = None, retained_present: RetainedPresent | None = None,
                 clock: Callable[[], float] = time.monotonic, run_blocking: Callable | None = None, follow_up: Callable[..., Awaitable[None]] | None = None):
        self.db, self._section, self._incarnation, self._notify = db, db_section, incarnation, notify
        self._verdict, self._retained, self._clock, self._follow_up = tracked_verdict, retained_settler, clock, follow_up
        self._retained_present = retained_present
        self._blocking = run_blocking or asyncio.to_thread
        self._fallback = uuid.uuid4()

    @property
    def me(self) -> uuid.UUID:
        return self._incarnation() or self._fallback

    # --- the pass --------------------------------------------------------------------------------------------------------
    async def __call__(self) -> int:
        """One pass; returns the number of requests it handled."""
        pass_deadline = self._clock() + P.PROCESS_RISK_PASS_SECONDS
        handled = rows = 0
        while handled < P.PROCESS_RISK_BATCH_REQUESTS and rows < P.PROCESS_RISK_BATCH_ROWS:
            if pass_deadline - self._clock() < P.PROCESS_RISK_REQUEST_TIMEOUT_SECONDS:
                break
            claimed = await self._claim_next()
            if claimed is None:
                break
            request, generation = claimed
            handled += 1
            rows += len(request.target_execution_ids)
            try:
                await self._process(request, generation)
            except asyncio.CancelledError:
                raise                                   # left VERIFYING: reclaimed later (design 9.1)
            except Exception as exc:  # noqa: BLE001
                log.warning("risk request failed: approval=%s error=%s", request.approval_id, type(exc).__name__)
                await self._retry_or_redecide(request, generation, type(exc).__name__)
        return handled

    async def _claim_next(self) -> tuple[ProcessRiskRequest, int] | None:
        async with self._section():
            async with self.db.transaction() as s:
                repo = ProcessRiskRequestRepository(s)
                for candidate in await repo.list_claimable(self.me, P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS, 4):
                    generation = await repo.claim(candidate.approval_id, self.me, P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS)
                    if generation is not None:
                        return (await repo.get(candidate.approval_id)), generation
        return None

    # --- one request -----------------------------------------------------------------------------------------------------
    async def _process(self, request: ProcessRiskRequest, generation: int) -> None:
        deadline = self._clock() + P.PROCESS_RISK_REQUEST_TIMEOUT_SECONDS
        async with self._section():
            async with self.db.transaction() as s:
                code, _ = await self._context_code(s, request, lock=False)
                rows = await self._read_targets(s, request) if code is None else {}
        targets: list[Target] = []
        if code is None:
            for exec_id in sorted(request.target_execution_ids):
                targets.append(await self._judge(rows.get(exec_id), deadline))
        await self._result(request, generation, targets)

    async def _read_targets(self, s, request) -> dict[uuid.UUID, TrackedRow | None]:
        repo = TaskRepository(s)
        return {i: await repo.tracked_row(i) for i in sorted(request.target_execution_ids)}

    async def _judge(self, row: TrackedRow | None, deadline: float) -> Target:
        if row is None:
            return Target(TrackedRow(uuid.UUID(int=0), uuid.UUID(int=0), None, None, None, None), ERROR)
        target = Target(row)
        cls = classify_row(row.id, row.pid, row.finished_at, row.detail_text)
        if cls.row_class in (RowClass.RESOLVED_PROVEN, RowClass.RESOLVED_RISK):
            target.verdict = RESOLVED
            return target
        if not cls.is_unresolved:
            return target                                 # an anomaly (or a legacy row) is never a risk target: ERROR
        remaining = deadline - self._clock()
        if remaining <= 0:
            return target                                 # rows not reached count as ERROR
        try:
            detail = decode_detail(row.detail_text)
            stale = StaleExecution(row.id, row.pid, row.process_create_time, detail)
            tracked = await retained_job_verdict(self._retained, self._retained_present, stale)
            if tracked is None:
                tracked = await asyncio.wait_for(self._blocking(self._verdict, stale),
                                                 timeout=min(P.PROCESS_RISK_ROW_TIMEOUT_SECONDS, remaining))
            target.tracked = tracked
            target.verdict, target.reverify = map_verdict(tracked)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a timeout or an exception of the verification itself is an ERROR verdict
            target.verdict = ERROR
        return target

    # --- the shared predicate (the same tier order for the pre-read and the locked result transaction) -------------------
    async def _context_code(self, s, request: ProcessRiskRequest, *, lock: bool):
        """Tiers T1 to T4 of item 6: the first that holds wins. Returns (code | None, (task, project, approval))."""
        tasks, projects, approvals = TaskRepository(s), ProjectRepository(s), ApprovalRepository(s)
        task = await (tasks.lock(request.task_id) if lock else tasks.get(request.task_id))
        project = await (projects.lock(request.project_id) if lock else projects.get(request.project_id))
        approval = await approvals.get(request.approval_id)
        decision = await approvals.get_decision(request.approval_id) if approval is not None else None
        ctx = (task, project, approval)
        if approval is None or decision is None or approval.task_id != request.task_id:
            return "APPROVAL_NOT_APPLICABLE", ctx
        if task is None or project is None or task.project_id != request.project_id:
            return "TASK_PROJECT_MISMATCH", ctx
        if task.status != "WAITING_APPROVAL":
            return "TASK_NOT_WAITING_APPROVAL", ctx
        if any(p.id != approval.id for p in await approvals.pending_for_task(task.id)):
            return "GATE_ALREADY_PENDING", ctx
        if decision.decision != "APPROVED" or decision.selected_option != OPTION_FOR_KIND.get(request.kind):
            return "RISK_REQUEST_NOT_APPLICABLE", ctx
        if project.write_epoch != request.dirty_epoch:
            return "DIRTY_EPOCH_CHANGED", ctx
        return None, ctx

    # --- the result transaction ------------------------------------------------------------------------------------------
    async def _result(self, request: ProcessRiskRequest, generation: int, targets: list[Target]) -> None:
        resume_inserted = False
        async with self._section():
            async with self.db.transaction() as s:
                repo = ProcessRiskRequestRepository(s)
                code, (task, project, approval) = await self._context_code(s, request, lock=True)   # Task -> Project
                locked_request = await repo.lock_fenced(request.approval_id, generation)            # -> request row
                if locked_request is None:
                    return                                                                          # claim lost: write nothing
                if code is not None:
                    await self._redecide(s, repo, request, generation, task, code)
                    return
                tasks = TaskRepository(s)
                ids = sorted(request.target_execution_ids)
                await tasks.lock_executions_ordered(ids)                                            # -> rows, ascending id
                fresh = {i: await tasks.tracked_row(i) for i in ids}
                in_project = await tasks.task_ids_of_project(
                    request.project_id, [r.task_id for r in fresh.values() if r is not None])
                by_id = {t.row.id: t for t in targets}
                for i in ids:
                    row, seen = fresh[i], by_id.get(i)
                    if (row is None or row.task_id not in in_project or seen is None
                            or (row.pid, row.process_create_time) != (seen.row.pid, seen.row.process_create_time)):
                        await self._redecide(s, repo, request, generation, task, "EXECUTION_IDENTITY_CHANGED")
                        return
                outcome = await self._apply(s, tasks, request, [(fresh[i], by_id[i]) for i in ids])
                status, reason, result = self._decide(request, outcome)
                attempts, next_at, last_error = request.attempts, None, None
                if status == "RETRYABLE":
                    attempts, last_error = request.attempts + 1, "VERIFICATION_ERROR"
                    if attempts >= P.PROCESS_RISK_MAX_ATTEMPTS:
                        status, reason = "NEEDS_REDECISION", "PROCESS_RECHECK_INCONCLUSIVE"
                    else:
                        next_at = datetime.now(UTC) + timedelta(seconds=retry_delay_seconds(attempts))
                if not await repo.write_result(request.approval_id, generation, status=status, result=result, reason=reason,
                                               attempts=attempts, last_error=last_error, next_attempt_at=next_at):
                    raise RuntimeError("the fenced result write matched no row after the fenced lock")
                if status == "APPLIED":
                    resume_inserted = await ExecutionRequestRepository(s).add_resume_if_absent(task.id, approval.id)
                elif status in ("REFUSED_ALIVE", "NEEDS_REDECISION") and self._follow_up is not None:
                    await self._follow_up(s, task, request, reason or status)
        if resume_inserted:
            self._notify()                                                                           # only after the commit

    async def _apply(self, s, tasks: TaskRepository, request, pairs: list[tuple[TrackedRow, Target]]) -> dict:
        """Writes the true proofs of the PROVABLE rows (even when another row is ALIVE) and, for an ACCEPT request without a live
        survivor or an ERROR, the disposition of the UNCLEAR rows. Returns the per-row summary."""
        summary = {"resolved": [], "proven": [], "alive": [], "unclear": [], "error": [], "accepted": []}
        verdicts = {}
        for row, target in pairs:
            verdict = target.verdict
            if verdict == RESOLVED:
                summary["resolved"].append(str(row.id))
            elif verdict == PROVABLE:
                never_ran = target.tracked.proof == P.PROOF_NEVER_RAN
                merge = {"abandoned_by_risk_worker": True, "proof": target.tracked.proof,
                         "termination": target.tracked.termination, "reason": target.tracked.reason,
                         "lifecycle": "SPAWN_FAILED" if never_ran else "TERMINATED", "tree": "EMPTY"}
                wrote = await tasks.write_proof_to_unresolved(row, merge={k: v for k, v in merge.items() if v is not None},
                                                              close=row.finished_at is None)
                if wrote or await self._resolved_now(tasks, row.id):
                    summary["proven"].append(str(row.id))
                else:
                    verdict = ERROR
            if verdict == ALIVE:
                summary["alive"].append(str(row.id))
            elif verdict == UNCLEAR:
                summary["unclear"].append(str(row.id))
            elif verdict == ERROR:
                summary["error"].append(str(row.id))
            verdicts[row.id] = verdict
        if request.kind == "ACCEPT" and not summary["alive"] and not summary["error"]:
            for row, target in pairs:
                if verdicts[row.id] != UNCLEAR:
                    continue
                if await tasks.write_disposition(row, self._record(request, row, target)) or await self._resolved_now(tasks, row.id):
                    summary["accepted"].append(str(row.id))
                else:
                    summary["error"].append(str(row.id))
        return summary

    @staticmethod
    async def _resolved_now(tasks: TaskRepository, row_id: uuid.UUID) -> bool:
        again = await tasks.tracked_row(row_id)
        return again is not None and classify_row(again.id, again.pid, again.finished_at, again.detail_text).row_class in (
            RowClass.RESOLVED_PROVEN, RowClass.RESOLVED_RISK)

    @staticmethod
    def _record(request: ProcessRiskRequest, row: TrackedRow, target: Target) -> dict:
        record = {"approval_id": str(request.approval_id), "execution_id": str(row.id), "pid": row.pid,
                  "create_time": row.process_create_time if row.process_create_time is not None else 0,
                  "last_verdict": "UNCLEAR", "requested_at": iso_z(request.created_at), "rationale": request.rationale,
                  "reverify_result": target.reverify or "UNKNOWN", "applied_at": iso_z(datetime.now(UTC))}
        if request.requested_by is not None:
            record["requested_by"] = request.requested_by
        return record

    @staticmethod
    def _decide(request: ProcessRiskRequest, summary: dict) -> tuple[str, str | None, dict]:
        """Item 6 (b) to (d) and item 8: the status of the request from the per-row summary."""
        result = {"rows": summary}
        if summary["alive"]:
            return "REFUSED_ALIVE", "LIVE_PROCESS_PRESENT", {**result, "survivors": summary["alive"]}
        if summary["error"]:
            return "RETRYABLE", "PROCESS_RECHECK_INCONCLUSIVE", result
        if request.kind == "RECHECK" and summary["unclear"]:
            return "NEEDS_REDECISION", "NO_LIVE_PROCESS_UNPROVEN", result
        return "APPLIED", None, result

    async def _redecide(self, s, repo, request, generation, task, code: str) -> None:
        """T1 to T5: `NEEDS_REDECISION(code)`, no disposition, no RESUME. The defensive codes keep an audit record; opening the
        EXECUTION_UNCERTAIN gate for them is the follow-up seam (item 6 (a), item 11)."""
        result: dict = {"code": code}
        if code in DEFENSIVE_CODES:
            result["defensive"] = {"code": code, "observed": None, "expected": str(request.approval_id),
                                   "at": iso_z(datetime.now(UTC))}
        await repo.write_result(request.approval_id, generation, status="NEEDS_REDECISION", result=result, reason=code)
        if self._follow_up is not None and task is not None:
            await self._follow_up(s, task, request, code)

    async def _retry_or_redecide(self, request: ProcessRiskRequest, generation: int, error_name: str) -> None:
        """An unexpected exception of the verification or its transactions: RETRYABLE with backoff, then NEEDS_REDECISION."""
        attempts = request.attempts + 1
        try:
            async with self._section():
                async with self.db.transaction() as s:
                    repo = ProcessRiskRequestRepository(s)
                    if attempts >= P.PROCESS_RISK_MAX_ATTEMPTS:
                        task = await TaskRepository(s).lock(request.task_id)               # Task -> Project -> request row
                        await ProjectRepository(s).lock(request.project_id)
                        written = await repo.write_result(request.approval_id, generation, status="NEEDS_REDECISION",
                                                          reason="PROCESS_RECHECK_INCONCLUSIVE", attempts=attempts,
                                                          last_error=error_name, result={"code": "PROCESS_RECHECK_INCONCLUSIVE"})
                        if written and task is not None and self._follow_up is not None:
                            await self._follow_up(s, task, request, "PROCESS_RECHECK_INCONCLUSIVE")
                    else:
                        await repo.write_result(
                            request.approval_id, generation, status="RETRYABLE", attempts=attempts, last_error=error_name,
                            next_attempt_at=datetime.now(UTC) + timedelta(seconds=retry_delay_seconds(attempts)))
        except Exception:  # noqa: BLE001 - the request stays VERIFYING and is reclaimed after the claim timeout
            log.warning("could not record the failure of approval=%s", request.approval_id)
