"""B2 phase 1: the constant inventory of design 9.4 (defaults), and the three central validators of 9.2 / 9.3
(`validate_process_timing_limits`, `validate_process_risk_limits`, `validate_process_input_constants`): they accept the defaults
and reject every violated relation, boundary and shape. Pure unit tests: no database, no process."""
import math
import re

import pytest

from app.validation import policies as P
from app.validation import validators as V

# --- the inventory: every new constant of design 9.4 with its default ---------------------------------------------------
DEFAULTS = {
    "ORPHAN_SWEEP_BATCH_ROWS": 50, "ORPHAN_SWEEP_PASS_SECONDS": 60, "ORPHAN_SWEEP_INTERVAL_SECONDS": 30.0,
    "ORPHAN_SWEEP_STARTUP_WAIT_SECONDS": 10, "ORPHAN_SWEEP_STALL_THRESHOLD": 5,
    "ORPHAN_SWEEP_BACKOFF_MIN_SECONDS": 30.0, "ORPHAN_SWEEP_BACKOFF_MAX_SECONDS": 300.0,
    "ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP": 16, "ORPHAN_SWEEP_LOG_THROTTLE_SECONDS": 300.0,
    "ORPHAN_SWEEP_MAX_INTERVAL_SECONDS": 86400.0, "ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS": 86400.0,
    "BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 15.0, "BACKGROUND_READY_TIMEOUT_SECONDS": 5.0,
    "BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS": 5.0, "BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 5.0,
    "BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 60.0, "BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP": 10,
    "BACKGROUND_MAX_RESTARTS": 5, "BACKGROUND_STABLE_SECONDS": 300.0, "BACKGROUND_RESTART_WINDOW_SECONDS": 3600.0,
    "BACKGROUND_RESTART_WINDOW_MAX": 10, "BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 5.0,
    "BACKGROUND_MIN_INTERVAL_SECONDS": 0.1, "BACKGROUND_DB_CONCURRENCY": 1,
    "BACKGROUND_COMPONENT_IDS": ("sweeper", "risk-worker", "capability"),
    "CAPABILITY_REFRESH_SECONDS": 10, "CAPABILITY_WRITE_TIMEOUT_SECONDS": 5, "CAPABILITY_TTL_SECONDS": 30,
    "PUMP_DRAIN_SECONDS": 5, "TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS": 10, "TREE_SETTLE_POLL_SECONDS": 1.0,
    "JOB_CLOSE_SETTLE_SECONDS": 2.0, "JOB_TERMINATE_EXIT_CODE": 1263094860,
    "PROCESS_RISK_IDLE_INTERVAL_SECONDS": 15, "PROCESS_RISK_ROW_OVERHEAD_SECONDS": 3,
    "PROCESS_RISK_ROW_TIMEOUT_SECONDS": 10, "PROCESS_RISK_DB_OVERHEAD_SECONDS": 20,
    "PROCESS_RISK_REQUEST_TIMEOUT_SECONDS": 120, "PROCESS_RISK_PASS_OVERHEAD_SECONDS": 20,
    "PROCESS_RISK_PASS_SECONDS": 150, "PROCESS_RISK_CLAIM_MARGIN_SECONDS": 120,
    "PROCESS_RISK_CLAIM_TIMEOUT_SECONDS": 300, "PROCESS_RISK_BACKOFF_BASE_SECONDS": 5,
    "PROCESS_RISK_BACKOFF_CAP_SECONDS": 60, "PROCESS_RISK_MAX_ATTEMPTS": 5, "PROCESS_RISK_BATCH_REQUESTS": 4,
    "PROCESS_RISK_BATCH_ROWS": 32, "PROCESS_RISK_MAX_TARGETS": 8, "PROCESS_RISK_TARGETS_DB_LIMIT": 500,
    "PROCESS_RISK_NORMAL_UNRESOLVED_PER_TASK": 1, "PROCESS_RISK_TARGET_LOCK_SECONDS": 3,
    "PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS": 2, "PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS": 1,
    "PROCESS_RECHECK_COOLDOWN_BASE_SECONDS": 60, "PROCESS_RECHECK_COOLDOWN_CAP_SECONDS": 900,
    "PROCESS_RECHECK_WINDOW_SECONDS": 3600, "PROCESS_RECHECK_MAX_PER_WINDOW": 4,
    "TRACKED_EXECUTION_MAX_AGE_SECONDS": 3660, "PROCESS_RISK_REQUEST_STALE_SECONDS": 165,
    "CANDIDATE_PAGE_ROWS": 50, "CANDIDATE_MAX_SCANNED_ROWS": 1000, "CANDIDATE_SCAN_MAX_SECONDS": 5,
    "CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS": 2, "DIAGNOSTIC_MAX_ANOMALIES": 50,
    "REQUESTED_BY_MAX_LENGTH": 128, "REQUESTED_BY_PATTERN": r"[A-Za-z0-9._@:+-]+",
    "PROCESS_RISK_RATIONALE_MAX_LENGTH": 2000, "DETAIL_SCHEMA_VERSION": 1,
    "DISPOSITION_KIND_RISK_ACCEPTED": "OPERATOR_RISK_ACCEPTED",
}
# Existing constants that the B2 relations reuse unchanged (design 9.2 and 9.4).
EXISTING = {"PROCESS_TERMINATE_GRACE_SECONDS": 5, "MAX_PG_TIMEOUT_MS": 2_147_483_647, "MAX_CLI_TIMEOUT_SECONDS": 3600,
            "PROOF_MAX_ATTEMPTS": 3, "IDEMPOTENCY_KEY_MAX_LENGTH": 128, "SANITIZED_JSON_MAX_BYTES": 64 * 1024,
            "SANITIZE_MAX_STRING_CHARS": 4000}

