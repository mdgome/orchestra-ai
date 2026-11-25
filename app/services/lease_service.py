"""PostgreSQL lease: single RUNNING run per task, serialised by the task row lock.

Acquire/takeover is three steps and never holds a row lock while talking to the OS:
  A (short txn)  lock task, inspect the current run, commit
  -- (no txn)    verify / terminate the previous run's CLI processes
  B (short txn)  lock task again, re-validate that nothing changed, ABANDON old + INSERT new
If the previous CLI cannot be proven gone, the takeover is UNCERTAIN and nothing is started.

B2 (design 8): a TRACKED execution row (`detail.tracked` is the JSON boolean true) is judged by an injected tracked-verdict
callable that receives the whole row, not only a ProcessIdentity. Its closing write is a JSONB merge (the tracked keys
survive), carries the positive proof and, for a row without a pid, the `pid IS NULL` guard. Several rows are locked with ONE
ordered statement (`lock_executions_ordered`) before any is updated, so the takeover never locks in the order opposite to the
risk decision. An untracked agent-CLI row keeps today's rules and today's whole-`detail` replacement.
"""
import asyncio
import json
import logging
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import psutil
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.infra.process_control import (
    CONFIRMED_GONE, ProcessIdentity, TerminationResult, check_alive, terminate_tree,
)
from app.models.tables import Execution
from app.repositories.approval_repository import ExecutionRequestRepository
from app.repositories.task_repository import NEVER_RAN_EVIDENCE_GUARD_SQL, TaskRepository
from app.validation import policies as P
from app.validation.exceptions import LeaseLost

log = logging.getLogger(__name__)
Terminator = Callable[[ProcessIdentity], TerminationResult]


def utcnow() -> datetime:
    return datetime.now(UTC)


class AcquireStatus(StrEnum):
    ACQUIRED = "ACQUIRED"
    HELD = "HELD"            # a live (unexpired) lease, or the situation changed under us
    UNCERTAIN = "UNCERTAIN"  # expired, but the previous CLI could not be proven terminated
    FENCED = "FENCED"        # the execution request was requeued/re-claimed: this caller no longer owns it


@dataclass(frozen=True)
class Lease:
    run_id: uuid.UUID
    task_id: uuid.UUID
    runner_id: str


@dataclass(frozen=True)
class AcquireResult:
    status: AcquireStatus
    lease: Lease | None = None
    previous_run_id: uuid.UUID | None = None
    reason: str | None = None


def _default_terminator(identity: ProcessIdentity) -> TerminationResult:
    return terminate_tree(identity, P.PROCESS_TERMINATE_GRACE_SECONDS)


@dataclass(frozen=True)
class StaleExecution:
    """An unfinished execution row of the abandoned run, as Txn A read it."""
    id: uuid.UUID
    pid: int | None
    create_time: float | None
    detail: dict | None

    @property
    def tracked(self) -> bool:
        """Tracked in the sense of the verdict: the marker is exactly the JSON boolean true (design 6.1 (d))."""
        return isinstance(self.detail, dict) and self.detail.get("tracked") is True

    @property
    def tracked_ish(self) -> bool:
        """A tracked marker or a tracked-only key of ANY kind: its `detail` is merged, never replaced (keeps the keys)."""
        return isinstance(self.detail, dict) and ("tracked" in self.detail or bool(P.TRACKED_ONLY_KEYS & self.detail.keys()))


@dataclass(frozen=True)
class TrackedVerdict:
    """The OS-side conclusion for one tracked row. `proof` is written only when `confirmed`."""
    confirmed: bool
    proof: str | None = None
    termination: str | None = None
    reason: str | None = None


