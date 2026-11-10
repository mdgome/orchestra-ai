"""Definition of Done. Anything unclear is NOT a pass."""
from dataclasses import dataclass

from app.infra.workspace_snapshot import is_current_snapshot_version
from app.services.artifacts import has_critical


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
