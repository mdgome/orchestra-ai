"""The pure evaluation of the TRACKED_EXECUTIONS causes (design 14.1): normal progress (C4), the row causes, the request causes
and their independence of U. No database: the facts are handed over exactly as `TrackedDiagnosticsReader` loads them."""
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.repositories.task_repository import ExecutionFact
from app.services.diagnostics_service import Status, merge_causes
from app.services.project_terminal import TrackedAnomaly, TrackedBlock
from app.services.tracked_diagnostics import evaluate
from app.validation import policies as P

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
RUNNER = "runner-1"


def uid(n):
    return uuid.UUID(int=n)


def fact(n=1, task=100, run=200, runner=RUNNER, age=60, finished=False):
    return ExecutionFact(uid(n), uid(task), uid(run) if run is not None else None, runner,
                         NOW - timedelta(seconds=age), NOW if finished else None)


def run(status="RUNNING", runner=RUNNER, lease=3600):
    return SimpleNamespace(status=status, runner_id=runner, lease_expires_at=NOW + timedelta(seconds=lease))


def check(facts, *, tasks=None, runs=None, steps=None, unresolved=None, anomalies=(), requests=(), latest=None, capability=None,
          anomaly_exact=True, total=None):
    facts = facts if isinstance(facts, list) else [facts]
    block = TrackedBlock(unresolved_ids=tuple(f.id for f in facts) if unresolved is None else tuple(unresolved),
                         anomalies=tuple(anomalies), anomalies_total=len(anomalies) if total is None else total,
                         anomalies_total_is_exact=anomaly_exact, anomalies_truncated=not anomaly_exact)
    verdict = evaluate(block, {f.id: f for f in facts}, tasks if tasks is not None else {uid(100): "TESTING"},
                       runs if runs is not None else {uid(200): run()}, steps if steps is not None else {uid(100): "RUNNING"},
                       requests, latest, capability, NOW)
    return {c.reason_code: c for c in verdict.causes}, verdict


def anomaly(n=9, task=100, sub=None):
    return TrackedAnomaly(uid(n), uid(task), uid(7), "OTHER_SCHEMA_INVALID" if sub else "BAD_PROOF", sub)


# --- normal progress (C4) -----------------------------------------------------------------------------------------------------
def test_an_open_row_of_a_testing_task_with_a_running_step_run_lease_and_runner_is_normal_and_not_a_cause():
    causes, verdict = check(fact())
    assert causes == {} and verdict.running == 1


def test_the_pre_step_ids_before_call_with_no_tester_step_yet_is_normal():
    causes, verdict = check(fact(), steps={uid(100): None})
    assert causes == {} and verdict.running == 1


def test_a_waiting_approval_task_with_a_still_valid_lease_is_task_not_executing_never_normal():
    causes, verdict = check(fact(), tasks={uid(100): "WAITING_APPROVAL"}, runs={uid(200): run(lease=3000)})
    assert set(causes) == {"TASK_NOT_EXECUTING"} and verdict.running == 0
    assert causes["TASK_NOT_EXECUTING"].related_execution_ids == (uid(1),) and causes["TASK_NOT_EXECUTING"].severity is Status.WARN


@pytest.mark.parametrize("step", ["INTERRUPTED", "NEEDS_MANUAL_RECOVERY", "WAITING_APPROVAL"])
def test_a_testing_task_with_a_parked_tester_step_is_not_executing(step):
    causes, verdict = check(fact(), steps={uid(100): step})
    assert set(causes) == {"TASK_NOT_EXECUTING"} and verdict.running == 0


def test_a_non_testing_non_terminal_task_is_not_executing():
    causes, _ = check(fact(), tasks={uid(100): "DEVELOPING"})
    assert set(causes) == {"TASK_NOT_EXECUTING"}


def test_an_anomaly_anywhere_in_the_project_makes_an_otherwise_normal_row_not_normal_without_adding_a_row_cause():
    causes, verdict = check(fact(), anomalies=[anomaly(9, task=101)], tasks={uid(100): "TESTING", uid(101): "TESTING"})
    assert set(causes) == {"TRACKED_EXECUTION_ANOMALY"} and verdict.running == 0


# --- the row causes ----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("closed", [False, True])
def test_a_terminal_task_has_only_the_terminal_cause_for_an_open_or_a_closed_unproven_row(closed):
    causes, _ = check(fact(finished=closed, run=None), tasks={uid(100): "FAILED"}, runs={})
    assert set(causes) == {"TASK_TERMINAL_WITH_UNRESOLVED"} and causes["TASK_TERMINAL_WITH_UNRESOLVED"].severity is Status.WARN


def test_a_terminal_task_with_an_anomaly_row_has_the_terminal_cause_and_the_fail_anomaly_cause():
    causes, _ = check([], tasks={uid(100): "CANCELLED"}, anomalies=[anomaly()])
    assert set(causes) == {"TASK_TERMINAL_WITH_UNRESOLVED", "TRACKED_EXECUTION_ANOMALY"}
    assert causes["TRACKED_EXECUTION_ANOMALY"].severity is Status.FAIL
    assert causes["TASK_TERMINAL_WITH_UNRESOLVED"].related_execution_ids == (uid(9),)


