from app.services.completion_checker import DoDInput, check_definition_of_done

STEP = "STEP_SNAPSHOT_V1:" + "1" * 64
TEST_OUT = {"test_result": {"passed": True, "junit_ok": True, "executed": 2, "skipped": 0}, "code_fingerprint": STEP,
            "requirements_hash": "r", "test_policy_hash": "p"}
REVIEW = {"verdict": "PASS", "issues": [], "code_fingerprint": STEP}


def dod(fingerprint=STEP, test=None, review=None):
    return DoDInput(test_output=test or {**TEST_OUT, "code_fingerprint": fingerprint},
                    review=review or {**REVIEW, "code_fingerprint": fingerprint}, current_code_fingerprint=fingerprint,
                    requirements_hash="r", test_policy_hash="p", design_review_passed=True, pending_approvals=0)


def test_a_current_version_fingerprint_can_complete():
    assert check_definition_of_done(dod()) == []


def test_an_old_unversioned_or_foreign_prefix_fingerprint_can_never_complete():
    for value in ("a" * 64, "WORKSPACE_FINGERPRINT_V1:" + "1" * 64, "STEP_SNAPSHOT_V0:" + "1" * 64, ""):
        problems = check_definition_of_done(dod(fingerprint=value))
        assert any("snapshot version" in p for p in problems), value