# A plain callable (not a Protocol): the whole row in, the verdict out. It does blocking OS work and runs in a thread.
TrackedVerdictFn = Callable[[StaleExecution], TrackedVerdict]
# Phase-4 seam: the Runner's registry of retained Jobs (`CommandExecutor.resettle_retained`); True = the Job is proven empty.
RetainedSettler = Callable[[uuid.UUID], Awaitable[bool]]
# True while the registry still holds a retained Job of the execution (`CommandExecutor.has_retained`): a Job not proven empty.
RetainedPresent = Callable[[uuid.UUID], bool]

_GONE_OR_REUSED = frozenset({TerminationResult.ALREADY_EXITED, TerminationResult.IDENTITY_MISMATCH})


def _survivors_of(root: ProcessIdentity) -> list[int]:
    """Live processes whose parent is the recorded root and which were not created before it (only direct children are
    reliable once the root is gone)."""
    found = []
    for proc in psutil.process_iter(["pid", "ppid", "create_time"]):
        try:
            info = proc.info
            if info["ppid"] == root.pid and info["create_time"] is not None and info["create_time"] >= root.create_time - 1.0:
                found.append(info["pid"])
        except psutil.Error:
            continue
    return found


def spawned_process_unproven(detail) -> bool:
    """True when the executor recorded on the row that a spawned process was not proven gone (`TERMINATION_UNCONFIRMED`, a
    non-empty or unknown tree, `RESULT_MISSING`, `LIFECYCLE_MISSING`). Mirrored in SQL by `TaskRepository` for the guarded writes."""
    if not isinstance(detail, dict):
        return False
    return (detail.get("lifecycle") == "TERMINATION_UNCONFIRMED" or detail.get("tree") in ("NOT_EMPTY", "UNKNOWN")
            or detail.get("reason") in (P.TRACKED_RESULT_MISSING, "LIFECYCLE_MISSING"))


def default_tracked_verdict(row: StaleExecution, *, settle_seconds: float = P.JOB_CLOSE_SETTLE_SECONDS,
                            enabled: bool = P.JOB_KILL_ON_CLOSE_ASSUMPTION_ENABLED,
                            host: str | None = None) -> TrackedVerdict:
    """Design 8: what recovery may conclude about a tracked row of a previous run (blocking; call it in a thread)."""
    if row.pid is None:
        # I4: the identity is persisted BEFORE the resume and the owner's identity write is fenced by `finished_at IS NULL`, so
        # a row without a pid never ran user code. That says nothing about a process that may still exist: when the row itself
        # records that a spawned process was NOT proven gone, `NEVER_RAN` is not proven (the row stays open for the retained-Job
        # re-check and the RECOVERY gate). A crash before the executor could write anything leaves no such record (design 8,
        # `SUSPENDED_ORPHAN_POSSIBLE`).
        if spawned_process_unproven(row.detail):
            return TrackedVerdict(False, reason=P.TRACKED_SPAWNED_PROCESS_UNPROVEN)
        return TrackedVerdict(True, P.PROOF_NEVER_RAN, None, P.TRACKED_SUSPENDED_ORPHAN_POSSIBLE)
    if row.create_time is None:
        return TrackedVerdict(False, reason="IDENTITY_RECORD_MISSING")
    if not enabled:
        return TrackedVerdict(False, reason="H3_DISABLED")
    owner = (row.detail or {}).get("runner_process")
    if not isinstance(owner, dict):
        return TrackedVerdict(False, reason="IDENTITY_RECORD_MISSING")
    try:
        owner_identity = ProcessIdentity(int(owner["pid"]), float(owner["create_time"]))
    except (KeyError, TypeError, ValueError):
        return TrackedVerdict(False, reason="IDENTITY_RECORD_MISSING")
    if owner.get("host") != (host or socket.gethostname()):
        return TrackedVerdict(False, reason="OTHER_HOST")                                    # condition 1
    owner_state = check_alive(owner_identity)
    if owner_state not in _GONE_OR_REUSED:                                                   # condition 2
        return TrackedVerdict(False, termination=str(owner_state), reason="RUNNER_NOT_PROVEN_DEAD")
    root = ProcessIdentity(row.pid, row.create_time)
    root_state = check_alive(root)
    if root_state not in _GONE_OR_REUSED:                                                    # condition 3
        return TrackedVerdict(False, termination=str(root_state), reason="ROOT_NOT_GONE")
    deadline = time.monotonic() + settle_seconds                                             # condition 4
    while _survivors_of(root):
        if time.monotonic() >= deadline:
            return TrackedVerdict(False, termination=str(TerminationResult.STILL_ALIVE), reason="SURVIVORS_AFTER_SETTLE")
        time.sleep(0.1)
    return TrackedVerdict(True, P.PROOF_JOB_KILL_ON_CLOSE_ASSUMED, str(TerminationResult.ALREADY_EXITED), None)


