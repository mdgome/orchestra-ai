"""The orphan sweeper pass (design 7.4 'Orphan sweeper', 'Candidate scan', rules (a) to (h)).

`OrphanSweeper` is the injectable `sweep_pass(start, policy) -> PassOutcome` of the Runner: it collects ONE batch with the keyset
page procedure (a failed or cancelled collection discards everything), then settles the targets one at a time in ascending id,
each in its own short guarded transaction, and moves the committed cursor only over the decided prefix. OS work (the tracked
verdict) runs in a thread, never inside a database section; every database section takes `db_section()` (the Runner's shared
semaphore) and is one short transaction.
"""
import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.db import Database
from app.repositories.task_repository import TaskRepository, TrackedRow
from app.services.lease_service import (RetainedPresent, RetainedSettler, StaleExecution, TrackedVerdict, TrackedVerdictFn,
                                          default_tracked_verdict, retained_job_verdict)
from app.services.runner import PassOutcome
from app.services.sweeper_stall import CANDIDATE, PAGE_AFTER, SWEEP_NO_PROGRESS
from app.services.tracked_classifier import Classification, RowClass, classify_row, decode_detail
from app.validation import policies as P
from app.validation.validators import SweeperPassPolicy

log = logging.getLogger(__name__)

SWEEP_QUERY_FAILED = "SWEEP_QUERY_FAILED"
SWEEP_PAGE_TIMEOUT = "SWEEP_PAGE_TIMEOUT"
SWEEP_CLASSIFICATION_ERROR = "SWEEP_CLASSIFICATION_ERROR"
SWEEP_EXCEPTION = "SWEEP_EXCEPTION"

# Result states of the candidate scan (design 7.4 table); `cap` of SCAN_LIMIT_REACHED is ROW or TIME.
TARGETS_READY, NO_ELIGIBLE_TARGETS, SCAN_LIMIT_REACHED, QUERY_FAILED = (
    "TARGETS_READY", "NO_ELIGIBLE_TARGETS", "SCAN_LIMIT_REACHED", "QUERY_FAILED")
# Anomaly kinds the sweeper never rewrites: an unknown schema version is left untouched (design 7.4).
_NEVER_SETTLED_KINDS = frozenset({"UNKNOWN_SCHEMA_VERSION"})
_ALIVE_REASONS = frozenset({"ROOT_NOT_GONE", "SURVIVORS_AFTER_SETTLE"})
_ANOMALY_ORIGINAL_KEYS = ("proof", "disposition", "risk_acceptance", "v", "reason_code")


def sqlstate_of(exc: BaseException) -> str | None:
    """The SQLSTATE of a database error (`exc.orig.sqlstate`, asyncpg), never the exception class or text."""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)


@dataclass
class ScanResult:
    state: str
    targets: list[tuple[TrackedRow, Classification]] = field(default_factory=list)
    scan_cursor: uuid.UUID | None = None
    cap: str | None = None                       # ROW | TIME for SCAN_LIMIT_REACHED
    failure: str | None = None                   # a SWEEP_* code for QUERY_FAILED
    failure_position: uuid.UUID | None = None
    failure_kind: str = PAGE_AFTER
    scanned: int = 0
    decode_errors: int = 0


