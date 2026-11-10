"""The request-recovery transaction and its evidence, isolated from `Database.transaction()`.

Ownership: `run_recovery_transaction` is the only owner of the session, its transaction and its connection. The body only
borrows the session. Ownership ends when the connection is RELEASED (pool return after commit/rollback + close, or a
discard through `invalidate()`); until then the session stays referenced from the evidence so a later cleanup attempt can
still settle it.

Two independent axes are tracked:
  * DB outcome       what happened to the data. Decided by the PHASE the transaction was in, never by guessing from an
                     exception type: COMMITTED (commit() returned), ROLLED_BACK / FAILED_BEFORE_COMMIT (COMMIT was never
                     sent, so nothing can have been committed), UNCERTAIN (COMMIT was sent and its result is unknown).
  * Resource outcome whether the session/connection is given back: RELEASED, PENDING (close/invalidate still running),
                     FAILED (finished without a confirmed release).
A failing close never turns a COMMITTED/ROLLED_BACK result into UNCERTAIN, and a confirmed commit never implies the
connection is released.

`statement_timeout` (set transaction-locally) only limits statements on the server. It does not bound the COMMIT round
trip, the network or close(); those are bounded from outside by the Runner's operation / shutdown waits.
"""
import asyncio
import enum
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tables import ExecutionRequest

log = logging.getLogger(__name__)


class Phase(enum.Enum):
    PREPARE = "PREPARE"          # session / connection checkout
    BODY = "BODY"                # statement_timeout + request changes: nothing is committed yet
    COMMITTING = "COMMITTING"    # set just BEFORE commit() is called
    DONE = "DONE"                # commit() returned


class DbOutcome(enum.Enum):
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED_BEFORE_COMMIT = "FAILED_BEFORE_COMMIT"
    UNCERTAIN = "UNCERTAIN"


class ResourceOutcome(enum.Enum):
    RELEASED = "RELEASED"
    PENDING = "PENDING"
    FAILED = "FAILED"


class RecoveryOutcome(enum.Enum):
    """The `outcome` key of a dict `Runner.recover` returns. Every other result is an exception (`RecoveryTimeout`,
    `RecoveryUnsettled`, the original body error, the caller's CancelledError)."""
    COMMITTED = "COMMITTED"
    SKIPPED = "SKIPPED"


class Intent(enum.Enum):
    REQUEUE = "REQUEUE"
    FINISH_DONE = "FINISH_DONE"


class Verdict(enum.Enum):
    """Result of the read-only reconciliation of an UNCERTAIN recovery."""
    NO_CHANGES = "NO_CHANGES"
    ROLLED_BACK = "ROLLED_BACK"
    COMMITTED = "COMMITTED"
    UNRESOLVED = "UNRESOLVED"


@dataclass
class Candidate:
    request_id: uuid.UUID
    original_claim_token: uuid.UUID | None
    intent: Intent
    original_status: str = "CLAIMED"
    update_applied: bool | None = None       # None = not attempted yet, False = fenced (rowcount 0), True = applied


@dataclass
class RecoveryEvidence:
    """Mutable and readable from outside the operation task, so it survives a cancelled / stalled task."""
    phase: Phase = Phase.PREPARE
    candidates: list[Candidate] = field(default_factory=list)
    candidates_complete: bool = False        # candidates AND intents are final; set before the first UPDATE
    fenced_count: int = 0
    db_outcome: DbOutcome | None = None
    txn_closed: bool = False                 # commit()/rollback() returned
    conn_released: bool = False              # close() or invalidate() returned
    session: AsyncSession | None = None      # kept until released: the ownership needed to settle again


@dataclass
class RecoveryResult:
    db: DbOutcome | None
    resource: ResourceOutcome
    evidence: RecoveryEvidence
    summary: dict | None = None
    error: BaseException | None = None       # the body error behind FAILED_BEFORE_COMMIT / UNCERTAIN
    cancelled: bool = False                  # the operation was cancelled (by whoever; the Runner knows why)
    cleanup_errors: list[BaseException] = field(default_factory=list)

    @property
    def released(self) -> bool:
        return self.resource is ResourceOutcome.RELEASED


_CLEANUP_ATTEMPTS = 3          # an invalidate() interrupted by a repeated cancellation is simply tried again


async def _attempt(step, errors: list[BaseException]) -> bool:
    try:
        await step()
        return True
    except (Exception, asyncio.CancelledError) as exc:
        errors.append(exc)
        return False


async def release_session(session: AsyncSession, evidence: RecoveryEvidence, errors: list[BaseException], *,
                          rollback: bool, discard: bool) -> ResourceOutcome:
    """Gives the session's connection back. Normal path: [rollback ->] close (pool return). A failed rollback/close, or
    `discard` (commit result unknown), ends in `invalidate()`, which discards the connection and closes the session.
    Nothing here is `shield`ed: a cancellation interrupts a step and the next step of the ladder runs; the outside
    wait is what bounds the whole thing (a task that cannot finish stays live = PENDING)."""
    if not discard:
        rolled_back = await _attempt(session.rollback, errors) if rollback else True
        evidence.txn_closed = evidence.txn_closed or rolled_back
        if rolled_back and await _attempt(session.close, errors):
            evidence.txn_closed = True
            evidence.conn_released = True
            evidence.session = None
            return ResourceOutcome.RELEASED
    for _ in range(_CLEANUP_ATTEMPTS):
        if await _attempt(session.invalidate, errors):
            evidence.conn_released = True
            evidence.session = None
            return ResourceOutcome.RELEASED
    return ResourceOutcome.FAILED


