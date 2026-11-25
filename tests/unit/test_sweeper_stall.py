"""SweeperStallState: the transition table of design 7.4 (rows 1 to 13, 16, 21 to 32), the persistent configuration-invalid state
(CI-1 to CI-5), the saturating backoff and the corrupted fallback (row 28). Pure: no event loop, no database."""
import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.sweeper_stall import (
    CANDIDATE, PAGE_AFTER, SWEEP_CONFIG_INVALID, SweeperFallbackInvalid, SweeperStallState, backoff_delay, retry_at)
from app.validation import policies as P

T = 3
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def u(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def state(threshold=T) -> SweeperStallState:
    return SweeperStallState(threshold=threshold)


def fail(st, start=None, pos=None, kind=PAGE_AFTER, code="SWEEP_QUERY_FAILED", delay=30.0):
    return st.record_failed_pass(pass_start=start, position=pos, kind=kind, code=code, now=NOW, retry_delay_seconds=delay)


def ok(st, committed=None, advanced=False, wrapped=False, valid=True):
    return st.record_pass_without_failure(committed=committed, advanced=advanced, wrapped=wrapped, config_valid=valid, now=NOW)


# --- rows 1 to 4: counting, replacement and pinning --------------------------------------------------------------------------
def test_row_1_first_failure_creates_the_record():
    st = state()
    assert fail(st, u(5), u(7), CANDIDATE) == 1
    assert (st.has_record, st.consecutive_failures, st.stalled) == (True, 1, False)
    assert (st.first_failure_cursor, st.current_failure_cursor) == (u(7), u(7))
    assert st.next_retry_at == NOW + timedelta(seconds=30)


def test_row_1_with_threshold_one_stalls_at_once():
    st = state(1)
    fail(st)
    assert st.stalled and st.stalled_since == NOW


def test_row_2_same_start_counts_up_to_the_threshold_and_keeps_the_first_position():
    st = state()
    fail(st, u(1), u(10), CANDIDATE)
    assert fail(st, u(1), u(11), CANDIDATE) == 2
    assert not st.stalled and st.consecutive_failures == 2
    fail(st, u(1), u(12), CANDIDATE)
    assert st.stalled and st.consecutive_failures == 3
    assert (st.first_failure_cursor, st.current_failure_cursor) == (u(10), u(12))


def test_row_3_a_different_start_replaces_a_pending_record():
    st = state()
    fail(st, u(1), u(10), CANDIDATE)
    fail(st, u(1), u(10), CANDIDATE)
    assert fail(st, u(2), u(20), CANDIDATE) == 3
    assert (st.consecutive_failures, st.pass_start_cursor, st.first_failure_cursor) == (1, u(2), u(20))


def test_row_4_a_stalled_record_is_pinned_not_replaced():
    st = state()
    for _ in range(T):
        fail(st, u(1), u(10), CANDIDATE)
    since = st.stalled_since
    assert fail(st, u(2), u(30), CANDIDATE) == 4
    assert st.stalled and st.stalled_since == since and st.consecutive_failures == T + 1
    assert (st.first_failure_cursor, st.current_failure_cursor, st.pass_start_cursor) == (u(10), u(30), u(2))


def test_row_13_no_progress_accumulates_like_any_failed_pass():
    st = state()
    for _ in range(T):
        fail(st, u(1), u(4), CANDIDATE, code="SWEEP_NO_PROGRESS")
    assert st.stalled and st.last_failure_code == "SWEEP_NO_PROGRESS"


def test_an_unknown_or_config_code_is_not_an_ordinary_failure():
    st = state()
    with pytest.raises(ValueError):
        fail(st, code="SWEEP_BOGUS")
    with pytest.raises(ValueError):
        fail(st, code=SWEEP_CONFIG_INVALID)


# --- rows 5 to 12: clearing by position kind ---------------------------------------------------------------------------------
def test_row_5_candidate_position_is_passed_at_equality():
    st = state()
    fail(st, u(1), u(10), CANDIDATE)
    assert ok(st, committed=u(9), advanced=True)["record"] == "kept"
    assert ok(st, committed=u(10), advanced=True)["record"] == "cleared"
    assert not st.has_record and st.consecutive_failures == 0


def test_row_6_page_after_position_needs_a_strict_advance():
    st = state()
    fail(st, u(1), u(10), PAGE_AFTER)
    assert ok(st, committed=u(10), advanced=True)["record"] == "kept"     # equality: the page after it was never read
    assert ok(st, committed=u(11), advanced=True)["record"] == "cleared"


def test_first_page_null_position_clears_on_any_advance_but_not_on_a_repeated_failure():
    st = state()
    fail(st, None, None, PAGE_AFTER)
    assert ok(st, committed=None, advanced=False)["record"] == "kept"      # row 11
    assert ok(st, committed=u(1), advanced=True)["record"] == "cleared"


def test_row_7_mixed_kinds_each_by_their_own_rule():
    st = state()
    fail(st, u(1), u(10), CANDIDATE)
    fail(st, u(1), u(20), PAGE_AFTER)
    assert ok(st, committed=u(20), advanced=True)["record"] == "kept"      # the PAGE_AFTER at equality is not passed
    assert ok(st, committed=u(21), advanced=True)["record"] == "cleared"


def test_row_8_some_or_none_passed_keeps_the_record():
    st = state()
    fail(st, u(1), u(10), CANDIDATE)
    fail(st, u(1), u(50), CANDIDATE)
    before = (st.consecutive_failures, st.next_retry_at)
    assert ok(st, committed=u(20), advanced=True)["record"] == "kept"
    assert (st.consecutive_failures, st.next_retry_at) == before


@pytest.mark.parametrize("kind", [CANDIDATE, PAGE_AFTER])
def test_rows_9_and_10_a_wrap_around_passes_every_position(kind):
    st = state()
    fail(st, u(1), u(10**6), kind)
    assert ok(st, committed=None, advanced=False, wrapped=True)["record"] == "cleared"


def test_row_11_no_advance_and_no_wrap_changes_nothing():
    st = state()
    fail(st, u(1), u(10), CANDIDATE)
    before = (st.consecutive_failures, st.last_failure_code)
    assert ok(st)["record"] == "kept"
    assert (st.consecutive_failures, st.last_failure_code) == before


def test_a_stalled_record_is_cleared_by_a_passing_success():
    st = state()
    for _ in range(T):
        fail(st, u(1), u(10), CANDIDATE)
    assert st.stalled
    ok(st, committed=u(10), advanced=True)
    assert not st.stalled and st.stalled_since is None


def test_a_success_with_no_record_is_a_no_op():
    st = state()
    assert ok(st, committed=u(1), advanced=True) == {"record": "kept", "config": "none"}


# --- rows 22 to 26, 29, 30: configuration invalid ----------------------------------------------------------------------------
def test_row_23_detection_without_a_record_counts_once_and_stores_the_context():
    st = state()
    st.record_config_invalid(pass_start=u(3), boundary_cursor=u(3), now=NOW, fallback_seconds=86400.0, reasons=("X",))
    assert st.consecutive_failures == 1 and st.last_failure_code == SWEEP_CONFIG_INVALID
    assert st.config_invalid_active and st.config_invalid_trigger_position == u(3)
    assert st.config_invalid_trigger_kind == PAGE_AFTER and st.config_invalid_trigger_failure_code is None
    assert st.next_retry_at == NOW + timedelta(seconds=86400) == st.config_invalid_next_retry_at
    assert st.config_currently_valid is False


def test_row_22_detection_with_a_record_at_the_same_start_keeps_its_position_and_takes_the_trigger_code():
    st = state()
    fail(st, u(1), u(10), CANDIDATE, code="SWEEP_PAGE_TIMEOUT")
    st.record_config_invalid(pass_start=u(1), boundary_cursor=u(1), now=NOW, fallback_seconds=86400.0)
    assert st.consecutive_failures == 2
    assert (st.current_failure_cursor, st.current_failure_kind) == (u(10), CANDIDATE)
    assert st.config_invalid_trigger_failure_code == "SWEEP_PAGE_TIMEOUT"


def test_the_trigger_code_is_never_the_config_code_and_a_repeated_detection_keeps_it():
    st = state()
    fail(st, u(1), u(10), CANDIDATE, code="SWEEP_PAGE_TIMEOUT")
    st.record_config_invalid(pass_start=u(1), boundary_cursor=u(1), now=NOW, fallback_seconds=86400.0)
    st.record_config_invalid(pass_start=u(1), boundary_cursor=u(1), now=NOW, fallback_seconds=86400.0)   # row 30
    assert st.config_invalid_trigger_failure_code == "SWEEP_PAGE_TIMEOUT"
    assert st.consecutive_failures == 3


def test_row_24_repeated_detections_count_once_per_pass_and_reach_the_threshold():
    st = state()
    for _ in range(T):
        st.record_config_invalid(pass_start=None, boundary_cursor=None, now=NOW, fallback_seconds=86400.0)
    assert st.stalled and st.config_invalid_active


def test_row_4_a_stalled_record_stays_pinned_under_a_config_detection_at_another_start():
    st = state()
    for _ in range(T):
        fail(st, u(1), u(10), CANDIDATE)
    st.record_config_invalid(pass_start=u(9), boundary_cursor=u(9), now=NOW, fallback_seconds=86400.0)
    assert st.stalled and st.first_failure_cursor == u(10) and st.pass_start_cursor == u(9)


@pytest.mark.parametrize("bad", [86400.5, 0, 1e300, float("nan"), float("inf"), True, None, "86400", 10**400],
                         ids=["86400.5", "zero", "1e300", "nan", "inf", "bool", "none", "str", "10e400"])
def test_row_28_a_corrupted_fallback_writes_nothing(bad):
    st = state()
    fail(st, u(1), u(10), CANDIDATE)
    snapshot = (st.consecutive_failures, st.last_failure_code, st.config_invalid_active, st.first_failure_cursor)
    with pytest.raises(SweeperFallbackInvalid):
        st.record_config_invalid(pass_start=u(1), boundary_cursor=u(1), now=NOW, fallback_seconds=bad)
    assert (st.consecutive_failures, st.last_failure_code, st.config_invalid_active,
            st.first_failure_cursor) == snapshot


def test_a_valid_fallback_that_overflows_the_datetime_stores_datetime_max():
    near_max = datetime.max.replace(tzinfo=UTC) - timedelta(seconds=10)
    assert retry_at(near_max, 86400.0) == datetime.max.replace(tzinfo=UTC)
    st = state()
    st.record_config_invalid(pass_start=None, boundary_cursor=None, now=near_max, fallback_seconds=86400.0)
    assert st.config_invalid_next_retry_at == datetime.max.replace(tzinfo=UTC) and st.consecutive_failures == 1


def test_row_29_another_failure_code_changes_only_the_ordinary_fields():
    st = state()
    st.record_config_invalid(pass_start=u(2), boundary_cursor=u(2), now=NOW, fallback_seconds=86400.0)
    context = (st.config_invalid_trigger_position, st.config_invalid_trigger_kind, st.config_invalid_detected_at)
    fail(st, u(2), u(1), CANDIDATE, code="SWEEP_EXCEPTION")                # an EARLIER position
    assert st.last_failure_code == "SWEEP_EXCEPTION" and st.config_invalid_active
    assert (st.config_invalid_trigger_position, st.config_invalid_trigger_kind,
            st.config_invalid_detected_at) == context


def test_rows_25_and_26_clearing_needs_valid_config_the_trigger_position_and_progress():
    st = state()
    st.record_config_invalid(pass_start=u(5), boundary_cursor=u(5), now=NOW, fallback_seconds=86400.0)
    assert ok(st, committed=u(9), advanced=True, valid=False)["config"] == "kept"      # not valid now
    assert ok(st, committed=u(5), advanced=True)["config"] == "kept"                   # PAGE_AFTER: equality is not enough
    assert ok(st)["config"] == "kept"                                                  # no progress
    assert ok(st, committed=u(6), advanced=True)["config"] == "cleared"
    assert not st.config_invalid_active and st.config_invalid_trigger_failure_code is None


def test_ci_4_the_record_and_the_config_state_clear_independently():
    st = state()
    st.record_config_invalid(pass_start=u(5), boundary_cursor=u(5), now=NOW, fallback_seconds=86400.0)
    out = ok(st, committed=None, advanced=False, wrapped=True)
    assert out == {"record": "cleared", "config": "cleared"}                           # a wrap passes every position
    st2 = state()
    st2.record_config_invalid(pass_start=u(5), boundary_cursor=u(5), now=NOW, fallback_seconds=86400.0)
    fail(st2, u(5), u(1), CANDIDATE, code="SWEEP_EXCEPTION")
    st2.has_record = False                                                             # the ordinary record goes away
    assert st2.config_invalid_active                                                   # rows 3 and 5 to 10 never touch it


def test_row_21_reset_empties_everything_and_row_31_keeps_a_config_only_state():
    st = state()
    st.record_config_invalid(pass_start=u(5), boundary_cursor=u(5), now=NOW, fallback_seconds=86400.0)
    assert st.exists
    st.reset()
    assert not st.exists and st.config_currently_valid is None and st.consecutive_failures == 0
    only = state()
    only.config_invalid_active = True                                                  # CI-only: no stall record
    assert only.exists and not only.has_record


def test_snapshots_expose_only_the_safe_subset():
    st = state()
    st.record_config_invalid(pass_start=u(2), boundary_cursor=u(2), now=NOW, fallback_seconds=86400.0, reasons=("RELATION",))
    snap, cfg = st.snapshot(), st.config_snapshot()
    assert not hasattr(snap, "first_failure_kind") and not hasattr(snap, "config_invalid_active")
    assert not hasattr(cfg, "config_invalid_trigger_failure_code") and not hasattr(cfg, "config_invalid_trigger_kind")
    assert cfg.config_invalid_active and cfg.config_invalid_reasons == ("RELATION",) and cfg.clearance_pending is False
    with pytest.raises(Exception):
        snap.stalled = True                                                            # frozen


# --- the backoff -------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("failures", [1, 2, 5, 16, 17, 1024, 1025, 10**400, 10**10000, 0, -5],
                         ids=lambda v: "huge" if isinstance(v, int) and v > 10**9 else str(v))
