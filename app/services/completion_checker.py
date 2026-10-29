"""Definition of Done. Anything unclear is NOT a pass."""
from dataclasses import dataclass

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


def check_definition_of_done(d: DoDInput) -> list[str]:
    """Returns the list of unmet conditions; empty means the task may complete."""
    problems: list[str] = []
    t = d.test_output
    if t is None:
        problems.append("tests were not run")
    else:
        result = t.get("test_result") or {}
        if not result.get("passed"):
            problems.append("required tests did not pass")
        if not result.get("junit_ok") or (result.get("executed", 0) - result.get("skipped", 0)) < 1:
            problems.append("test execution cannot be verified")
        if t.get("code_fingerprint") != d.current_code_fingerprint:
            problems.append("test result is not for the current code")
        if t.get("requirements_hash") != d.requirements_hash or t.get("test_policy_hash") != d.test_policy_hash:
            problems.append("test result was produced under different requirements or test policy")
    r = d.review
    if r is None:
        problems.append("code review missing")
    else:
        if r.get("verdict") != "PASS":
            problems.append(f"code review verdict is {r.get('verdict')}")
        if has_critical(r):
            problems.append("unresolved CRITICAL issue")
        if r.get("test_policy_concern"):
            problems.append("reviewer flagged weakened verification")
        if r.get("code_fingerprint") != d.current_code_fingerprint:
            problems.append("code review is not for the current code")
    if not d.design_review_passed:
        problems.append("design review PASS is not valid for the current basis")
    if d.pending_approvals:
        problems.append("unresolved Human Gate")
    return problems