@pytest.mark.parametrize("the_run", [None, "FINISHED", "ABANDONED"])
def test_no_run_or_a_run_that_is_not_running_is_no_owning_run(the_run):
    runs = {} if the_run is None else {uid(200): run(status=the_run)}
    causes, _ = check(fact(run=None if the_run is None else 200), runs=runs)
    assert set(causes) == {"NO_OWNING_RUN"}


def test_an_expired_lease_is_lease_expired_and_the_boundary_counts_as_expired():
    causes, _ = check(fact(), runs={uid(200): run(lease=-1)})
    assert set(causes) == {"LEASE_EXPIRED"}
    causes, _ = check(fact(), runs={uid(200): run(lease=0)})
    assert set(causes) == {"LEASE_EXPIRED"}
    causes, _ = check(fact(), runs={uid(200): run(lease=1)})
    assert causes == {}


def test_a_runner_id_mismatch_is_runner_mismatch():
    causes, _ = check(fact(runner="another-runner"))
    assert set(causes) == {"RUNNER_MISMATCH"}


def test_the_age_limit_is_exclusive_and_an_old_normal_row_has_execution_too_old_alone():
    limit = P.TRACKED_EXECUTION_MAX_AGE_SECONDS
    assert check(fact(age=limit))[0] == {}                                         # a long-running normal command within the limit
    causes, verdict = check(fact(age=limit + 1))
    assert set(causes) == {"EXECUTION_TOO_OLD"} and verdict.running == 0


def test_an_old_row_with_an_expired_lease_has_both_causes_and_a_young_failed_row_only_its_own():
    causes, _ = check(fact(age=P.TRACKED_EXECUTION_MAX_AGE_SECONDS + 5), runs={uid(200): run(lease=-5)})
    assert set(causes) == {"LEASE_EXPIRED", "EXECUTION_TOO_OLD"}
    causes, _ = check(fact(age=5), runs={uid(200): run(lease=-5)})
    assert set(causes) == {"LEASE_EXPIRED"}


def test_a_closed_unproven_row_of_a_non_terminal_task_is_closed_unproven_only():
    causes, _ = check(fact(finished=True))
    assert set(causes) == {"CLOSED_UNPROVEN"}


def test_the_causes_of_several_rows_are_merged_with_their_ids_ascending_and_bounded():
    many = [fact(n=n, run=None) for n in range(P.DIAGNOSTIC_MAX_RELATED_IDS + 20, 0, -1)]
    causes, _ = check(many, runs={})
    ids = causes["NO_OWNING_RUN"].related_execution_ids
    assert len(ids) == P.DIAGNOSTIC_MAX_RELATED_IDS and list(ids) == sorted(ids) and ids[0] == uid(1)


# --- the anomaly cause --------------------------------------------------------------------------------------------------------
def test_the_anomaly_cause_carries_the_bounded_list_the_counts_and_may_be_permanently_stuck():
    items = [anomaly(9), anomaly(10, sub="decode_error")]
    causes, _ = check([], anomalies=items)
    cause = causes["TRACKED_EXECUTION_ANOMALY"]
    assert cause.anomalies == tuple(items) and cause.anomalies_returned == 2 and cause.anomalies_truncated is False
    assert cause.anomalies_total == 2 and cause.anomalies_total_is_exact is True and cause.may_be_permanently_stuck is True
    assert cause.severity is Status.FAIL and cause.blocks_execution is False
    assert cause.related_execution_ids == (uid(9), uid(10))


def test_a_truncated_anomaly_list_has_an_unknown_total_and_a_plain_anomaly_is_not_permanently_stuck():
    causes, _ = check([], anomalies=[anomaly(9)], anomaly_exact=False)
    cause = causes["TRACKED_EXECUTION_ANOMALY"]
    assert cause.anomalies_truncated is True and cause.anomalies_total is None and cause.anomalies_total_is_exact is False
    assert cause.may_be_permanently_stuck is False


def test_a_cause_that_is_not_an_anomaly_has_the_neutral_anomaly_fields():
    causes, _ = check(fact(), runs={})
    c = causes["NO_OWNING_RUN"]
    assert (c.anomalies, c.anomalies_returned, c.anomalies_truncated, c.anomalies_total, c.anomalies_total_is_exact,
            c.may_be_permanently_stuck, c.sweeper, c.config_invalid, c.components) == ((), 0, False, None, False, False, None, None, ())


# --- the capability mismatch -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("state,expected", [("UNAVAILABLE", True), ("UNSUPPORTED_PLATFORM", True), ("SUPPORTED", False),
                                            ("STATUS_UNKNOWN", False), (None, False)])
def test_an_open_row_with_an_unavailable_or_unsupported_capability_is_a_mismatch(state, expected):
    causes, _ = check(fact(), capability=state)
    assert ("CAPABILITY_MISMATCH" in causes) is expected


def test_only_a_closed_row_never_gives_a_capability_mismatch():
    causes, _ = check(fact(finished=True), capability="UNAVAILABLE")
    assert "CAPABILITY_MISMATCH" not in causes