async def resettle_resources(evidence: RecoveryEvidence) -> RecoveryResult:
    """Another attempt to release a session that an earlier operation could not release (CLEANUP operation)."""
    errors: list[BaseException] = []
    session = evidence.session
    resource = (ResourceOutcome.RELEASED if session is None or evidence.conn_released
                else await release_session(session, evidence, errors, rollback=False, discard=True))
    return RecoveryResult(evidence.db_outcome, resource, evidence, cleanup_errors=errors)


async def run_recovery_transaction(sessions: async_sessionmaker, body, evidence: RecoveryEvidence,
                                   statement_timeout_ms: int) -> RecoveryResult:
    """One request-recovery transaction. `body(session, evidence)` does the work and returns a summary dict.
    Never raises for a body/commit/cleanup problem: everything is reported through the RecoveryResult (a task that is
    cancelled before it even starts has no result at all, and has also acquired nothing)."""
    session = sessions()
    evidence.session = session
    errors: list[BaseException] = []
    result = RecoveryResult(None, ResourceOutcome.PENDING, evidence, cleanup_errors=errors)
    failure: BaseException | None = None
    try:
        evidence.phase = Phase.PREPARE
        await session.connection()                                 # checkout (bounded by the pool timeout)
        evidence.phase = Phase.BODY
        await session.execute(text("select set_config('statement_timeout', :ms, true)"),
                              {"ms": str(statement_timeout_ms)})
        result.summary = await body(session, evidence)
        evidence.phase = Phase.COMMITTING                          # BEFORE the call: a failure inside is UNCERTAIN
        await session.commit()
        evidence.phase = Phase.DONE
        evidence.txn_closed = True
        evidence.db_outcome = result.db = DbOutcome.COMMITTED
    except (Exception, asyncio.CancelledError) as exc:             # noqa: BLE001 - classified by phase, never hidden
        failure = exc
        if isinstance(exc, asyncio.CancelledError):
            result.cancelled = True
        else:
            result.error = exc
        if evidence.phase is Phase.COMMITTING:
            evidence.db_outcome = result.db = DbOutcome.UNCERTAIN
        else:                                                      # COMMIT was never sent
            evidence.db_outcome = result.db = (DbOutcome.ROLLED_BACK if result.cancelled
                                               else DbOutcome.FAILED_BEFORE_COMMIT)
    if failure is None:
        result.resource = await release_session(session, evidence, errors, rollback=False, discard=False)
    else:
        uncertain = result.db is DbOutcome.UNCERTAIN
        result.resource = await release_session(session, evidence, errors, rollback=not uncertain, discard=uncertain)
    for err in errors:
        log.warning("recovery transaction cleanup problem: %r", err)
    return result


# --- reconciliation of an UNCERTAIN recovery (read-only) ----------------------------------------------------------
def applied_candidates(original: RecoveryEvidence) -> list[Candidate]:
    return [c for c in original.candidates if c.update_applied is True]


def reconcile_precheck(original: RecoveryEvidence) -> Verdict | None:
    """The decisions that need no query. Fixed order (explicit branches: the empty applied set must never reach the
    all()-based classification, where ORIGINAL and INTENDED would both be vacuously true):
      1. candidates incomplete or any candidate not attempted (unknown)   -> UNRESOLVED
      2. nothing was applied (no candidates, or every one fenced)         -> NO_CHANGES
    Fenced candidates (update_applied False, a normal rowcount 0) are not part of the commit decision at all."""
    if not original.candidates_complete or any(c.update_applied is None for c in original.candidates):
        return Verdict.UNRESOLVED
    if not applied_candidates(original):
        return Verdict.NO_CHANGES
    return None


def judge_rows(original: RecoveryEvidence, rows: list[tuple]) -> Verdict:
    """`rows` = (id, status, claim_token) of ONE `SELECT ... WHERE id IN (applied ids)`. Needs a non-empty applied set."""
    applied = {c.request_id: c for c in applied_candidates(original)}
    ids = [r[0] for r in rows]
    if not applied or len(ids) != len(applied) or len(set(ids)) != len(ids) or set(ids) != set(applied):
        return Verdict.UNRESOLVED                                  # missing / duplicate / foreign rows
    states = []
    for request_id, status, token in rows:
        cand = applied[request_id]
        if status == cand.original_status and token == cand.original_claim_token:
            states.append("ORIGINAL")
        elif ((cand.intent is Intent.REQUEUE and status == "PENDING" and token is None)
              or (cand.intent is Intent.FINISH_DONE and status == "DONE")):
            states.append("INTENDED")
        else:
            states.append("OTHER")
    if all(s == "ORIGINAL" for s in states):
        return Verdict.ROLLED_BACK
    if all(s == "INTENDED" for s in states):
        return Verdict.COMMITTED
    return Verdict.UNRESOLVED                                      # mixed or OTHER


async def reconcile_body(original: RecoveryEvidence, session: AsyncSession, _evidence: RecoveryEvidence) -> dict:
    """Read-only: a single SELECT for every applied candidate (one statement snapshot). Relies on the Runner being
    STOPPING / loop cancelled with the advisory lock held: nobody this Runner owns changes those rows meanwhile."""
    pre = reconcile_precheck(original)
    if pre is not None:
        return {"verdict": pre}
    ids = [c.request_id for c in applied_candidates(original)]
    rows = (await session.execute(select(ExecutionRequest.id, ExecutionRequest.status, ExecutionRequest.claim_token)
                                  .where(ExecutionRequest.id.in_(ids)))).all()
    return {"verdict": judge_rows(original, [tuple(r) for r in rows])}
