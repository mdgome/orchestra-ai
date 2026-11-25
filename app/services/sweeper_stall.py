"""Sweeper stall record and the persistent configuration-invalid state (design 7.4 'Sweeper stall visibility', rows 1 to 32 of
its transition table, CI-1 to CI-7). Pure: no I/O, no clock of its own, no Runner import. The Runner owns one instance (in
memory only); this module is the specification of the transition table and nothing else.

Positions are execution ids (uuid.UUID) or None (None ranks below every id); a failure position has a kind, CANDIDATE (a row
that failed) or PAGE_AFTER (the boundary before a page that could not be read). The committed cursor of a pass is passed in as
`committed` (None = no cursor) together with the flags `advanced` and `wrapped`.
"""
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.validation import policies as P
from app.validation.validators import check_sweep_fallback

CANDIDATE = "CANDIDATE"
PAGE_AFTER = "PAGE_AFTER"
SWEEP_CONFIG_INVALID = "SWEEP_CONFIG_INVALID"
# The fixed validation reasons of the configuration-invalid state (design 7.4 CI-1, 9.4): never free text, a constant name or a value.
SWEEPER_CONFIG_INVALID_REASONS = ("TYPE", "NON_FINITE", "OUT_OF_RANGE", "RELATION", "OVERFLOW", "DATETIME_LIMIT")
SWEEP_NO_PROGRESS = "SWEEP_NO_PROGRESS"
FAILURE_CODES = frozenset({"SWEEP_QUERY_FAILED", "SWEEP_PAGE_TIMEOUT", "SWEEP_CLASSIFICATION_ERROR", "SWEEP_EXCEPTION",
                           "SWEEP_CANCELLED", SWEEP_NO_PROGRESS, SWEEP_CONFIG_INVALID})


class SweeperFallbackInvalid(ValueError):
    """Table row 28: the fallback itself is corrupted or uncomputable when it is used. Nothing is written; the pass ends as an
    abnormal exit that the supervisor handles."""


@dataclass(frozen=True)
class SweeperStallSnapshot:
    """The external frozen view (the safe subset). The two component fields, the position kinds and the config_invalid_* fields
    stay internal; the exposed part of the configuration-invalid state is `SweeperConfigInvalidSnapshot`."""
    stalled: bool
    stalled_since: datetime | None
    pass_start_cursor: uuid.UUID | None
    first_failure_cursor: uuid.UUID | None
    current_failure_cursor: uuid.UUID | None
    consecutive_failures: int
    last_failure_code: str | None
    last_failure_at: datetime | None
    next_retry_at: datetime | None


@dataclass(frozen=True)
class SweeperConfigInvalidSnapshot:
    config_invalid_active: bool
    config_invalid_detected_at: datetime | None
    config_invalid_trigger_position: uuid.UUID | None
    config_invalid_next_retry_at: datetime | None
    config_invalid_reasons: tuple[str, ...]
    config_currently_valid: bool | None
    config_evaluated_at: datetime | None
    clearance_pending: bool
    next_recheck_at: datetime | None = None     # the effective time of the next evaluation the Runner SCHEDULED (null: none or unknown)

    # The attribute names of the API model SweeperConfigInvalidOut (design 14.2: equal names make `from_attributes` work).
    @property
    def active(self) -> bool:
        return self.config_invalid_active

    @property
    def detected_at(self) -> datetime | None:
        return self.config_invalid_detected_at

    @property
    def trigger_position(self) -> uuid.UUID | None:
        return self.config_invalid_trigger_position

    @property
    def next_retry_at(self) -> datetime | None:
        return self.config_invalid_next_retry_at          # the audit value of the trigger pass, never a schedule

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.config_invalid_reasons


def backoff_delay(consecutive_failures: int, minimum: float, maximum: float, exponent_cap: int) -> float:
    """Saturating exponential backoff (design 7.4 'Retry rhythm'): the exponent is bounded BEFORE any power is taken, so the
    result is finite and inside [minimum, maximum] for every count, a huge integer included."""
    failures = max(consecutive_failures, 1)
    m1, e1 = math.frexp(minimum)
    m2, e2 = math.frexp(maximum)
    sat = max(0, (e2 - e1) + (0 if m1 >= m2 else 1))          # the minimal exponent with minimum * 2 ** sat >= maximum
    exponent = min(failures - 1, exponent_cap, sat)
    try:
        return float(min(maximum, math.ldexp(minimum, exponent)))
    except OverflowError:                                     # only for limits near the float range: saturated
        return float(maximum)


def retry_at(now: datetime, seconds) -> datetime:
    """`now + seconds`; a valid sum that leaves the datetime range stores datetime.max in UTC (D35-4)."""
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError:
        return datetime.max.replace(tzinfo=UTC)