# --- the request causes, independent of U --------------------------------------------------------------------------------------
def request(n, status, *, due_ago=0, attempts=0, claimed_ago=None, reason=None):
    return SimpleNamespace(approval_id=uid(n), status=status, next_attempt_at=NOW - timedelta(seconds=due_ago),
                           attempts=attempts, reason=reason,
                           claimed_at=None if claimed_ago is None else NOW - timedelta(seconds=claimed_ago))


STALE, CLAIM = P.PROCESS_RISK_REQUEST_STALE_SECONDS, P.PROCESS_RISK_CLAIM_TIMEOUT_SECONDS


def test_a_stale_requested_request_is_request_without_worker_although_u_is_empty():
    causes, verdict = check([], requests=[request(1, "REQUESTED", due_ago=STALE + 1)])
    assert set(causes) == {"REQUEST_WITHOUT_WORKER"} and causes["REQUEST_WITHOUT_WORKER"].related_request_ids == (uid(1),)
    assert verdict.running == 0


def test_a_fresh_request_and_a_request_at_the_boundary_give_no_cause_and_a_due_retryable_one_does():
    assert check([], requests=[request(1, "REQUESTED", due_ago=STALE)])[0] == {}
    assert check([], requests=[request(1, "REQUESTED", due_ago=1)])[0] == {}
    assert set(check([], requests=[request(1, "RETRYABLE", due_ago=STALE + 1, attempts=1)])[0]) == {"REQUEST_WITHOUT_WORKER"}


def test_a_request_waiting_for_its_scheduled_next_attempt_is_not_flagged_but_one_past_due_plus_the_threshold_is():
    """The boundary of REQUEST_WITHOUT_WORKER: the clock is `next_attempt_at`, not the creation time. A RETRYABLE request that is
    waiting out its backoff (`next_attempt_at` in the FUTURE) is working as designed; only a request due longer than the stale
    limit ago has no worker."""
    waiting_far = request(1, "RETRYABLE", due_ago=-3600, attempts=1)                # scheduled an hour ahead
    waiting_near = request(2, "RETRYABLE", due_ago=-1, attempts=1)
    just_due = request(3, "RETRYABLE", due_ago=0, attempts=1)
    inside_threshold = request(4, "RETRYABLE", due_ago=STALE, attempts=1)
    past_threshold = request(5, "RETRYABLE", due_ago=STALE + 1, attempts=1)
    for fresh in (waiting_far, waiting_near, just_due, inside_threshold):
        assert "REQUEST_WITHOUT_WORKER" not in check([], requests=[fresh])[0]
    flagged = check([], requests=[waiting_far, past_threshold, waiting_near])[0]["REQUEST_WITHOUT_WORKER"]
    assert flagged.related_request_ids == (uid(5),)                                # only the one past due + threshold


def test_a_verifying_request_claimed_longer_than_the_claim_timeout_is_a_claim_timeout():
    assert set(check([], requests=[request(1, "VERIFYING", claimed_ago=CLAIM + 1)])[0]) == {"REQUEST_CLAIM_TIMEOUT"}
    assert check([], requests=[request(1, "VERIFYING", claimed_ago=CLAIM)])[0] == {}


def test_retryable_at_max_attempts_minus_one_is_retry_stuck_and_below_it_is_not():
    near = request(1, "RETRYABLE", attempts=P.PROCESS_RISK_MAX_ATTEMPTS - 1)
    assert set(check([], requests=[near])[0]) == {"REQUEST_RETRY_STUCK"}
    assert check([], requests=[request(1, "RETRYABLE", attempts=P.PROCESS_RISK_MAX_ATTEMPTS - 2)])[0] == {}


def test_an_inconclusive_latest_request_is_retry_stuck_only_while_u_is_non_empty():
    latest = SimpleNamespace(approval_id=uid(5), status="NEEDS_REDECISION", reason="PROCESS_RECHECK_INCONCLUSIVE")
    assert set(check(fact(), latest=latest)[0]) == {"REQUEST_RETRY_STUCK"}
    assert check([], latest=latest)[0] == {}
    other = SimpleNamespace(approval_id=uid(5), status="NEEDS_REDECISION", reason="SOMETHING_ELSE")
    assert check(fact(), latest=other)[0] == {}


def test_several_causes_at_once_are_each_listed_once_ordered_by_severity_then_code():
    causes, verdict = check([fact(1, run=None), fact(2, finished=True)], runs={}, anomalies=[anomaly(9, task=101)],
                            requests=[request(3, "REQUESTED", due_ago=STALE + 1)],
                            tasks={uid(100): "TESTING", uid(101): "TESTING"})
    merged = merge_causes(verdict.causes)
    assert [c.reason_code for c in merged] == ["TRACKED_EXECUTION_ANOMALY", "CLOSED_UNPROVEN", "NO_OWNING_RUN",
                                               "REQUEST_WITHOUT_WORKER"]
    assert [c.severity for c in merged] == [Status.FAIL, Status.WARN, Status.WARN, Status.WARN]