def test_backoff_is_finite_and_inside_its_limits(failures):
    d = backoff_delay(failures, P.ORPHAN_SWEEP_BACKOFF_MIN_SECONDS, P.ORPHAN_SWEEP_BACKOFF_MAX_SECONDS,
                      P.ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP)
    assert math.isfinite(d) and P.ORPHAN_SWEEP_BACKOFF_MIN_SECONDS <= d <= P.ORPHAN_SWEEP_BACKOFF_MAX_SECONDS


def test_backoff_sequence_doubles_and_saturates():
    seq = [backoff_delay(k, 30.0, 300.0, 16) for k in range(1, 7)]
    assert seq == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]


def test_backoff_exponent_cap_zero_is_a_constant_delay_and_a_naive_power_would_overflow():
    assert {backoff_delay(k, 30.0, 300.0, 0) for k in (1, 2, 50)} == {30.0}
    assert backoff_delay(10**6, 1e-300, 1e300, 62) <= 1e300                            # float overflow never happens


def test_peek_count_matches_what_the_record_will_hold():
    st = state()
    assert st.peek_count(None) == 1
    fail(st, u(1), u(2), CANDIDATE)
    assert st.peek_count(u(1)) == 2 and st.peek_count(u(9)) == 1
    for _ in range(T):
        fail(st, u(1), u(2), CANDIDATE)
    assert st.stalled and st.peek_count(u(9)) == st.consecutive_failures + 1


def test_the_module_imports_no_runner_and_no_schema():
    import app.services.sweeper_stall as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "app.schemas" not in src and "services.runner" not in src
