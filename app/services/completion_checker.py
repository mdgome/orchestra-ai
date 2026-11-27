"""Definition of Done. Anything unclear is NOT a pass."""
from dataclasses import dataclass

from app.infra.workspace_snapshot import is_current_snapshot_version
from app.services.artifacts import has_critical
from app.validation import policies as P


@dataclass(frozen=True)
class DoDInput:
    test_output: dict | None            # output of the current TEST step
    review: dict | None                 # output of the current CODE_REVIEW step
    current_code_fingerprint: str
    requirements_hash: str
    test_policy_hash: str
    design_review_passed: bool          # PASS recorded for the *current* design_review_basis
    pending_approvals: int
    # Profile completion criteria (empty for a task without a profile or without criteria): the criteria the CURRENT
    # profile snapshot requires, and its hash. The reviewer's evaluation is stored in the review output.
    profile_hash: str | None = None
    profile_criteria: tuple = ()


@dataclass(frozen=True)
class Condition:
    """One Definition-of-Done condition. `met` is None when it could not be evaluated because a prerequisite is missing
    (tests that never ran have no verdict to inspect); `message` is the problem text reported when it is NOT met."""
    id: str
    met: bool | None
    message: str


def profile_criteria_problems(expected: tuple, profile_hash: str | None, review: dict) -> list[str]:
    """Deterministic check of the reviewer's STORED evaluation of the profile's completion criteria. Whether a criterion is
    really satisfied is the reviewer's judgement; the orchestrator does not verify it independently. What it does enforce:
    the evaluation belongs to the current profile and covers exactly the current criteria, once each, each marked met
    with a non-empty evidence."""
    problems: list[str] = []
    if review.get("profile_hash") != profile_hash:
        problems.append("the review was made for a different profile")
    entries = review.get("profile_criteria")
    if not isinstance(entries, list):
        return [*problems, "the criteria were not evaluated"]
    wanted = [c["id"] for c in expected]
    seen: dict[str, dict] = {}
    for entry in entries:
        cid = entry.get("id") if isinstance(entry, dict) else None
        if cid in seen:
            problems.append(f"criterion {cid} is evaluated more than once")
        elif cid not in wanted:
            problems.append(f"unknown criterion {cid!r}")
        else:
            seen[cid] = entry
    problems += [f"criterion {cid} was not evaluated" for cid in wanted if cid not in seen]
    for cid, entry in seen.items():
        if entry.get("met") is not True:
            problems.append(f"criterion {cid} is not met")
        elif not isinstance(entry.get("evidence"), str) or not entry["evidence"].strip():
            problems.append(f"criterion {cid} has no evidence")
    return problems


def evaluate_definition_of_done(d: DoDInput) -> list[Condition]:
    """Every condition with its own verdict, in a fixed order. Pure: it reads only `d`, writes nothing and decides
    nothing; `check_definition_of_done` is its single consumer for the completion decision."""
    out: list[Condition] = []

    def add(cond_id: str, met: bool | None, message: str) -> None:
        out.append(Condition(cond_id, met, message))

    add("code_fingerprint_current", is_current_snapshot_version(d.current_code_fingerprint),
        "code fingerprint is missing or not of the current snapshot version")
    t = d.test_output
    add("tests_run", t is not None, "tests were not run")
    result = (t.get("test_result") or {}) if t is not None else {}
    add("tests_passed", None if t is None else bool(result.get("passed")), "required tests did not pass")
    add("tests_verifiable", None if t is None else bool(
        result.get("junit_ok") and (result.get("executed", 0) - result.get("skipped", 0)) >= 1),
        "test execution cannot be verified")
    add("tests_for_current_code", None if t is None else t.get("code_fingerprint") == d.current_code_fingerprint,
        "test result is not for the current code")
    add("tests_for_current_policy", None if t is None else (
        t.get("requirements_hash") == d.requirements_hash and t.get("test_policy_hash") == d.test_policy_hash),
        "test result was produced under different requirements or test policy")
    r = d.review
    add("review_present", r is not None, "code review missing")
    add("review_verdict_pass", None if r is None else r.get("verdict") == "PASS",
        f"code review verdict is {r.get('verdict') if r is not None else None}")
    add("review_no_critical_issue", None if r is None else not has_critical(r), "unresolved CRITICAL issue")
    add("review_no_weakened_verification", None if r is None else not r.get("test_policy_concern"),
        "reviewer flagged weakened verification")
    add("review_for_current_code", None if r is None else r.get("code_fingerprint") == d.current_code_fingerprint,
        "code review is not for the current code")
    add("design_review_valid", d.design_review_passed, "design review PASS is not valid for the current basis")
    if d.profile_criteria:       # an ADDITIONAL condition: nothing above is weakened by it
        listed = None if r is None else profile_criteria_problems(d.profile_criteria, d.profile_hash, r)
        add("profile_criteria_met", None if listed is None else not listed,
            "profile completion criteria are not satisfied: " + "; ".join(listed or []))
    add("no_pending_approval", not d.pending_approvals, "unresolved Human Gate")
    return out