class OrphanSweeper:
    def __init__(self, db: Database, db_section: Callable[[], object], *, incarnation: Callable[[], str | None] = lambda: None,
                 tracked_verdict: TrackedVerdictFn = default_tracked_verdict, retained_settler: RetainedSettler | None = None,
                 retained_present: RetainedPresent | None = None, clock: Callable[[], float] = time.monotonic, run_blocking: Callable | None = None):
        self.db, self._section, self._incarnation = db, db_section, incarnation
        self._verdict, self._retained, self._clock = tracked_verdict, retained_settler, clock
        self._retained_present = retained_present
        self._blocking = run_blocking or asyncio.to_thread

    # --- the pass --------------------------------------------------------------------------------------------------------
    async def __call__(self, start: uuid.UUID | None, policy: SweeperPassPolicy) -> PassOutcome:
        pass_deadline = self._clock() + policy.pass_seconds
        scan = await self.collect(start, policy.batch_rows)
        if scan.state == QUERY_FAILED:                       # rule (f): discard, the committed cursor stays where it was
            return PassOutcome("FAILED", committed=start, code=scan.failure, position=scan.failure_position,
                               position_kind=scan.failure_kind)
        decided: list[uuid.UUID] = []
        failed: tuple[str, uuid.UUID] | None = None
        for row, cls in scan.targets:                        # rule (h): ascending id, one at a time, never in parallel
            if self._clock() >= pass_deadline:
                break                                        # no processing is started once the budget is spent
            try:
                if not await self._settle(row, cls):
                    break                                    # not decided: the cursor never passes it
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a non-isolated error of the processing ends the pass (rule (g)(iv))
                state = sqlstate_of(exc)
                code = SWEEP_PAGE_TIMEOUT if state == "57014" else SWEEP_QUERY_FAILED if state else SWEEP_EXCEPTION
                log.warning("sweeper row failed: id=%s code=%s", row.id, code)
                failed = (code, row.id)
                break
            decided.append(row.id)
        prefix = decided[-1] if decided else None
        if failed is not None:
            return PassOutcome("FAILED", committed=prefix if prefix is not None else start, advanced=prefix is not None,
                               code=failed[0], position=failed[1], position_kind=CANDIDATE)
        all_decided = len(decided) == len(scan.targets)
        if all_decided:
            if scan.state == NO_ELIGIBLE_TARGETS or (scan.state == TARGETS_READY and scan.cap == "END"):
                return PassOutcome("OK", committed=None, advanced=False, wrapped=True)         # rule (g)(i)
            committed = scan.scan_cursor                                                     # rule (g)(ii)
            return PassOutcome("OK", committed=committed if committed is not None else start,
                               advanced=committed is not None and committed != start)
        if not decided:                                                                      # zero decided: no progress
            return PassOutcome("NO_PROGRESS", committed=start, code=SWEEP_NO_PROGRESS, position=scan.targets[0][0].id,
                               position_kind=CANDIDATE)
        return PassOutcome("OK", committed=prefix, advanced=True)                            # rule (g)(iii)

    # --- collection ------------------------------------------------------------------------------------------------------
    async def collect(self, start: uuid.UUID | None, batch_rows: int) -> ScanResult:
        """The page procedure of 7.4 (steps 1 to 9). The scan cursor advances only over classified rows."""
        scan_start = self._clock()
        deadline = scan_start + P.CANDIDATE_SCAN_MAX_SECONDS
        cursor, scanned = start, 0
        targets: list[tuple[TrackedRow, Classification]] = []
        decode_errors = 0
        while True:
            remaining_targets = batch_rows - len(targets)
            if remaining_targets <= 0:
                return ScanResult(TARGETS_READY, targets, cursor, "FULL", scanned=scanned, decode_errors=decode_errors)
            page_size = min(P.CANDIDATE_PAGE_ROWS, P.CANDIDATE_MAX_SCANNED_ROWS - scanned, remaining_targets)
            if page_size <= 0:
                return self._limited(targets, cursor, "ROW", scanned, decode_errors)
            remaining = deadline - self._clock()
            timeout_ms = int(min(P.CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS, remaining) * 1000)
            if timeout_ms < 1:
                return self._limited(targets, cursor, "TIME", scanned, decode_errors)
            try:
                async with self._section():
                    async with self.db.transaction() as s:
                        repo = TaskRepository(s)
                        await repo.set_local_statement_timeout(timeout_ms)
                        page = await repo.sweep_page(cursor, page_size)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                state = sqlstate_of(exc)
                code = SWEEP_PAGE_TIMEOUT if state == "57014" else SWEEP_QUERY_FAILED
                log.warning("sweeper page failed: after=%s code=%s", cursor, code)
                return ScanResult(QUERY_FAILED, [], start, failure=code, failure_position=cursor, failure_kind=PAGE_AFTER)
            for row in page:
                try:
                    cls = classify_row(row.id, row.pid, row.finished_at, row.detail_text)
                except Exception:  # noqa: BLE001 - not isolated per row: the whole pass fails and nothing is processed
                    log.warning("sweeper classification failed: id=%s", row.id)
                    return ScanResult(QUERY_FAILED, [], start, failure=SWEEP_CLASSIFICATION_ERROR, failure_position=row.id,
                                      failure_kind=CANDIDATE)
                cursor = row.id                              # rule (a): a decode_error row counts as classified
                scanned += 1
                if cls.sub_reason == "decode_error":
                    decode_errors += 1                       # rule (10): skipped, never written, its detail never logged
                    continue
                if cls.is_unresolved or (cls.is_anomaly and cls.anomaly_kind not in _NEVER_SETTLED_KINDS):
                    targets.append((row, cls))
            if len(page) < page_size:                        # step (9): only a short page shows the end of the set
                state = TARGETS_READY if targets else NO_ELIGIBLE_TARGETS
                return ScanResult(state, targets, cursor, "END", scanned=scanned, decode_errors=decode_errors)

    @staticmethod
    def _limited(targets, cursor, cap, scanned, decode_errors) -> ScanResult:
        state = TARGETS_READY if targets else SCAN_LIMIT_REACHED
        return ScanResult(state, targets, cursor, cap, scanned=scanned, decode_errors=decode_errors)

    # --- settlement of one row -------------------------------------------------------------------------------------------
    async def _verdict_of(self, row: TrackedRow, cls: Classification) -> TrackedVerdict:
        detail = decode_detail(row.detail_text) if row.detail_text else {}
        stale = StaleExecution(row.id, row.pid, row.process_create_time, detail if isinstance(detail, dict) else None)
        if cls.is_unresolved:
            retained = await retained_job_verdict(self._retained, self._retained_present, stale)
            if retained is not None:
                return retained
        return await self._blocking(self._verdict, stale)

    async def _settle(self, row: TrackedRow, cls: Classification) -> bool:
        """True when the row is decided (committed or no-write proven), False when it is not (the cursor stops before it)."""
        verdict = await self._verdict_of(row, cls)                     # OS work: no database section is open
        if not verdict.confirmed:
            return True                                                # unprovable: left untouched, no-write proven
        never_ran = verdict.proof == P.PROOF_NEVER_RAN
        async with self._section():
            async with self.db.transaction() as s:
                repo = TaskRepository(s)
                if cls.is_unresolved:
                    merge = {"abandoned_by_sweeper": True, "proof": verdict.proof, "termination": verdict.termination,
                             "reason": verdict.reason, "lifecycle": "SPAWN_FAILED" if never_ran else "TERMINATED",
                             "tree": "EMPTY"}
                    wrote = await repo.write_proof_to_unresolved(
                        row, merge={k: v for k, v in merge.items() if v is not None}, close=row.finished_at is None)
                else:
                    detail = decode_detail(row.detail_text)
                    keys = [k for k in _ANOMALY_ORIGINAL_KEYS if k in detail]
                    if detail.get("tracked") is not True and "tracked" in detail:
                        keys.append("tracked")
                    settlement = {"kind": cls.anomaly_kind, "verdict": verdict.proof,
                                  "settled_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                                  "incarnation": self._incarnation()}
                    wrote = await repo.settle_anomaly(row, proof=verdict.proof, termination=verdict.termination,
                                                      settlement=settlement, original_keys=keys)
                if wrote:
                    return True
                again = await repo.tracked_row(row.id)                 # 0 rows: re-read, never write twice
        if again is None:
            return True                                                # deleted by another path: left the stream
        now_cls = classify_row(again.id, again.pid, again.finished_at, again.detail_text)
        return now_cls.row_class in (RowClass.RESOLVED_PROVEN, RowClass.RESOLVED_RISK, RowClass.LEGACY)
