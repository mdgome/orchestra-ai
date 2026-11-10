"""Profile completion criteria in the Definition of Done: a deterministic check of the reviewer's STORED evaluation.

Whether a criterion is really satisfied is the reviewer's judgement; the orchestrator does not verify it. What is checked
here is that the evaluation belongs to the current profile and covers exactly its criteria, each met with evidence.
Nothing the Definition of Done already required is weakened by adding criteria."""
import pytest

from app.services.completion_checker import (DoDInput, check_definition_of_done, evaluate_definition_of_done,
                                             profile_criteria_problems)

STEP = "STEP_SNAPSHOT_V1:" + "1" * 64
PROFILE_HASH = "a" * 64
CRITERIA = ({"id": "DOC", "text": "documented"}, {"id": "PERF", "text": "fast enough"})
TEST_OUT = {"test_result": {"passed": True, "junit_ok": True, "executed": 2, "skipped": 0}, "code_fingerprint": STEP,
            "requirements_hash": "r", "test_policy_hash": "p"}


def evaluation(**override):
    review = {"verdict": "PASS", "issues": [], "code_fingerprint": STEP, "profile_hash": PROFILE_HASH,
              "profile_criteria": [{"id": "DOC", "met": True, "evidence": "README section 3"},
                                   {"id": "PERF", "met": True, "evidence": "benchmark in tests"}]}
    review.update(override)
    return review


def dod(review, criteria=CRITERIA, profile_hash=PROFILE_HASH, test=None):
    return DoDInput(test_output=test or TEST_OUT, review=review, current_code_fingerprint=STEP, requirements_hash="r",
                    test_policy_hash="p", design_review_passed=True, pending_approvals=0, profile_hash=profile_hash,
                    profile_criteria=criteria)


def test_a_complete_evaluation_for_the_current_profile_lets_the_task_complete():
    assert check_definition_of_done(dod(evaluation())) == []
    assert profile_criteria_problems(CRITERIA, PROFILE_HASH, evaluation()) == []


@pytest.mark.parametrize("override,fragment", [
    ({"profile_criteria": [{"id": "DOC", "met": True, "evidence": "x"}]}, "criterion PERF was not evaluated"),
    ({"profile_criteria": []}, "criterion DOC was not evaluated"),
    ({"profile_criteria": [{"id": "DOC", "met": True, "evidence": "a"}, {"id": "DOC", "met": True, "evidence": "b"},
                           {"id": "PERF", "met": True, "evidence": "c"}]}, "criterion DOC is evaluated more than once"),
    ({"profile_criteria": [{"id": "DOC", "met": True, "evidence": "a"}, {"id": "PERF", "met": True, "evidence": "b"},
                           {"id": "GHOST", "met": True, "evidence": "c"}]}, "unknown criterion 'GHOST'"),
    ({"profile_criteria": [{"id": "DOC", "met": True, "evidence": ""}, {"id": "PERF", "met": True, "evidence": "b"}]},
     "criterion DOC has no evidence"),
    ({"profile_criteria": [{"id": "DOC", "met": True, "evidence": "   "}, {"id": "PERF", "met": True, "evidence": "b"}]},
     "criterion DOC has no evidence"),
    ({"profile_criteria": [{"id": "DOC", "met": True}, {"id": "PERF", "met": True, "evidence": "b"}]},
     "criterion DOC has no evidence"),
    ({"profile_criteria": [{"id": "DOC", "met": False, "evidence": "missing"}, {"id": "PERF", "met": True, "evidence": "b"}]},
     "criterion DOC is not met"),
    ({"profile_criteria": [{"id": "DOC", "met": "yes", "evidence": "a"}, {"id": "PERF", "met": True, "evidence": "b"}]},
     "criterion DOC is not met"),
    ({"profile_criteria": "all good"}, "the criteria were not evaluated"),
    ({"profile_criteria": None}, "the criteria were not evaluated"),
    ({"profile_hash": "b" * 64}, "the review was made for a different profile"),
    ({"profile_hash": None}, "the review was made for a different profile"),
])
def test_every_way_the_stored_evaluation_can_be_wrong_blocks_completion(override, fragment):
    problems = check_definition_of_done(dod(evaluation(**override)))
    assert any(fragment in p for p in problems), problems
    assert any(p.startswith("profile completion criteria are not satisfied") for p in problems)


def test_an_evaluation_that_lacks_the_key_entirely_is_not_a_pass():
    review = evaluation()
    del review["profile_criteria"]
    assert check_definition_of_done(dod(review))


def test_an_entry_that_is_not_an_object_is_not_counted():
    review = evaluation(profile_criteria=["DOC", "PERF"])
    problems = profile_criteria_problems(CRITERIA, PROFILE_HASH, review)
    assert "criterion DOC was not evaluated" in problems and "unknown criterion None" in problems


def test_an_evaluation_made_for_an_older_profile_is_not_reused_after_the_profile_changed():
    old = evaluation()
    new_criteria = ({"id": "DOC", "text": "documented"}, {"id": "SEC", "text": "reviewed for security"})
    problems = check_definition_of_done(dod(old, criteria=new_criteria, profile_hash="c" * 64))
    assert any("different profile" in p for p in problems) and any("SEC was not evaluated" in p for p in problems)


def test_criteria_never_weaken_what_the_definition_of_done_already_requires():
    failing_tests = {**TEST_OUT, "test_result": {"passed": False, "junit_ok": True, "executed": 2, "skipped": 0}}
    problems = check_definition_of_done(dod(evaluation(), test=failing_tests))      # criteria all met, tests failed
    assert "required tests did not pass" in problems
    assert check_definition_of_done(dod(evaluation(verdict="FAIL")))                # criteria met, verdict FAIL
    assert check_definition_of_done(dod(evaluation(issues=[{"severity": "CRITICAL", "description": "x"}])))
    assert check_definition_of_done(dod(evaluation(test_policy_concern=True)))


def test_without_criteria_the_conditions_are_exactly_the_original_ones():
    ids = [c.id for c in evaluate_definition_of_done(dod(evaluation(), criteria=(), profile_hash=None))]
    assert "profile_criteria_met" not in ids and len(ids) == 13
    # an evaluation a task without criteria happens to carry is simply ignored
    assert check_definition_of_done(dod(evaluation(profile_criteria="junk"), criteria=(), profile_hash=None)) == []


def test_a_missing_review_leaves_the_criteria_condition_unevaluated():
    conditions = {c.id: c for c in evaluate_definition_of_done(DoDInput(
        test_output=TEST_OUT, review=None, current_code_fingerprint=STEP, requirements_hash="r", test_policy_hash="p",
        design_review_passed=True, pending_approvals=0, profile_hash=PROFILE_HASH, profile_criteria=CRITERIA))}
    assert conditions["profile_criteria_met"].met is None and conditions["review_present"].met is False