def check_definition_of_done(d: DoDInput) -> list[str]:
    """Returns the list of unmet conditions; empty means the task may complete."""
    return [c.message for c in evaluate_definition_of_done(d) if c.met is False]


# --- Project Test Run (a TEST_ONLY Task): no plan, design review, code review or profile criteria apply -------------------
@dataclass(frozen=True)
class TestOnlyInput:
    __test__ = False                    # not a pytest class, whatever the name starts with
    test_policy_unchanged: bool         # the step recorded the very policy hash the Task was created with
    cancel_requested: bool              # Task.cancel_requested_at, read under the Task row lock in the completing transaction
    test_result: dict                   # TestRunResult.to_dict(): the orchestrator's own result, never the agent's word
    min_tests_executed: int
    process_tree_confirmed: bool        # no unterminated tracked execution is left for the Task (B2)


# condition id -> the Task failure_reason it produces when unmet, in the order the first unmet one is reported
TEST_ONLY_FAILURE_REASONS = (
    ("TEST_POLICY_UNCHANGED", P.TEST_RUN_TEST_POLICY_CHANGED),
    ("NO_CANCEL_REQUESTED", P.TEST_RUN_CANCEL_REQUESTED),
    ("NO_TIMEOUT", P.TEST_RUN_TESTS_TIMEOUT),
    ("COMMANDS_SUCCEEDED", P.TEST_RUN_TESTS_FAILED),
    ("JUNIT_OK", P.TEST_RUN_NO_JUNIT),
    ("NO_FAILED_TESTS", P.TEST_RUN_TESTS_FAILED),
    ("MIN_TESTS_EXECUTED", P.TEST_RUN_TOO_FEW_TESTS),
    ("TESTS_PASSED", P.TEST_RUN_TESTS_FAILED),            # the orchestrator's own verdict: the fallback for anything above missed
)


def evaluate_test_only_completion(d: TestOnlyInput) -> list[Condition]:
    """Completion conditions of a TEST_ONLY Task (same shape as the workflow's, so a report can show them). Anything unclear
    is NOT a pass. The lease and the claim fence are not listed: they are enforced structurally (the completing transaction
    raises LeaseLost / is fenced), they never produce a verdict."""
    result = d.test_result or {}
    outcomes = [str(o) for o in (result.get("outcomes") or [])]
    junit_ok = bool(result.get("junit_ok"))
    executed = int(result.get("executed") or 0) - int(result.get("skipped") or 0)
    return [
        Condition("TEST_POLICY_UNCHANGED", d.test_policy_unchanged, "the test policy changed while the tests ran"),
        Condition("NO_CANCEL_REQUESTED", not d.cancel_requested, "cancellation was requested"),
        Condition("NO_TIMEOUT", "TIMEOUT" not in outcomes, "a test command timed out"),
        Condition("COMMANDS_SUCCEEDED", bool(outcomes) and all(o == "SUCCESS" for o in outcomes),
                  "a test command did not succeed"),
        Condition("JUNIT_OK", junit_ok, "no usable JUnit result was produced by this run"),
        Condition("NO_FAILED_TESTS", not (result.get("failures") or []), "at least one test failed"),
        Condition("MIN_TESTS_EXECUTED", junit_ok and executed >= d.min_tests_executed,
                  f"fewer than {d.min_tests_executed} test(s) actually executed"),
        # `passed` is the orchestrator's verdict (exit codes, fresh JUnit, no failures, minimum executed)
        Condition("TESTS_PASSED", bool(result.get("passed")), "the tests did not pass"),
        Condition("PROCESS_TREE_TERMINATION_CONFIRMED", d.process_tree_confirmed,
                  "a tracked command is not proven to have stopped"),
    ]


def failure_reason_of_test_only(conditions: list[Condition]) -> str | None:
    """The failure_reason of the first unmet condition (None when all are met). An unmet process-tree condition has no
    reason code here: the caller fails closed instead (it can only happen if the B2 proof was bypassed)."""
    unmet = {c.id for c in conditions if c.met is not True}
    return next((reason for cid, reason in TEST_ONLY_FAILURE_REASONS if cid in unmet), None)