def _passed(position, kind: str | None, committed, advanced: bool, wrapped: bool) -> bool:
    """Has this failure position been passed by the rule of its own kind (a wrap-around passes every position)?"""
    if wrapped:
        return True
    if not advanced:
        return False
    if position is None:
        return True                                  # the first page: an advance of at least one row
    if committed is None:
        return False
    return committed >= position if kind == CANDIDATE else committed > position


@dataclass
class SweeperStallState:
    threshold: int = P.ORPHAN_SWEEP_STALL_THRESHOLD
    # --- the ordinary stall record (rows 1 to 13) ---
    has_record: bool = False
    stalled: bool = False
    stalled_since: datetime | None = None
    pass_start_cursor: uuid.UUID | None = None
    consecutive_failures: int = 0
    first_failure_cursor: uuid.UUID | None = None
    first_failure_kind: str | None = None
    current_failure_cursor: uuid.UUID | None = None
    current_failure_kind: str | None = None
    last_failure_code: str | None = None
    last_failure_at: datetime | None = None
    next_retry_at: datetime | None = None
    # --- the persistent configuration-invalid state (CI-1; independent of the record, CI-4) ---
    config_invalid_active: bool = False
    config_invalid_detected_at: datetime | None = None
    config_invalid_trigger_position: uuid.UUID | None = None
    config_invalid_trigger_kind: str | None = None
    config_invalid_trigger_failure_code: str | None = None
    config_invalid_next_retry_at: datetime | None = None
    config_invalid_reasons: tuple[str, ...] = ()
    config_currently_valid: bool | None = None
    config_evaluated_at: datetime | None = None
    wake_pending: bool = False
    # The instant of the next evaluation of the configuration guard that the sweeper has scheduled (design 14.2): the end of the
    # debounce while a wake-up is pending, otherwise the end of the pass wait; None while no wait is scheduled (a pass runs, the
    # task is gone, nothing recorded). It is NOT `next_retry_at` (the retry schedule / audit value) and never bumps the revision.
    next_recheck_at: datetime | None = None
    _events: list = field(default_factory=list)

    # ---------------------------------------------------------------------------------------------------- failures
    def _apply_failure(self, *, pass_start, position, kind, code, now, next_retry_at) -> int:
        """Rows 1 to 4 (and 13, 16, 22, 23 through them). Returns the row number that applied."""
        if not self.has_record:
            row = 1
            self.has_record, self.consecutive_failures = True, 1
            self.pass_start_cursor = pass_start
            self.first_failure_cursor, self.first_failure_kind = position, kind
        elif pass_start == self.pass_start_cursor:
            row = 4 if self.stalled else 2
            self.consecutive_failures += 1
        elif not self.stalled:
            row = 3                                    # replaced: the delay restarts at the minimum
            self.consecutive_failures = 1
            self.pass_start_cursor = pass_start
            self.first_failure_cursor, self.first_failure_kind = position, kind
            self.stalled_since = None
        else:
            row = 4                                    # pinned, not replaced
            self.consecutive_failures += 1
            self.pass_start_cursor = pass_start
        self.current_failure_cursor, self.current_failure_kind = position, kind
        self.last_failure_code, self.last_failure_at, self.next_retry_at = code, now, next_retry_at
        if not self.stalled and self.consecutive_failures >= self.threshold:
            self.stalled, self.stalled_since = True, now
        return row

    def peek_count(self, pass_start) -> int:
        """The `consecutive_failures` the record WILL have after a failed pass that starts at `pass_start` (the backoff input)."""
        if not self.has_record:
            return 1
        if pass_start == self.pass_start_cursor or self.stalled:
            return self.consecutive_failures + 1
        return 1

    def record_failed_pass(self, *, pass_start, position, kind: str, code: str, now: datetime,
                           retry_delay_seconds: float) -> int:
        """A failed pass (rows 1 to 4; row 13 for SWEEP_NO_PROGRESS and row 16 for SWEEP_CANCELLED name the code)."""
        if code not in FAILURE_CODES or code == SWEEP_CONFIG_INVALID:
            raise ValueError(f"not an ordinary failure code: {code!r}")
        return self._apply_failure(pass_start=pass_start, position=position, kind=kind, code=code, now=now,
                                   next_retry_at=retry_at(now, retry_delay_seconds))

    def record_config_invalid(self, *, pass_start, boundary_cursor, now: datetime, fallback_seconds,
                              reasons: tuple[str, ...] = ()) -> int:
        """Rows 22, 23 and 30. The FIRST step is `now + fallback` (row 28: a corrupted fallback raises
        `SweeperFallbackInvalid` and nothing is written, the record and the count stay as they were)."""
        try:
            check_sweep_fallback(fallback_seconds)
        except ValueError as exc:
            raise SweeperFallbackInvalid(str(exc)) from exc
        next_retry = retry_at(now, fallback_seconds)
        prior = self.last_failure_code if self.has_record else None
        if self.has_record and pass_start == self.pass_start_cursor:       # row 22: the position of the record stays
            position, kind = self.current_failure_cursor, self.current_failure_kind
        else:                                                              # row 23: the boundary before the unread page
            position, kind = boundary_cursor, PAGE_AFTER
        row = self._apply_failure(pass_start=pass_start, position=position, kind=kind, code=SWEEP_CONFIG_INVALID,
                                  now=now, next_retry_at=next_retry)
        trigger = prior if prior not in (None, SWEEP_CONFIG_INVALID) else self.config_invalid_trigger_failure_code
        # one atomic replacement of the whole context (row 30 when it was already active)
        self.config_invalid_active = True
        self.config_invalid_detected_at = now
        self.config_invalid_trigger_position, self.config_invalid_trigger_kind = position, kind
        self.config_invalid_trigger_failure_code = trigger
        self.config_invalid_next_retry_at = next_retry
        self.config_invalid_reasons = tuple(r for r in SWEEPER_CONFIG_INVALID_REASONS if r in set(reasons))   # fixed vocabulary only
        self.config_currently_valid, self.config_evaluated_at = False, now
        return row

    # ------------------------------------------------------------------------------------------------------ success
    def record_pass_without_failure(self, *, committed, advanced: bool, wrapped: bool, config_valid: bool,
                                    now: datetime) -> dict:
        """Rows 5 to 11 for the ordinary record and rows 25 and 26 for the configuration-invalid state, independently (CI-4).
        Returns {'record': 'cleared'|'kept', 'config': 'cleared'|'kept'|'none'}."""
        out = {"record": "kept", "config": "none"}
        progress = advanced or wrapped
        if self.has_record and progress:
            if (_passed(self.first_failure_cursor, self.first_failure_kind, committed, advanced, wrapped)
                    and _passed(self.current_failure_cursor, self.current_failure_kind, committed, advanced, wrapped)):
                self._clear_record()
                out["record"] = "cleared"
        self.config_currently_valid, self.config_evaluated_at = config_valid, now
        if self.config_invalid_active:
            out["config"] = "kept"
            if (config_valid and progress and _passed(self.config_invalid_trigger_position, self.config_invalid_trigger_kind,
                                                      committed, advanced, wrapped)):
                self._clear_config()
                out["config"] = "cleared"
        return out

    def _clear_record(self) -> None:
        self.has_record = self.stalled = False
        self.stalled_since = self.pass_start_cursor = None
        self.consecutive_failures = 0
        self.first_failure_cursor = self.first_failure_kind = None
        self.current_failure_cursor = self.current_failure_kind = None
        self.last_failure_code = self.last_failure_at = self.next_retry_at = None

    def _clear_config(self) -> None:
        self.config_invalid_active = False
        self.config_invalid_detected_at = self.config_invalid_trigger_position = None
        self.config_invalid_trigger_kind = self.config_invalid_trigger_failure_code = None
        self.config_invalid_next_retry_at = None
        self.config_invalid_reasons = ()
        self.wake_pending = False

    def note_config_evaluation(self, valid: bool, now: datetime) -> None:
        self.config_currently_valid, self.config_evaluated_at = valid, now

    def reset(self) -> None:
        """Row 21: the process or the Runner object is replaced. (A stop and start of the SAME object keeps everything, row 31.)"""
        self._clear_record()
        self._clear_config()
        self.config_currently_valid = self.config_evaluated_at = None
        self.next_recheck_at = None

    # ------------------------------------------------------------------------------------------------------ views
    def snapshot(self) -> SweeperStallSnapshot:
        return SweeperStallSnapshot(self.stalled, self.stalled_since, self.pass_start_cursor, self.first_failure_cursor,
                                    self.current_failure_cursor, self.consecutive_failures, self.last_failure_code,
                                    self.last_failure_at, self.next_retry_at)

    def config_snapshot(self) -> SweeperConfigInvalidSnapshot:
        return SweeperConfigInvalidSnapshot(
            self.config_invalid_active, self.config_invalid_detected_at, self.config_invalid_trigger_position,
            self.config_invalid_next_retry_at, self.config_invalid_reasons, self.config_currently_valid,
            self.config_evaluated_at, self.config_invalid_active and bool(self.config_currently_valid), self.next_recheck_at)

    @property
    def exists(self) -> bool:
        """A stall record OR a configuration-invalid state (CI-only included) exists (rows 31 and 21)."""
        return self.has_record or self.config_invalid_active