async def retained_job_verdict(settler: RetainedSettler | None, present: RetainedPresent | None,
                               row: StaleExecution) -> TrackedVerdict | None:
    """The retained-Job registry of the Runner decides first (design 7.3): proven empty -> `JOB_EMPTY`. A row WITHOUT a pid whose
    retained Job could not be proven empty (the executor's `TERMINATION_UNCONFIRMED` write may have been lost, so the row carries
    no evidence) must not fall through to `NEVER_RAN` by absence of evidence: the registry itself is the evidence that a spawned
    process may still exist. None = no retained Job answered, the row is judged on its own."""
    if settler is None:
        return None
    if await settler(row.id):
        return TrackedVerdict(True, P.PROOF_JOB_EMPTY, str(TerminationResult.TERMINATED), None)
    if row.pid is None and present is not None and present(row.id):
        return TrackedVerdict(False, reason=P.TRACKED_SPAWNED_PROCESS_UNPROVEN)
    return None


class _TakeoverRaced(Exception):
    """An execution row changed between Txn A and Txn B (an identity committed late): the whole Txn B rolls back."""


class LeaseService:
    def __init__(self, db: Database, runner_id: str, ttl_seconds: int = P.LEASE_TTL_SECONDS,
                 terminator: Terminator = _default_terminator, clock: Callable[[], datetime] = utcnow,
                 tracked_verdict: TrackedVerdictFn = default_tracked_verdict,
                 retained_settler: RetainedSettler | None = None, retained_present: RetainedPresent | None = None):
        self.db = db
        self.runner_id = runner_id
        self.ttl = ttl_seconds
        self._terminate = terminator
        self._now = clock
        self._tracked_verdict = tracked_verdict
        self._retained_settler = retained_settler      # phase 4 wires CommandExecutor.resettle_retained here
        self._retained_present = retained_present
        self._revoked: set[uuid.UUID] = set()      # runs this process must stop writing for (shutdown)

    def revoke(self, run_id: uuid.UUID) -> None:
        """Local, in-memory: every later verify() of this lease raises LeaseLost, so an in-flight run stops without
        recording anything. The DB lease is left to expire, and the normal takeover path (process-tree check,
        RECOVERY gate when unproven) decides what happens next."""
        self._revoked.add(run_id)

    # --- acquire ---------------------------------------------------------------------------
    @staticmethod
    async def _fenced_out(s: AsyncSession, fence: tuple[uuid.UUID, uuid.UUID | None] | None) -> bool:
        return fence is not None and not await ExecutionRequestRepository(s).fence_ok(*fence)

    async def acquire(self, task_id: uuid.UUID, fence: tuple[uuid.UUID, uuid.UUID | None] | None = None
                      ) -> AcquireResult:
        """`fence` = (request id, claim token): checked in both transactions, so a coroutine whose claim was
        requeued meanwhile can never obtain (or take over) a lease."""
        # Txn A
        async with self.db.transaction() as s:
            repo = TaskRepository(s)
            if await repo.lock(task_id) is None:
                raise LookupError("task not found")
            if await self._fenced_out(s, fence):
                return AcquireResult(AcquireStatus.FENCED, reason="execution request claim superseded")
            now = self._now()
            current = await repo.running_run(task_id)
            if current is None:
                run = await repo.insert_run(task_id, self.runner_id, now, self.ttl)
                return AcquireResult(AcquireStatus.ACQUIRED, Lease(run.id, task_id, self.runner_id))
            if current.lease_expires_at > now:
                return AcquireResult(AcquireStatus.HELD, previous_run_id=current.id, reason="lease is live")
            seen = (current.id, current.row_version)
            stale = [StaleExecution(e.id, e.pid, e.process_create_time, e.detail)
                     for e in await repo.unterminated_executions(task_id) if e.run_id == current.id]
        # lock released here

        # OS work, outside any transaction
        verdicts: dict[uuid.UUID, TerminationResult | TrackedVerdict | None] = {}
        for row in stale:
            verdicts[row.id] = await self._judge(row)
        unproven = [(eid, v) for eid, v in verdicts.items() if not self._proven(v)]
        if unproven:
            return AcquireResult(AcquireStatus.UNCERTAIN, previous_run_id=seen[0],
                                 reason="previous CLI process termination not confirmed: "
                                        + ",".join(self._describe(v) for _, v in unproven))

        # Txn B
        try:
            async with self.db.transaction() as s:
                repo = TaskRepository(s)
                await repo.lock(task_id)
                if await self._fenced_out(s, fence):
                    return AcquireResult(AcquireStatus.FENCED, reason="execution request claim superseded")
                now = self._now()
                current = await repo.running_run(task_id)
                if current is None or (current.id, current.row_version) != seen or current.lease_expires_at > now:
                    return AcquireResult(AcquireStatus.HELD, previous_run_id=seen[0], reason="takeover raced")
                await self._close_abandoned(s, repo, stale, verdicts)
                await repo.close_run(current.id, "ABANDONED", "lease_expired_takeover")
                run = await repo.insert_run(task_id, self.runner_id, now, self.ttl)
                return AcquireResult(AcquireStatus.ACQUIRED, Lease(run.id, task_id, self.runner_id), seen[0])
        except _TakeoverRaced:
            return AcquireResult(AcquireStatus.HELD, previous_run_id=seen[0], reason="takeover raced")

    async def _judge(self, row: StaleExecution) -> TerminationResult | TrackedVerdict | None:
        """One unfinished row of the previous run: a tracked row by the tracked verdict, every other row as before."""
        if row.tracked:
            retained = await retained_job_verdict(self._retained_settler, self._retained_present, row)
            if retained is not None:
                return retained            # same Runner process: the retained Job is proven empty, or not proven (design 7.3)
            return await asyncio.to_thread(self._tracked_verdict, row)
        if row.pid is None or row.create_time is None:
            return None                                # STARTING without identity: may or may not have spawned
        return await asyncio.to_thread(self._terminate, ProcessIdentity(row.pid, row.create_time))

    @staticmethod
    def _proven(verdict) -> bool:
        if isinstance(verdict, TrackedVerdict):
            return verdict.confirmed
        return verdict is not None and verdict in CONFIRMED_GONE

    @staticmethod
    def _describe(verdict) -> str:
        if isinstance(verdict, TrackedVerdict):
            return verdict.reason or verdict.termination or "UNKNOWN"
        return str(verdict)

    @staticmethod
    async def _close_abandoned(s: AsyncSession, repo: TaskRepository, stale: list[StaleExecution], verdicts: dict) -> None:
        """Txn B closing loop. The target ids are sorted and locked with ONE ordered statement first; the UPDATEs then iterate
        the returned ordered sequence, so the order never depends on the unordered `unterminated_executions` result."""
        by_id = {row.id: row for row in stale}
        locked = await repo.lock_executions_ordered(list(by_id))
        now_seen = {r.id: r for r in (await s.execute(
            select(Execution.id, Execution.pid, Execution.process_create_time).where(Execution.id.in_(locked)))).all()}
        for exec_id in locked:
            row, verdict, seen_now = by_id[exec_id], verdicts[exec_id], now_seen[exec_id]
            if row.tracked and isinstance(verdict, TrackedVerdict):
                never_ran = verdict.proof == P.PROOF_NEVER_RAN
                if never_ran and seen_now.pid is not None:
                    raise _TakeoverRaced()             # an identity was committed after Txn A read the row: not NEVER_RAN
                if not never_ran and (seen_now.pid, seen_now.process_create_time) != (row.pid, row.create_time):
                    raise _TakeoverRaced()
                merge = {"abandoned_by_takeover": True, "proof": verdict.proof, "termination": verdict.termination,
                         "reason": verdict.reason, "lifecycle": "SPAWN_FAILED" if never_ran else "TERMINATED",
                         "tree": "EMPTY"}
                merge = {k: v for k, v in merge.items() if v is not None}
                result = await s.execute(text(
                    "UPDATE executions SET finished_at = now(), " + ("" if never_ran else "terminated_at = now(), ")
                    + "outcome = 'ERROR', detail = detail || CAST(:merge AS jsonb) "
                    "WHERE id = :id AND finished_at IS NULL" + (" AND pid IS NULL AND " + NEVER_RAN_EVIDENCE_GUARD_SQL if never_ran else "")),
                    {"id": exec_id, "merge": json.dumps(merge)})
                if result.rowcount != 1 and never_ran:
                    raise _TakeoverRaced()
            elif row.tracked_ish:
                # a wrong marker, or tracked-only keys without one: judged by today's rules, but the keys are never replaced
                await s.execute(text(
                    "UPDATE executions SET finished_at = now(), terminated_at = now(), outcome = 'ERROR', "
                    "detail = detail || CAST(:merge AS jsonb) WHERE id = :id AND finished_at IS NULL"),
                    {"id": exec_id, "merge": json.dumps({"abandoned_by_takeover": True})})
            else:
                await s.execute(update(Execution).where(Execution.id == exec_id, Execution.finished_at.is_(None))
                                .values(finished_at=func.now(), terminated_at=func.now(), outcome="ERROR",
                                        detail={"abandoned_by_takeover": True}))

    # --- ownership -------------------------------------------------------------------------
    async def verify(self, session: AsyncSession, lease: Lease) -> None:
        """Re-validate ownership inside the caller's transaction (rowcount==1 or LeaseLost)."""
        if lease.run_id in self._revoked:
            raise LeaseLost("lease revoked by this runner", {"run_id": str(lease.run_id)})
        if not await TaskRepository(session).heartbeat(lease.run_id, lease.runner_id, self._now(), self.ttl):
            raise LeaseLost("lease ownership lost", {"run_id": str(lease.run_id)})

    async def heartbeat(self, lease: Lease) -> bool:
        async with self.db.transaction() as s:
            return await TaskRepository(s).heartbeat(lease.run_id, lease.runner_id, self._now(), self.ttl)

    async def release(self, lease: Lease, reason: str, session: AsyncSession | None = None) -> None:
        async def _do(s: AsyncSession) -> None:
            await TaskRepository(s).close_run(lease.run_id, "FINISHED", reason)
        if session is not None:
            await _do(session)
        else:
            async with self.db.transaction() as s:
                await _do(s)

    def start_heartbeat(self, lease: Lease, on_lost: Callable[[], Awaitable[None] | None]) -> asyncio.Task:
        async def loop() -> None:
            interval = max(self.ttl / 3, 0.05)
            while True:
                await asyncio.sleep(interval)
                try:
                    alive = await self.heartbeat(lease)
                except Exception:  # noqa: BLE001 - DB outage: treat as lost, fail closed
                    log.exception("heartbeat failed")
                    alive = False
                if not alive:
                    res = on_lost()
                    if asyncio.iscoroutine(res):
                        await res
                    return
        return asyncio.create_task(loop())