TIME_NAMES = [
    "ORPHAN_SWEEP_PASS_SECONDS", "ORPHAN_SWEEP_INTERVAL_SECONDS", "ORPHAN_SWEEP_STARTUP_WAIT_SECONDS",
    "ORPHAN_SWEEP_BACKOFF_MIN_SECONDS", "ORPHAN_SWEEP_BACKOFF_MAX_SECONDS", "ORPHAN_SWEEP_LOG_THROTTLE_SECONDS",
    "ORPHAN_SWEEP_MAX_INTERVAL_SECONDS", "ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS",
    "BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS", "BACKGROUND_READY_TIMEOUT_SECONDS",
    "BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS", "BACKGROUND_RESTART_BACKOFF_BASE_SECONDS",
    "BACKGROUND_RESTART_BACKOFF_CAP_SECONDS", "BACKGROUND_STABLE_SECONDS", "BACKGROUND_RESTART_WINDOW_SECONDS",
    "BACKGROUND_WAKEUP_DEBOUNCE_SECONDS", "BACKGROUND_MIN_INTERVAL_SECONDS", "CAPABILITY_REFRESH_SECONDS",
    "CAPABILITY_WRITE_TIMEOUT_SECONDS", "CAPABILITY_TTL_SECONDS", "PUMP_DRAIN_SECONDS",
    "TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS", "TREE_SETTLE_POLL_SECONDS", "JOB_CLOSE_SETTLE_SECONDS",
    "TRACKED_EXECUTION_MAX_AGE_SECONDS", "PROCESS_RISK_REQUEST_STALE_SECONDS", "CANDIDATE_SCAN_MAX_SECONDS",
    "CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS", "PROCESS_RISK_TARGET_LOCK_SECONDS",
    "PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS", "PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS",
]
TIMING_COUNT_NAMES = [
    "ORPHAN_SWEEP_BATCH_ROWS", "CANDIDATE_PAGE_ROWS", "CANDIDATE_MAX_SCANNED_ROWS",
    "BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP", "BACKGROUND_MAX_RESTARTS", "BACKGROUND_RESTART_WINDOW_MAX",
    "BACKGROUND_DB_CONCURRENCY",
]
RISK_TIME_NAMES = [
    "PROCESS_RISK_IDLE_INTERVAL_SECONDS", "PROCESS_RISK_ROW_OVERHEAD_SECONDS", "PROCESS_RISK_ROW_TIMEOUT_SECONDS",
    "PROCESS_RISK_DB_OVERHEAD_SECONDS", "PROCESS_RISK_REQUEST_TIMEOUT_SECONDS", "PROCESS_RISK_PASS_OVERHEAD_SECONDS",
    "PROCESS_RISK_PASS_SECONDS", "PROCESS_RISK_CLAIM_MARGIN_SECONDS", "PROCESS_RISK_CLAIM_TIMEOUT_SECONDS",
    "PROCESS_RISK_BACKOFF_BASE_SECONDS", "PROCESS_RISK_BACKOFF_CAP_SECONDS", "PROCESS_RECHECK_COOLDOWN_BASE_SECONDS",
    "PROCESS_RECHECK_COOLDOWN_CAP_SECONDS", "PROCESS_RECHECK_WINDOW_SECONDS",
]
RISK_COUNT_NAMES = [
    "PROCESS_RISK_MAX_ATTEMPTS", "PROCESS_RISK_BATCH_REQUESTS", "PROCESS_RISK_BATCH_ROWS", "PROCESS_RISK_MAX_TARGETS",
    "PROCESS_RISK_NORMAL_UNRESOLVED_PER_TASK", "PROCESS_RECHECK_MAX_PER_WINDOW",
]
INPUT_COUNT_NAMES = ["JOB_TERMINATE_EXIT_CODE", "DIAGNOSTIC_MAX_ANOMALIES", "ORPHAN_SWEEP_STALL_THRESHOLD",
                     "ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP", "REQUESTED_BY_MAX_LENGTH", "PROCESS_RISK_RATIONALE_MAX_LENGTH",
                     "DETAIL_SCHEMA_VERSION"]
SHAPE_SET = [pytest.param(True, id="bool"), pytest.param(None, id="None"), pytest.param("5", id="str"),
             pytest.param(math.nan, id="nan"), pytest.param(math.inf, id="+inf"), pytest.param(-math.inf, id="-inf")]


def run(validator, monkeypatch, **overrides):
    for name, value in overrides.items():
        assert hasattr(P, name), name
        monkeypatch.setattr(P, name, value)
    validator()


def accepted(validator, monkeypatch, **overrides) -> None:
    run(validator, monkeypatch, **overrides)


def refused(validator, monkeypatch, **overrides) -> None:
    """Refused, and the message names one of the changed constants (the violation is the one under test)."""
    with pytest.raises(ValueError) as info:
        run(validator, monkeypatch, **overrides)
    assert any(name in str(info.value) for name in overrides), (overrides, str(info.value))


# --- the inventory ---------------------------------------------------------------------------------------------------------
def test_every_inventory_constant_has_its_design_default():
    assert len(DEFAULTS) == 69
    for name, value in DEFAULTS.items():
        assert getattr(P, name) == value, name
        assert type(getattr(P, name)) is type(value), f"{name}: int/float kind of the default"


def test_existing_constants_reused_by_the_relations_are_unchanged():
    for name, value in EXISTING.items():
        assert getattr(P, name) == value, name


def test_job_terminate_exit_code_is_ascii_kill():
    assert P.JOB_TERMINATE_EXIT_CODE == 0x4B494C4C and bytes.fromhex("4B494C4C") == b"KILL"


def test_schema_constants_have_the_design_shape():
    assert [f.key for f in P.RISK_ACCEPTANCE_SCHEMA] == ["approval_id", "execution_id", "pid", "create_time", "last_verdict",
                                                          "requested_at", "rationale", "reverify_result", "applied_at",
                                                          "requested_by"]
    assert [f.key for f in P.RISK_ACCEPTANCE_SCHEMA if not f.required] == ["requested_by"]
    assert P.RISK_ACCEPTANCE_REQUIRED_KEYS == frozenset(f.key for f in P.RISK_ACCEPTANCE_SCHEMA if f.required)
    assert len(P.RISK_ACCEPTANCE_REQUIRED_KEYS) == 9 and "requested_by" not in P.RISK_ACCEPTANCE_REQUIRED_KEYS
    assert P.TRACKED_ONLY_KEYS == frozenset({"v", "runner_process", "checkpoint_step_key", "proof", "disposition",
                                             "reason_code", "risk_acceptance", "anomaly_original", "anomaly_settlement"})
    assert P.RISK_ACCEPTANCE_REQUESTED_BY_RE == "^[A-Za-z0-9._@:+-]{1,128}$"


def test_regular_expressions_behave_as_the_schema_says():
    uuid_re, ts_re = re.compile(P.RISK_ACCEPTANCE_UUID_RE), re.compile(P.RISK_ACCEPTANCE_TS_RE)
    assert uuid_re.fullmatch("0b1c2d3e-0000-4000-8000-0123456789ab") and not uuid_re.fullmatch("0B1C2D3E-0000-4000-8000-0123456789AB")
    assert ts_re.fullmatch("2026-11-24T10:20:30Z") and ts_re.fullmatch("2026-11-24T10:20:30.123456Z")
    assert not ts_re.fullmatch("2026-11-24T10:20:30.1234567Z") and not ts_re.fullmatch("2026-11-24 10:20:30Z")
    by_re = re.compile(P.RISK_ACCEPTANCE_REQUESTED_BY_RE)
    assert by_re.fullmatch("a" * 128) and not by_re.fullmatch("a" * 129) and not by_re.fullmatch("") and not by_re.fullmatch("a b")
    forbidden = re.compile(P.RISK_ACCEPTANCE_RATIONALE_FORBIDDEN)
    for bad in ("\u0001", "\u0008", "\u000b", "\u001f", "\u007f", "\u009f", " ", " ", "‪", "‮", "⁦",
                "⁩"):
        assert forbidden.search("x" + bad + "y"), repr(bad)
    for good in ("\n", "\t", " ", "a", " ", "‧", " ", "⁥", "⁪", "\U0001F600"):
        assert not forbidden.search(good), repr(good)


# --- defaults and the linked set ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("validator", [V.validate_process_timing_limits, V.validate_process_risk_limits,
                                        V.validate_process_input_constants, V.validate_process_policy])
