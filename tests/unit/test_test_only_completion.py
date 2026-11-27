"""Completion conditions of a TEST_ONLY Task (a Project Test Run): pure function, no database."""
import pytest

from app.services.completion_checker import (
    TestOnlyInput, evaluate_test_only_completion, failure_reason_of_test_only,
)
from app.validation import policies as P

PASSED = {"passed": True, "junit_ok": True, "executed": 3, "skipped": 0, "outcomes": ["SUCCESS"], "exit_codes": [0],
          "failures": []}


def verdict(result=None, *, policy_unchanged=True, cancel=False, minimum=1, tree=True):
    conditions = evaluate_test_only_completion(TestOnlyInput(
        test_policy_unchanged=policy_unchanged, cancel_requested=cancel, test_result=PASSED if result is None else result,
        min_tests_executed=minimum, process_tree_confirmed=tree))
    return conditions, failure_reason_of_test_only(conditions)


def test_a_clean_passing_run_meets_every_condition():
    conditions, reason = verdict()
    assert all(c.met is True for c in conditions) and reason is None
    assert {c.id for c in conditions} == {"TEST_POLICY_UNCHANGED", "NO_CANCEL_REQUESTED", "NO_TIMEOUT", "COMMANDS_SUCCEEDED",
                                          "JUNIT_OK", "NO_FAILED_TESTS", "MIN_TESTS_EXECUTED", "TESTS_PASSED",
                                          "PROCESS_TREE_TERMINATION_CONFIRMED"}


@pytest.mark.parametrize("result, expected", [
    ({**PASSED, "passed": False, "failures": ["a::b"]}, P.TEST_RUN_TESTS_FAILED),                       # a failing test
    ({**PASSED, "passed": False, "outcomes": ["ERROR"], "exit_codes": [1], "junit_ok": False}, P.TEST_RUN_TESTS_FAILED),   # crash, no XML
    ({**PASSED, "passed": False, "outcomes": ["TIMEOUT"], "exit_codes": [None], "junit_ok": False}, P.TEST_RUN_TESTS_TIMEOUT),
    ({**PASSED, "passed": False, "junit_ok": False, "executed": 0}, P.TEST_RUN_NO_JUNIT),               # exit 0 but no fresh result
    ({**PASSED, "passed": False, "executed": 1, "skipped": 1}, P.TEST_RUN_TOO_FEW_TESTS),               # everything skipped
    ({}, P.TEST_RUN_TESTS_FAILED),                                                                      # nothing recorded is never a pass
])
def test_each_way_a_run_can_fail_has_its_own_reason(result, expected):
    conditions, reason = verdict(result, minimum=1)
    assert reason == expected and any(c.met is False for c in conditions)


def test_the_passed_flag_alone_is_not_trusted_without_the_other_facts():
    conditions, reason = verdict({"passed": True, "junit_ok": False, "outcomes": ["SUCCESS"], "executed": 0})
    assert reason == P.TEST_RUN_NO_JUNIT                                   # JUnit missing beats a claimed pass
    _, reason = verdict({"passed": True, "junit_ok": True, "outcomes": [], "executed": 2})
    assert reason == P.TEST_RUN_TESTS_FAILED                               # no recorded command outcome


def test_a_cancel_request_or_a_changed_policy_wins_over_a_passing_run():
    assert verdict(cancel=True)[1] == P.TEST_RUN_CANCEL_REQUESTED
    assert verdict(policy_unchanged=False)[1] == P.TEST_RUN_TEST_POLICY_CHANGED
    assert verdict(policy_unchanged=False, cancel=True)[1] == P.TEST_RUN_TEST_POLICY_CHANGED     # the first unmet one is reported
    assert verdict({**PASSED, "passed": False, "failures": ["x"]}, cancel=True)[1] == P.TEST_RUN_CANCEL_REQUESTED


def test_the_required_minimum_comes_from_the_policy():
    assert verdict({**PASSED, "executed": 2}, minimum=2)[1] is None
    assert verdict({**PASSED, "executed": 2}, minimum=3)[1] == P.TEST_RUN_TOO_FEW_TESTS


def test_an_unproven_process_tree_blocks_completion_but_has_no_failure_reason():
    conditions, reason = verdict(tree=False)
    assert [c.id for c in conditions if c.met is False] == ["PROCESS_TREE_TERMINATION_CONFIRMED"]
    assert reason is None                                                  # the caller fails closed instead of reporting a code


def test_the_failure_reasons_are_exactly_the_policy_constants():
    from app.services.completion_checker import TEST_ONLY_FAILURE_REASONS
    assert {reason for _, reason in TEST_ONLY_FAILURE_REASONS} == {
        P.TEST_RUN_TEST_POLICY_CHANGED, P.TEST_RUN_CANCEL_REQUESTED, P.TEST_RUN_TESTS_TIMEOUT, P.TEST_RUN_NO_JUNIT,
        P.TEST_RUN_TESTS_FAILED, P.TEST_RUN_TOO_FEW_TESTS}              # PROJECT_BUSY is raised by the Runner path, not here