def test_validators_accept_the_defaults(validator):
    validator()


def test_the_linked_set_runs_all_three_validators_and_refuses_on_any_violation(monkeypatch):
    for name, bad in (("CAPABILITY_TTL_SECONDS", 1), ("PROCESS_RISK_ROW_TIMEOUT_SECONDS", 1),
                      ("ORPHAN_SWEEP_STALL_THRESHOLD", 0)):
        monkeypatch.setattr(P, name, bad)
        with pytest.raises(ValueError):
            V.validate_process_policy()
        monkeypatch.undo()


def test_runner_construction_is_refused_on_a_violated_relation_and_create_app_on_a_violated_timing(monkeypatch):
    from app.main import create_app
    from app.services.runner import Runner
    monkeypatch.setattr(P, "PROCESS_RISK_ROW_TIMEOUT_SECONDS", 9)               # (i): 5 + 2 + 3 = 10 > 9
    with pytest.raises(ValueError):
        Runner(None, None, None, None, [], advisory_key=1)
    monkeypatch.undo()
    monkeypatch.setattr(P, "CAPABILITY_TTL_SECONDS", 24)                        # below 2 x 10 + 5 = 25
    with pytest.raises(ValueError):
        create_app(run_preflight=False)


# --- timing relations (design 9.2) ----------------------------------------------------------------------------------------
TIMING_VALID = [
    {}, {"ORPHAN_SWEEP_PASS_SECONDS": 10}, {"ORPHAN_SWEEP_PASS_SECONDS": 600}, {"ORPHAN_SWEEP_PASS_SECONDS": 86400.0},
    {"ORPHAN_SWEEP_BATCH_ROWS": 1}, {"ORPHAN_SWEEP_BATCH_ROWS": 1000}, {"CANDIDATE_PAGE_ROWS": 1},
    {"CANDIDATE_PAGE_ROWS": 1000}, {"CANDIDATE_MAX_SCANNED_ROWS": 50},
    {"ORPHAN_SWEEP_STARTUP_WAIT_SECONDS": 60}, {"ORPHAN_SWEEP_STARTUP_WAIT_SECONDS": 0.5},
    {"BACKGROUND_MIN_INTERVAL_SECONDS": 1.0}, {"BACKGROUND_MIN_INTERVAL_SECONDS": 5.0},
    {"ORPHAN_SWEEP_INTERVAL_SECONDS": 0.1, "ORPHAN_SWEEP_BACKOFF_MIN_SECONDS": 0.1, "BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 0.1},
    {"ORPHAN_SWEEP_BACKOFF_MIN_SECONDS": 300.0}, {"ORPHAN_SWEEP_BACKOFF_MAX_SECONDS": 86400.0},
    {"ORPHAN_SWEEP_LOG_THROTTLE_SECONDS": 0.1}, {"ORPHAN_SWEEP_LOG_THROTTLE_SECONDS": 86400},
    {"ORPHAN_SWEEP_MAX_INTERVAL_SECONDS": 86400}, {"ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS": 86400},
    {"BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 7.5}, {"BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 300.0},
    {"BACKGROUND_READY_TIMEOUT_SECONDS": 0.5}, {"BACKGROUND_READY_TIMEOUT_SECONDS": 60.0},
    {"BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS": 0.5}, {"BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS": 60.0},
    {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 0.1}, {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 60.0},
    {"BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP": 0}, {"BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP": 62},
    {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 3600.0, "BACKGROUND_STABLE_SECONDS": 3600.0},
    {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 300.0},
    {"BACKGROUND_STABLE_SECONDS": 86400.0, "BACKGROUND_RESTART_WINDOW_SECONDS": 86400.0},
    {"BACKGROUND_STABLE_SECONDS": 3600.0}, {"BACKGROUND_RESTART_WINDOW_SECONDS": 300.0},
    {"BACKGROUND_RESTART_WINDOW_SECONDS": 86400.0},
    {"BACKGROUND_MAX_RESTARTS": 1}, {"BACKGROUND_MAX_RESTARTS": 10}, {"BACKGROUND_MAX_RESTARTS": 100,
                                                                       "BACKGROUND_RESTART_WINDOW_MAX": 100},
    {"BACKGROUND_RESTART_WINDOW_MAX": 5}, {"BACKGROUND_RESTART_WINDOW_MAX": 1000},
    {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 30.0},
    {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 60.0, "ORPHAN_SWEEP_INTERVAL_SECONDS": 60.0,
     "ORPHAN_SWEEP_BACKOFF_MIN_SECONDS": 60.0},
    {"BACKGROUND_DB_CONCURRENCY": 2}, {"BACKGROUND_DB_CONCURRENCY": 8},
    {"CAPABILITY_REFRESH_SECONDS": 0.1, "CAPABILITY_WRITE_TIMEOUT_SECONDS": 0.05, "CAPABILITY_TTL_SECONDS": 30},
    {"CAPABILITY_TTL_SECONDS": 25}, {"CAPABILITY_TTL_SECONDS": 10**400}, {"CAPABILITY_WRITE_TIMEOUT_SECONDS": 9.99},
    {"PUMP_DRAIN_SECONDS": 0.001}, {"TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS": 0.001},
    {"TREE_SETTLE_POLL_SECONDS": 5}, {"TREE_SETTLE_POLL_SECONDS": 0.001}, {"JOB_CLOSE_SETTLE_SECONDS": 0.001},
    {"TRACKED_EXECUTION_MAX_AGE_SECONDS": 3621}, {"TRACKED_EXECUTION_MAX_AGE_SECONDS": 10**400},
    {"PUMP_DRAIN_SECONDS": 20, "TRACKED_EXECUTION_MAX_AGE_SECONDS": 3636},
    {"CANDIDATE_SCAN_MAX_SECONDS": 2.0}, {"CANDIDATE_SCAN_MAX_SECONDS": 10},
    {"CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS": 0.001}, {"CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS": 5},
    {"PROCESS_RISK_TARGET_LOCK_SECONDS": 2}, {"PROCESS_RISK_TARGET_LOCK_SECONDS": 600},
    {"PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS": 0.001},
    {"PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS": 3},
    {"PROCESS_RISK_REQUEST_STALE_SECONDS": 160, "PROCESS_RISK_PASS_SECONDS": 145},
]
TIMING_INVALID = [
    # ORPHAN_SWEEP_BATCH_ROWS / candidate scan
    {"ORPHAN_SWEEP_BATCH_ROWS": 0}, {"ORPHAN_SWEEP_BATCH_ROWS": -1}, {"ORPHAN_SWEEP_BATCH_ROWS": 1.5},
    {"ORPHAN_SWEEP_BATCH_ROWS": 50.0}, {"ORPHAN_SWEEP_BATCH_ROWS": 1001}, {"ORPHAN_SWEEP_BATCH_ROWS": 10**400},
    {"CANDIDATE_PAGE_ROWS": 0}, {"CANDIDATE_PAGE_ROWS": 1001}, {"CANDIDATE_PAGE_ROWS": 10**400},
    {"CANDIDATE_MAX_SCANNED_ROWS": 0}, {"CANDIDATE_MAX_SCANNED_ROWS": 49}, {"CANDIDATE_MAX_SCANNED_ROWS": 7,
                                                                            "CANDIDATE_PAGE_ROWS": 5,
                                                                            "ORPHAN_SWEEP_BATCH_ROWS": 5},
    {"CANDIDATE_MAX_SCANNED_ROWS": 49, "CANDIDATE_PAGE_ROWS": 49},
    # ORPHAN_SWEEP_PASS_SECONDS
    {"ORPHAN_SWEEP_PASS_SECONDS": 0}, {"ORPHAN_SWEEP_PASS_SECONDS": -1.0}, {"ORPHAN_SWEEP_PASS_SECONDS": 5},
    {"ORPHAN_SWEEP_PASS_SECONDS": 9}, {"ORPHAN_SWEEP_PASS_SECONDS": 86400.5}, {"ORPHAN_SWEEP_PASS_SECONDS": 1e300},
    {"ORPHAN_SWEEP_PASS_SECONDS": 10**400},
    {"ORPHAN_SWEEP_PASS_SECONDS": 6.9, "ORPHAN_SWEEP_STARTUP_WAIT_SECONDS": 1, "CANDIDATE_SCAN_MAX_SECONDS": 2},
    {"ORPHAN_SWEEP_STARTUP_WAIT_SECONDS": 61}, {"ORPHAN_SWEEP_STARTUP_WAIT_SECONDS": 0}, {"ORPHAN_SWEEP_STARTUP_WAIT_SECONDS": -1.0},
    {"ORPHAN_SWEEP_STARTUP_WAIT_SECONDS": 1e300}, {"ORPHAN_SWEEP_STARTUP_WAIT_SECONDS": 10**400},
    # sweeper intervals
    {"ORPHAN_SWEEP_INTERVAL_SECONDS": 0}, {"ORPHAN_SWEEP_INTERVAL_SECONDS": 0.09}, {"ORPHAN_SWEEP_INTERVAL_SECONDS": 1e-9},
    {"ORPHAN_SWEEP_INTERVAL_SECONDS": 5e-324}, {"ORPHAN_SWEEP_INTERVAL_SECONDS": 86400.5},
    {"ORPHAN_SWEEP_INTERVAL_SECONDS": 1e300}, {"ORPHAN_SWEEP_INTERVAL_SECONDS": 10**400},
    {"ORPHAN_SWEEP_INTERVAL_SECONDS": -1.0},
    {"ORPHAN_SWEEP_BACKOFF_MIN_SECONDS": 29.9},                       # below the sweep interval
    {"ORPHAN_SWEEP_BACKOFF_MIN_SECONDS": 300.1},                      # above the maximum
    {"ORPHAN_SWEEP_BACKOFF_MAX_SECONDS": 29.9}, {"ORPHAN_SWEEP_BACKOFF_MAX_SECONDS": 86400.5},
    {"ORPHAN_SWEEP_BACKOFF_MAX_SECONDS": 1e300}, {"ORPHAN_SWEEP_BACKOFF_MAX_SECONDS": 5e-324},
    {"ORPHAN_SWEEP_LOG_THROTTLE_SECONDS": 0}, {"ORPHAN_SWEEP_LOG_THROTTLE_SECONDS": 0.09},
    {"ORPHAN_SWEEP_LOG_THROTTLE_SECONDS": 86400.5}, {"ORPHAN_SWEEP_LOG_THROTTLE_SECONDS": 10**400},
    {"ORPHAN_SWEEP_MAX_INTERVAL_SECONDS": 86400.5}, {"ORPHAN_SWEEP_MAX_INTERVAL_SECONDS": 86399.9},
    {"ORPHAN_SWEEP_MAX_INTERVAL_SECONDS": 0}, {"ORPHAN_SWEEP_MAX_INTERVAL_SECONDS": -1.0},
    {"ORPHAN_SWEEP_MAX_INTERVAL_SECONDS": 1e300}, {"ORPHAN_SWEEP_MAX_INTERVAL_SECONDS": 10**400},
    {"ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS": 86400.5}, {"ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS": 3600.0},
    {"ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS": 0}, {"ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS": 1e300},
    {"ORPHAN_SWEEP_CONFIG_FALLBACK_SECONDS": 10**400},
    # the floor
    {"BACKGROUND_MIN_INTERVAL_SECONDS": 0}, {"BACKGROUND_MIN_INTERVAL_SECONDS": 0.09},
    {"BACKGROUND_MIN_INTERVAL_SECONDS": -1.0}, {"BACKGROUND_MIN_INTERVAL_SECONDS": 5e-324},
    {"BACKGROUND_MIN_INTERVAL_SECONDS": 1e-9},
    # settle, READY, margin
    {"BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 0}, {"BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 0.99},
    {"BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 300.01}, {"BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 7.0},
    {"BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 1.0}, {"BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 1e300},
    {"BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS": 10**400},
    {"TREE_SETTLE_POLL_SECONDS": 6}, {"TREE_SETTLE_POLL_SECONDS": 0}, {"TREE_SETTLE_POLL_SECONDS": -1.0},
    {"BACKGROUND_READY_TIMEOUT_SECONDS": 0}, {"BACKGROUND_READY_TIMEOUT_SECONDS": 0.49},
    {"BACKGROUND_READY_TIMEOUT_SECONDS": 60.01}, {"BACKGROUND_READY_TIMEOUT_SECONDS": 1e300},
    {"BACKGROUND_READY_TIMEOUT_SECONDS": 10**400},
    {"BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS": 0}, {"BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS": 0.49},
    {"BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS": 60.01}, {"BACKGROUND_RECOVERY_SETTLE_MARGIN_SECONDS": 10**400},
    # restart delay, stability, window
    {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 0}, {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 0.0},
    {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": -1.0}, {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 0.09},
    {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 1e-9}, {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 5e-324},
    {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 1e300}, {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 10**400},
    {"BACKGROUND_RESTART_BACKOFF_BASE_SECONDS": 60.1},                # above the cap
    {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 4.9},                  # below the base
    {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 3600.0},               # above the stability time at the defaults
    {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 3600.5, "BACKGROUND_STABLE_SECONDS": 3601.0},
    {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 300.1}, {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 0},
    {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": -1.0}, {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 5e-324},
    {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 1e300}, {"BACKGROUND_RESTART_BACKOFF_CAP_SECONDS": 10**400},
    {"BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP": -1}, {"BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP": 63},
    {"BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP": 10**400}, {"BACKGROUND_RESTART_BACKOFF_EXPONENT_CAP": 10.0},
    {"BACKGROUND_MAX_RESTARTS": 0}, {"BACKGROUND_MAX_RESTARTS": -1}, {"BACKGROUND_MAX_RESTARTS": 101},
    {"BACKGROUND_MAX_RESTARTS": 100},                                   # above the window maximum 10
    {"BACKGROUND_MAX_RESTARTS": 11}, {"BACKGROUND_MAX_RESTARTS": 10**400}, {"BACKGROUND_MAX_RESTARTS": 5.0},
    {"BACKGROUND_RESTART_WINDOW_MAX": 0}, {"BACKGROUND_RESTART_WINDOW_MAX": -1}, {"BACKGROUND_RESTART_WINDOW_MAX": 1001},
    {"BACKGROUND_RESTART_WINDOW_MAX": 4},                              # below the consecutive budget 5
    {"BACKGROUND_RESTART_WINDOW_MAX": 10**400},
    {"BACKGROUND_STABLE_SECONDS": 0}, {"BACKGROUND_STABLE_SECONDS": -1.0}, {"BACKGROUND_STABLE_SECONDS": 59.9},
    {"BACKGROUND_STABLE_SECONDS": 3600.1},                             # above the window
    {"BACKGROUND_STABLE_SECONDS": 86400.5, "BACKGROUND_RESTART_WINDOW_SECONDS": 86400.5},
    {"BACKGROUND_STABLE_SECONDS": 1e300}, {"BACKGROUND_STABLE_SECONDS": 10**400},
    {"BACKGROUND_RESTART_WINDOW_SECONDS": 0}, {"BACKGROUND_RESTART_WINDOW_SECONDS": -1.0},
    {"BACKGROUND_RESTART_WINDOW_SECONDS": 299.9}, {"BACKGROUND_RESTART_WINDOW_SECONDS": 86400.5},
    {"BACKGROUND_RESTART_WINDOW_SECONDS": 1e300}, {"BACKGROUND_RESTART_WINDOW_SECONDS": 10**400},
    {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 0}, {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": -1.0},
    {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 0.09}, {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 1e-9},
    {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 5e-324}, {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 60.5},
    {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 30.5},                      # above the sweep interval
    {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 1e300}, {"BACKGROUND_WAKEUP_DEBOUNCE_SECONDS": 10**400},
    {"BACKGROUND_DB_CONCURRENCY": 0}, {"BACKGROUND_DB_CONCURRENCY": -1}, {"BACKGROUND_DB_CONCURRENCY": 9},
    {"BACKGROUND_DB_CONCURRENCY": 1.0}, {"BACKGROUND_DB_CONCURRENCY": 10**400},
    # capability task
    {"CAPABILITY_REFRESH_SECONDS": 0.09}, {"CAPABILITY_REFRESH_SECONDS": 0}, {"CAPABILITY_REFRESH_SECONDS": -1},
    {"CAPABILITY_REFRESH_SECONDS": 12.6},                              # TTL 30 < 2 x 12.6 + 5
    {"CAPABILITY_WRITE_TIMEOUT_SECONDS": 10}, {"CAPABILITY_WRITE_TIMEOUT_SECONDS": 0},
    {"CAPABILITY_WRITE_TIMEOUT_SECONDS": -1}, {"CAPABILITY_WRITE_TIMEOUT_SECONDS": 10**400},
    {"CAPABILITY_TTL_SECONDS": 24.9}, {"CAPABILITY_TTL_SECONDS": 0}, {"CAPABILITY_TTL_SECONDS": -1},
    {"CAPABILITY_REFRESH_SECONDS": 10**400},
    # tracked run and the diagnostics thresholds
    {"PUMP_DRAIN_SECONDS": 0}, {"PUMP_DRAIN_SECONDS": -1},
    {"TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS": 0}, {"TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS": -1},
    {"JOB_CLOSE_SETTLE_SECONDS": 0}, {"JOB_CLOSE_SETTLE_SECONDS": -1.0}, {"JOB_CLOSE_SETTLE_SECONDS": 10**400},
    {"TRACKED_EXECUTION_MAX_AGE_SECONDS": 3620.9}, {"TRACKED_EXECUTION_MAX_AGE_SECONDS": 0},
    {"TRACKED_EXECUTION_MAX_AGE_SECONDS": -1},
    {"PUMP_DRAIN_SECONDS": 45},                                         # live sum 3661 > 3660
    {"PROCESS_RISK_REQUEST_STALE_SECONDS": 164}, {"PROCESS_RISK_REQUEST_STALE_SECONDS": 166},
    {"PROCESS_RISK_REQUEST_STALE_SECONDS": 0},
    # candidate scan and the risk-target lock
    {"CANDIDATE_SCAN_MAX_SECONDS": 0}, {"CANDIDATE_SCAN_MAX_SECONDS": -1.0}, {"CANDIDATE_SCAN_MAX_SECONDS": 1.99},
    {"CANDIDATE_SCAN_MAX_SECONDS": 10.01}, {"CANDIDATE_SCAN_MAX_SECONDS": 1e300}, {"CANDIDATE_SCAN_MAX_SECONDS": 10**400},
    {"CANDIDATE_SCAN_MAX_SECONDS": 60, "PROCESS_RISK_ROW_TIMEOUT_SECONDS": 600},   # not strictly below the pass
    {"CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS": 0}, {"CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS": 0.0004},
    {"CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS": -1.0}, {"CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS": 5.01},
    {"CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS": 1e300}, {"CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS": 10**400},
    {"PROCESS_RISK_TARGET_LOCK_SECONDS": 0}, {"PROCESS_RISK_TARGET_LOCK_SECONDS": 1.9},
    {"PROCESS_RISK_TARGET_LOCK_SECONDS": 0.0004}, {"PROCESS_RISK_TARGET_LOCK_SECONDS": 3e9},
    {"PROCESS_RISK_TARGET_LOCK_SECONDS": 10**400},
    {"PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS": 3.1}, {"PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS": 1},
    {"PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS": 0},
    {"PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS": 2}, {"PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS": 0},
    {"PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS": 0.0004}, {"PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS": 10**400},
]


def case_id(overrides):
    return "-".join(f"{k}={v!r}"[:60] for k, v in overrides.items()) or "defaults"


@pytest.mark.parametrize("overrides", TIMING_VALID, ids=case_id)
def test_timing_limits_accept_valid_values_and_boundaries(monkeypatch, overrides):
    accepted(V.validate_process_timing_limits, monkeypatch, **overrides)


@pytest.mark.parametrize("overrides", TIMING_INVALID, ids=case_id)
def test_timing_limits_refuse_violations_boundaries_and_overflow(monkeypatch, overrides):
    refused(V.validate_process_timing_limits, monkeypatch, **overrides)


def test_pump_drain_of_six_is_still_inside_the_tracked_age(monkeypatch):
    accepted(V.validate_process_timing_limits, monkeypatch, PUMP_DRAIN_SECONDS=6)


@pytest.mark.parametrize("name", TIME_NAMES)
@pytest.mark.parametrize("bad", SHAPE_SET)
def test_every_timing_constant_refuses_the_shape_set(monkeypatch, name, bad):
    refused(V.validate_process_timing_limits, monkeypatch, **{name: bad})


@pytest.mark.parametrize("name", TIMING_COUNT_NAMES)
@pytest.mark.parametrize("bad", [*[p.values[0] for p in SHAPE_SET], 1.5, 5.0])
def test_every_timing_count_refuses_the_shape_set_and_a_float(monkeypatch, name, bad):
    refused(V.validate_process_timing_limits, monkeypatch, **{name: bad})


def test_the_settle_relation_is_exact_at_the_boundary(monkeypatch):
    refused(V.validate_process_timing_limits, monkeypatch, BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS=7.0)
    accepted(V.validate_process_timing_limits, monkeypatch, BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS=7.0001)
    refused(V.validate_process_timing_limits, monkeypatch, BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS=15.0,
            PROCESS_TERMINATE_GRACE_SECONDS=14)               # 14 + 1 + 1 = 16 >= 15


def test_the_tracked_age_is_checked_against_the_live_sum_not_the_literal(monkeypatch):
    monkeypatch.setattr(P, "MAX_CLI_TIMEOUT_SECONDS", 3700)            # live sum 3721 > 3660 (the literal 3621 is not used)
    with pytest.raises(ValueError):
        V.validate_process_timing_limits()
    monkeypatch.setattr(P, "TRACKED_EXECUTION_MAX_AGE_SECONDS", 3721)
    V.validate_process_timing_limits()


def test_tracked_age_default_is_the_cli_timeout_plus_a_fixed_margin_of_60_seconds():
    assert P.TRACKED_EXECUTION_MAX_AGE_SECONDS == P.MAX_CLI_TIMEOUT_SECONDS + 60
    live = (P.MAX_CLI_TIMEOUT_SECONDS + P.TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS + P.PROCESS_TERMINATE_GRACE_SECONDS
            + P.PUMP_DRAIN_SECONDS + P.TREE_SETTLE_POLL_SECONDS)
    assert live == 3621 and P.TRACKED_EXECUTION_MAX_AGE_SECONDS - live == 39


def test_the_default_relations_hold_with_the_documented_numbers():
    assert P.ORPHAN_SWEEP_PASS_SECONDS >= P.JOB_CLOSE_SETTLE_SECONDS + P.PROCESS_TERMINATE_GRACE_SECONDS == 7.0
    assert P.CANDIDATE_SCAN_MAX_SECONDS < P.ORPHAN_SWEEP_PASS_SECONDS
    assert P.CAPABILITY_TTL_SECONDS >= 2 * P.CAPABILITY_REFRESH_SECONDS + P.CAPABILITY_WRITE_TIMEOUT_SECONDS == 25
    assert P.PROCESS_RISK_REQUEST_STALE_SECONDS == P.PROCESS_RISK_IDLE_INTERVAL_SECONDS + P.PROCESS_RISK_PASS_SECONDS
    assert P.BACKGROUND_COMPONENT_SETTLE_TIMEOUT_SECONDS > P.PROCESS_TERMINATE_GRACE_SECONDS + P.TREE_SETTLE_POLL_SECONDS + 1.0
    assert P.PROCESS_RISK_TARGET_LOCK_WAIT_TIMEOUT_SECONDS < P.PROCESS_RISK_TARGET_LOCK_STATEMENT_TIMEOUT_SECONDS \
        <= P.PROCESS_RISK_TARGET_LOCK_SECONDS


# --- risk limits (design 7.5 items 5 and 8) --------------------------------------------------------------------------------
RISK_VALID = [
    {}, {"PROCESS_RISK_ROW_TIMEOUT_SECONDS": 10}, {"PROCESS_RISK_ROW_TIMEOUT_SECONDS": 11},
    {"JOB_CLOSE_SETTLE_SECONDS": 2.0}, {"PROCESS_RISK_ROW_OVERHEAD_SECONDS": 3}, {"PROCESS_RISK_MAX_TARGETS": 10},
    {"PROCESS_RISK_MAX_TARGETS": 1}, {"PROCESS_RISK_REQUEST_TIMEOUT_SECONDS": 100},
    {"PROCESS_RISK_REQUEST_TIMEOUT_SECONDS": 100, "PROCESS_RISK_PASS_SECONDS": 120},
    {"PROCESS_RISK_PASS_SECONDS": 140}, {"PROCESS_RISK_CLAIM_TIMEOUT_SECONDS": 240},
    {"PROCESS_RISK_BACKOFF_CAP_SECONDS": 299}, {"PROCESS_RISK_BACKOFF_BASE_SECONDS": 60},
    {"PROCESS_RISK_BACKOFF_BASE_SECONDS": 0.1}, {"PROCESS_RISK_IDLE_INTERVAL_SECONDS": 0.1},
    {"PROCESS_RISK_IDLE_INTERVAL_SECONDS": 10**6},
    {"PROCESS_RISK_MAX_TARGETS": 500, "PROCESS_RISK_REQUEST_TIMEOUT_SECONDS": 5020, "PROCESS_RISK_PASS_SECONDS": 5040,
     "PROCESS_RISK_CLAIM_TIMEOUT_SECONDS": 5140},
    {"PROCESS_RISK_MAX_ATTEMPTS": 1}, {"PROCESS_RISK_BATCH_REQUESTS": 1}, {"PROCESS_RISK_BATCH_ROWS": 1},
    {"PROCESS_RECHECK_MAX_PER_WINDOW": 1}, {"PROCESS_RECHECK_COOLDOWN_BASE_SECONDS": 900},
    {"PROCESS_RECHECK_COOLDOWN_CAP_SECONDS": 3600}, {"PROCESS_RECHECK_COOLDOWN_BASE_SECONDS": 3600,
                                                     "PROCESS_RECHECK_COOLDOWN_CAP_SECONDS": 3600},
    {"PROCESS_RECHECK_WINDOW_SECONDS": 900}, {"PROCESS_RECHECK_WINDOW_SECONDS": 10**400},
    {"PROCESS_RISK_PASS_SECONDS": 10**400},
]
RISK_INVALID = [
    {"PROCESS_RISK_ROW_TIMEOUT_SECONDS": 9.99},                        # rule (i): 5 + 2 + 3 = 10
    {"JOB_CLOSE_SETTLE_SECONDS": 2.01}, {"PROCESS_RISK_ROW_OVERHEAD_SECONDS": 3.01},
    {"PROCESS_TERMINATE_GRACE_SECONDS": 5.01},
    {"PROCESS_RISK_REQUEST_TIMEOUT_SECONDS": 99.9, "PROCESS_RISK_MAX_TARGETS": 8,
     "PROCESS_RISK_ROW_TIMEOUT_SECONDS": 10},                          # rule (ii): 8 x 10 + 20 = 100
    {"PROCESS_RISK_MAX_TARGETS": 11},                                  # 11 x 10 + 20 = 130 > 120
    {"PROCESS_RISK_DB_OVERHEAD_SECONDS": 40.1},
    {"PROCESS_RISK_PASS_SECONDS": 139.9, "PROCESS_RISK_REQUEST_TIMEOUT_SECONDS": 120},   # rule (iii): 120 + 20 = 140
    {"PROCESS_RISK_PASS_OVERHEAD_SECONDS": 30.1},
    {"PROCESS_RISK_CLAIM_TIMEOUT_SECONDS": 239.9},                     # rule (iv): 120 + 120 = 240
    {"PROCESS_RISK_CLAIM_MARGIN_SECONDS": 180.1},
    {"PROCESS_RISK_BACKOFF_CAP_SECONDS": 300}, {"PROCESS_RISK_BACKOFF_CAP_SECONDS": 4},           # rule (v)
    {"PROCESS_RISK_BACKOFF_BASE_SECONDS": 61}, {"PROCESS_RISK_BACKOFF_BASE_SECONDS": 0.09},
    {"PROCESS_RISK_BACKOFF_BASE_SECONDS": 1e-9}, {"PROCESS_RISK_BACKOFF_BASE_SECONDS": 5e-324},
    {"PROCESS_RISK_BACKOFF_BASE_SECONDS": 0}, {"PROCESS_RISK_BACKOFF_CAP_SECONDS": 0.09},
    {"PROCESS_RISK_IDLE_INTERVAL_SECONDS": 0.09}, {"PROCESS_RISK_IDLE_INTERVAL_SECONDS": 0},
    {"PROCESS_RISK_IDLE_INTERVAL_SECONDS": -1},
    {"PROCESS_RISK_MAX_TARGETS": 0}, {"PROCESS_RISK_MAX_TARGETS": -1},                           # rule (vi)
    {"PROCESS_RISK_MAX_TARGETS": 501, "PROCESS_RISK_REQUEST_TIMEOUT_SECONDS": 5030, "PROCESS_RISK_PASS_SECONDS": 5050,
     "PROCESS_RISK_CLAIM_TIMEOUT_SECONDS": 5150},
    {"PROCESS_RISK_MAX_TARGETS": 10**400}, {"PROCESS_RISK_MAX_TARGETS": 8.0},
    {"PROCESS_RISK_NORMAL_UNRESOLVED_PER_TASK": 0}, {"PROCESS_RISK_NORMAL_UNRESOLVED_PER_TASK": 2},
    {"PROCESS_RISK_TARGETS_DB_LIMIT": 7}, {"PROCESS_RISK_TARGETS_DB_LIMIT": 0},
    {"PROCESS_RISK_MAX_ATTEMPTS": 0}, {"PROCESS_RISK_MAX_ATTEMPTS": 1.5},                        # rule (vii)
    {"PROCESS_RISK_BATCH_REQUESTS": 0}, {"PROCESS_RISK_BATCH_ROWS": 0}, {"PROCESS_RISK_BATCH_ROWS": -1},
    {"PROCESS_RECHECK_MAX_PER_WINDOW": 0}, {"PROCESS_RECHECK_MAX_PER_WINDOW": 4.0},
    {"PROCESS_RECHECK_COOLDOWN_BASE_SECONDS": 901}, {"PROCESS_RECHECK_COOLDOWN_CAP_SECONDS": 59},
    {"PROCESS_RECHECK_COOLDOWN_CAP_SECONDS": 3601}, {"PROCESS_RECHECK_WINDOW_SECONDS": 899},
    {"PROCESS_RECHECK_COOLDOWN_BASE_SECONDS": 0}, {"PROCESS_RECHECK_COOLDOWN_CAP_SECONDS": -1},
    {"PROCESS_RECHECK_WINDOW_SECONDS": 0},
    {"PROCESS_RISK_ROW_OVERHEAD_SECONDS": 0}, {"PROCESS_RISK_ROW_TIMEOUT_SECONDS": 0},
    {"PROCESS_RISK_REQUEST_TIMEOUT_SECONDS": -1}, {"PROCESS_RISK_PASS_SECONDS": 0}, {"PROCESS_RISK_CLAIM_TIMEOUT_SECONDS": 0},
]


@pytest.mark.parametrize("overrides", RISK_VALID, ids=case_id)
def test_risk_limits_accept_valid_values_and_boundaries(monkeypatch, overrides):
    accepted(V.validate_process_risk_limits, monkeypatch, **overrides)


@pytest.mark.parametrize("overrides", RISK_INVALID, ids=case_id)
def test_risk_limits_refuse_each_violated_rule(monkeypatch, overrides):
    refused(V.validate_process_risk_limits, monkeypatch, **overrides)


@pytest.mark.parametrize("name", RISK_TIME_NAMES)
@pytest.mark.parametrize("bad", SHAPE_SET)
def test_every_risk_time_constant_refuses_the_shape_set(monkeypatch, name, bad):
    refused(V.validate_process_risk_limits, monkeypatch, **{name: bad})


@pytest.mark.parametrize("name", RISK_COUNT_NAMES)
@pytest.mark.parametrize("bad", [*[p.values[0] for p in SHAPE_SET], 2.5, 1.0])
def test_every_risk_count_refuses_the_shape_set_and_a_float(monkeypatch, name, bad):
    refused(V.validate_process_risk_limits, monkeypatch, **{name: bad})


def test_risk_defaults_meet_every_rule_exactly_where_the_design_says():
    assert P.PROCESS_TERMINATE_GRACE_SECONDS + P.JOB_CLOSE_SETTLE_SECONDS + P.PROCESS_RISK_ROW_OVERHEAD_SECONDS == 10 \
        == P.PROCESS_RISK_ROW_TIMEOUT_SECONDS
    assert P.PROCESS_RISK_MAX_TARGETS * P.PROCESS_RISK_ROW_TIMEOUT_SECONDS + P.PROCESS_RISK_DB_OVERHEAD_SECONDS == 100
    assert P.PROCESS_RISK_REQUEST_TIMEOUT_SECONDS + P.PROCESS_RISK_PASS_OVERHEAD_SECONDS == 140 <= P.PROCESS_RISK_PASS_SECONDS
    assert P.PROCESS_RISK_REQUEST_TIMEOUT_SECONDS + P.PROCESS_RISK_CLAIM_MARGIN_SECONDS == 240 <= P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS
    assert P.PROCESS_RISK_BACKOFF_CAP_SECONDS < P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS


# --- non-timing constants (design 9.3) --------------------------------------------------------------------------------------
INPUT_VALID = [
    {}, {"JOB_TERMINATE_EXIT_CODE": 1}, {"JOB_TERMINATE_EXIT_CODE": 2_147_483_647}, {"DIAGNOSTIC_MAX_ANOMALIES": 1},
    {"DIAGNOSTIC_MAX_ANOMALIES": 10**400}, {"ORPHAN_SWEEP_STALL_THRESHOLD": 1}, {"ORPHAN_SWEEP_STALL_THRESHOLD": 1000},
    {"ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP": 0}, {"ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP": 62},
]
INPUT_INVALID = [
    {"JOB_TERMINATE_EXIT_CODE": 0}, {"JOB_TERMINATE_EXIT_CODE": -1}, {"JOB_TERMINATE_EXIT_CODE": 2_147_483_648},
    {"JOB_TERMINATE_EXIT_CODE": 10**400}, {"JOB_TERMINATE_EXIT_CODE": 1.0},
    {"DIAGNOSTIC_MAX_ANOMALIES": 0}, {"DIAGNOSTIC_MAX_ANOMALIES": -1}, {"DIAGNOSTIC_MAX_ANOMALIES": 1.5},
    {"ORPHAN_SWEEP_STALL_THRESHOLD": 0}, {"ORPHAN_SWEEP_STALL_THRESHOLD": -1}, {"ORPHAN_SWEEP_STALL_THRESHOLD": 1001},
    {"ORPHAN_SWEEP_STALL_THRESHOLD": 10**400}, {"ORPHAN_SWEEP_STALL_THRESHOLD": 5.0},
    {"ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP": -1}, {"ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP": 63},
    {"ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP": 10**400}, {"ORPHAN_SWEEP_BACKOFF_EXPONENT_CAP": 16.0},
    {"REQUESTED_BY_MAX_LENGTH": 127}, {"REQUESTED_BY_MAX_LENGTH": 129}, {"REQUESTED_BY_MAX_LENGTH": 128.0},
    {"PROCESS_RISK_RATIONALE_MAX_LENGTH": 1999}, {"PROCESS_RISK_RATIONALE_MAX_LENGTH": 2001},
    {"PROCESS_RISK_RATIONALE_MAX_LENGTH": 10**400},
    {"DETAIL_SCHEMA_VERSION": 2}, {"DETAIL_SCHEMA_VERSION": 0}, {"DISPOSITION_KIND_RISK_ACCEPTED": "ACCEPTED"},
    {"REQUESTED_BY_PATTERN": r"[A-Za-z0-9._@:+ -]+"},                  # admits a space
    {"REQUESTED_BY_PATTERN": r"[A-Za-z0-9._@:+\n-]+"},                 # admits a control character
    {"REQUESTED_BY_PATTERN": r"\S+"}, {"REQUESTED_BY_PATTERN": r".+"}, {"REQUESTED_BY_PATTERN": "[A-Za-z0-9._@:+-"},
    {"REQUESTED_BY_PATTERN": "[A-Za-z0-9._@:+-é]+"}, {"REQUESTED_BY_PATTERN": None},
    {"RISK_ACCEPTANCE_UUID_RE": "[0-9"}, {"RISK_ACCEPTANCE_TS_RE": "("}, {"RISK_ACCEPTANCE_RATIONALE_FORBIDDEN": "["},
    {"RISK_ACCEPTANCE_REQUESTED_BY_RE": "^[A-Za-z0-9._@:+-]{1,64}$"},
    {"RISK_ACCEPTANCE_REQUESTED_BY_RE": "("},
    {"BACKGROUND_COMPONENT_IDS": ("sweeper", "sweeper", "capability")},
    {"BACKGROUND_COMPONENT_IDS": ("sweeper", "", "capability")},
    {"BACKGROUND_COMPONENT_IDS": ("sweeper", "risk:worker", "capability")},
    {"BACKGROUND_COMPONENT_IDS": ("sweeper", "risk-wörker", "capability")},
    {"BACKGROUND_COMPONENT_IDS": ("sweeper", "risk-worker")},
    {"BACKGROUND_COMPONENT_IDS": ("sweeper", "risk-worker", "capability", "extra")},
    {"BACKGROUND_COMPONENT_IDS": ["sweeper", "risk-worker", "capability"]},
    {"BACKGROUND_COMPONENT_IDS": ("risk-worker", "sweeper", "capability")},
    {"TRACKED_ONLY_KEYS": frozenset({"v"})}, {"TRACKED_ONLY_KEYS": ["v"] * 9},
]


@pytest.mark.parametrize("overrides", INPUT_VALID, ids=case_id)
def test_input_constants_accept_valid_values_and_boundaries(monkeypatch, overrides):
    accepted(V.validate_process_input_constants, monkeypatch, **overrides)


@pytest.mark.parametrize("overrides", INPUT_INVALID, ids=case_id)
def test_input_constants_refuse_violations(monkeypatch, overrides):
    refused(V.validate_process_input_constants, monkeypatch, **overrides)


@pytest.mark.parametrize("name", INPUT_COUNT_NAMES)
@pytest.mark.parametrize("bad", SHAPE_SET)
def test_every_input_count_refuses_the_shape_set(monkeypatch, name, bad):
    refused(V.validate_process_input_constants, monkeypatch, **{name: bad})


def test_the_schema_must_have_ten_keys_nine_required_and_requested_by_optional(monkeypatch):
    fields = list(P.RISK_ACCEPTANCE_SCHEMA)
    refused(V.validate_process_input_constants, monkeypatch, RISK_ACCEPTANCE_SCHEMA=tuple(fields[:-1]))          # nine
    monkeypatch.undo()
    refused(V.validate_process_input_constants, monkeypatch, RISK_ACCEPTANCE_SCHEMA=tuple(fields + fields[:1]))  # duplicate
    monkeypatch.undo()
    refused(V.validate_process_input_constants, monkeypatch, RISK_ACCEPTANCE_SCHEMA=list(fields))                # not a tuple
    monkeypatch.undo()
    swapped = [f if f.key != "applied_at" else P.RiskAcceptanceField("applied_at", False, "string") for f in fields]
    refused(V.validate_process_input_constants, monkeypatch, RISK_ACCEPTANCE_SCHEMA=tuple(swapped))
    monkeypatch.undo()
    refused(V.validate_process_input_constants, monkeypatch, RISK_ACCEPTANCE_REQUIRED_KEYS=frozenset(
        P.RISK_ACCEPTANCE_REQUIRED_KEYS | {"requested_by"}))
    monkeypatch.undo()
    refused(V.validate_process_input_constants, monkeypatch, RISK_ACCEPTANCE_REQUIRED_KEYS=frozenset(
        set(P.RISK_ACCEPTANCE_REQUIRED_KEYS) - {"pid"}))


@pytest.mark.parametrize("floor", [6.0, 1e300, 10**400])
def test_a_floor_above_a_constant_that_it_bounds_is_refused(monkeypatch, floor):
    with pytest.raises(ValueError):                      # 6.0 is above the restart delay base 5.0; the others above every one
        run(V.validate_process_timing_limits, monkeypatch, BACKGROUND_MIN_INTERVAL_SECONDS=floor)


def test_risk_arithmetic_overflow_is_normalised_into_valueerror(monkeypatch):
    with pytest.raises(ValueError) as info:
        run(V.validate_process_risk_limits, monkeypatch, PROCESS_RISK_ROW_TIMEOUT_SECONDS=10**400,
            PROCESS_RISK_DB_OVERHEAD_SECONDS=1.5)
    assert type(info.value) is ValueError


def test_an_overflow_never_leaves_a_validator_as_anything_but_valueerror(monkeypatch):
    monkeypatch.setattr(P, "TRACKED_IDENTITY_PERSIST_TIMEOUT_SECONDS", 10**400)   # int + float overflows in the live sum
    with pytest.raises(ValueError) as info:
        V.validate_process_timing_limits()
    assert type(info.value) is ValueError and isinstance(info.value.__cause__, OverflowError)


def test_no_environment_variable_or_setting_reads_the_new_constants():
    import inspect

    from app import config
    assert "os.environ" not in inspect.getsource(P) and "getenv" not in inspect.getsource(P)
    fields = set(config.Settings.model_fields)
    assert not [n for n in DEFAULTS if n.lower() in fields], "a B2 constant must not be a Settings field"
