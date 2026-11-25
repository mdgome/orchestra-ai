"""Response shapes of the artifact / report views, and the final size contract.

Order for every response (plan B-2c): the service builds a payload of already sanitised, correctly typed values ->
strict validation against the model -> serialised size is measured -> over the limit: ONLY the fields this schema
reserves for it (`sanitization`, `omitted_items`, `omitted_sections`, `size_limited`, `SanitizedJson.reasons`) are used to
cut the payload again -> strict validation again -> size measured again, at most RESPONSE_SHRINK_MAX_ROUNDS times. Still
too big, or a cut broke the schema: a bounded fallback response; if even that does not fit, an explicit error. A response
is never sent without having passed both checks.
"""
import copy
import uuid
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, Field, JsonValue, ValidationError

from app.infra.response_sanitizer import Sanitizer, SanitizeReason
from app.validation import policies as P
from app.validation.exceptions import ResponseSizeUnsatisfiable


class _Out(BaseModel):
    pass


class SanitizedJsonOut(_Out):
    value: JsonValue
    truncated: bool
    reasons: list[str]
    omitted_paths: list[str]


class SanitizationOut(_Out):
    """What the whole response lost to sanitising or to its size limit. `reasons` are SanitizeReason codes."""
    truncated: bool = False
    reasons: list[str] = []
    omitted_items: int = 0
    omitted_sections: list[str] = []


class ValidityOut(_Out):
    state: str                   # CURRENT | STALE | UNVERIFIED | NOT_APPLICABLE
    reasons: list[str]


class ArtifactSummaryOut(_Out):
    id: uuid.UUID
    kind: str
    plan_revision: int
    workflow_iteration: int
    step_attempt: int
    status: str
    is_latest_succeeded: bool    # the most recently produced SUCCEEDED artifact of this kind (not "currently valid")
    validity: ValidityOut        # whether it is valid for the CURRENT code, requirements and policy
    created_at: datetime
    finished_at: datetime | None = None
    summary: str = ""


class ArtifactOut(ArtifactSummaryOut):
    content: SanitizedJsonOut | None = None
    error: str | None = None
    sanitization: SanitizationOut


class ArtifactListOut(_Out):
    items: list[ArtifactSummaryOut]
    total: int                   # every matching artifact in the database, whatever this page returned
    returned: int
    omitted_items: int           # left out of THIS page by the size limit; re-fetchable from next_offset
    has_more: bool
    size_limited: bool
    next_offset: int | None
    limit: int
    offset: int
    sanitization: SanitizationOut


# --- report -----------------------------------------------------------------------------------------------------
class IssueOut(_Out):
    severity: str
    description: str


class TaskSummaryOut(_Out):
    id: uuid.UUID
    project_id: uuid.UUID | None = None
    status: str
    plan_revision: int
    workflow_iteration: int
    replan_count: int
    cli_invocations: int
    run_seconds: float
    failure_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class PlanOut(_Out):
    artifact_id: uuid.UUID
    plan_revision: int
    validity: ValidityOut
    content: SanitizedJsonOut


class ImplementationOut(_Out):
    artifact_id: uuid.UUID
    plan_revision: int
    workflow_iteration: int
    validity: ValidityOut
    summary: str
    files_changed: list[str]


class TestRunOut(_Out):
    artifact_id: uuid.UUID
    plan_revision: int
    workflow_iteration: int
    validity: ValidityOut
    command: list[str] | None = None        # what the Orchestrator ran (from the task's stored test policy)
    run_by: str                              # ORCHESTRATOR: the Orchestrator ran it itself; the agent's text proves nothing
    passed: bool | None = None
    executed: int | None = None
    skipped: int | None = None
    reason: str | None = None
    failures: list[str] = []


class ReviewOut(_Out):
    artifact_id: uuid.UUID
    plan_revision: int
    workflow_iteration: int
    validity: ValidityOut
    verdict: str
    summary: str
    issues: list[IssueOut]


class IterationOut(_Out):
    plan_revision: int
    workflow_iteration: int
    step_attempts: dict[str, int]
    design_review_verdict: str | None = None
    design_issues: list[IssueOut] = []
    test_passed: bool | None = None
    test_reason: str | None = None
    code_review_verdict: str | None = None
    code_review_issues: list[IssueOut] = []


class GateOut(_Out):
    id: uuid.UUID
    type: str
    status: str
    checkpoint_step_key: str
    created_at: datetime
    decided_at: datetime | None = None
    decision: str | None = None
    selected_option: str | None = None
    rationale: str | None = None
    request_label: str = "SNAPSHOT_AT_OPENING"     # `request` is the stored payload as it was when the gate opened, never the options selectable now
    request: SanitizedJsonOut | None = None


class ConditionOut(_Out):
    id: str
    met: bool | None             # null = not evaluated because a prerequisite is missing
    message: str


class HistoricalCompletionOut(_Out):
    """What the stored records say about a COMPLETED transition at the time. The Definition-of-Done detail of that
    moment was never stored, so `conditions` is UNKNOWN: nothing is reconstructed."""
    recorded: bool
    completed_at: datetime | None = None
    transition_reason: str | None = None
    conditions: str
    note: str


class ReEvaluationOut(_Out):
    """The conditions evaluated NOW against the stored artifacts. It neither confirms nor cancels a past COMPLETED."""
    evaluated_at: datetime
    evaluation_basis: dict[str, str | int | None]
    all_met: bool
    conditions: list[ConditionOut]
    note: str


class LastTransitionOut(_Out):
    from_state: str | None = None
    to_state: str
    reason: str | None = None
    at: datetime
    event: SanitizedJsonOut | None = None


class NextActionOut(_Out):
    code: str
    text: str


class SweeperStatusOut(_Out):
    """The abbreviated sweeper status (14.2): no cursor position, no position kind. Its key set is also the `/health`
    `runner.sweeper` object; the default is the 'nothing stalled' object."""
    stalled: bool = False
    consecutive_failures: int = 0
    last_failure_code: str | None = None
    last_failure_at: datetime | None = None
    next_retry_at: datetime | None = None
    config_invalid_active: bool = False
    clearance_pending: bool = False


class RuntimeOut(_Out):
    state: str                   # KNOWN | UNKNOWN
    ready: bool | None = None
    blockers: list[str] = []
    warnings: list[str] = Field(default_factory=list)
    sweeper: SweeperStatusOut = Field(default_factory=SweeperStatusOut)
    background_degraded: bool = False


class ReportOut(_Out):
    task: TaskSummaryOut
    outcome: str
    outcome_note: str
    plan: PlanOut | None = None
    design_review: ReviewOut | None = None
    implementation: ImplementationOut | None = None
    tests: TestRunOut | None = None
    code_review: ReviewOut | None = None
    iterations: list[IterationOut] = []
    human_gates: list[GateOut] = []
    historical_completion: HistoricalCompletionOut
    current_re_evaluation: ReEvaluationOut
    last_transition: LastTransitionOut | None = None
    next_actions: list[NextActionOut]
    runtime: RuntimeOut
    sanitization: SanitizationOut


# --- the size contract ------------------------------------------------------------------------------------------------
Step = Callable[[dict], bool]        # edits the payload in place; True = it removed something


def _bytes(obj: BaseModel) -> int:
    return len(obj.model_dump_json().encode("utf-8"))


def _note(payload: dict, *reasons: SanitizeReason, section: str | None = None, items: int = 0) -> None:
    s = payload["sanitization"]
    s["truncated"] = True
    for reason in reasons:
        if reason.value not in s["reasons"]:
            s["reasons"].append(reason.value)
    s["omitted_items"] += items
    if section and section not in s["omitted_sections"]:
        s["omitted_sections"].append(section)


def finalize(model: type[BaseModel], payload: dict, max_bytes: int, steps: list[Step],
             fallback: Callable[[dict], dict]) -> BaseModel:
    """Strict validation -> size -> (cut only through reserved fields -> strict validation -> size) x N -> fallback."""
    payload = copy.deepcopy(payload)
    obj = model.model_validate(payload, strict=True)       # a payload the service built wrongly is a bug: it raises
    for _ in range(P.RESPONSE_SHRINK_MAX_ROUNDS):
        if _bytes(obj) <= max_bytes:
            return obj
        if not any(step(payload) for step in steps):
            break
        try:
            obj = model.model_validate(payload, strict=True)
        except ValidationError:
            break                                             # a cut broke the schema: stop cutting, use the fallback
    else:
        if _bytes(obj) <= max_bytes:
            return obj
    try:
        obj = model.model_validate(fallback(payload), strict=True)
    except ValidationError as exc:
        raise ResponseSizeUnsatisfiable("the bounded fallback response is not valid", {"errors": len(exc.errors())}) from exc
    if _bytes(obj) > max_bytes:
        raise ResponseSizeUnsatisfiable("the bounded fallback response still exceeds its size limit",
                                        {"limit_bytes": max_bytes})
    return obj


def _shrink_sanitized(holder: dict, key: str, payload: dict, section: str) -> bool:
    """Halves the byte budget of one SanitizedJson field. Uses only the reserved fields of that field."""
    node = holder.get(key)
    if not node or node["value"] is None:
        return False
    current = len(str(node["value"]).encode("utf-8"))
    if current < 512:
        return False
    cut = Sanitizer().json(node["value"], max_bytes=max(256, current // 2))
    node["value"] = cut.value
    node["truncated"] = True
    for reason in (*cut.reasons, SanitizeReason.SIZE_LIMIT.value):
        if reason not in node["reasons"]:
            node["reasons"].append(reason)
    node["omitted_paths"] = list({*node["omitted_paths"], *cut.omitted_paths})[:P.SANITIZE_MAX_OMITTED_PATHS]
    _note(payload, SanitizeReason.SIZE_LIMIT, section=section)
    return True


# artifact list ---------------------------------------------------------------------------------------------------------
def _drop_list_tail(payload: dict) -> bool:
    items = payload["items"]
    if len(items) <= 1:
        return False                                          # the first item always stays (validate_response_limits)
    drop = max(1, len(items) // 2)
    del items[-drop:]
    payload["omitted_items"] += drop
    payload["returned"] = len(items)
    payload["size_limited"] = True
    payload["next_offset"] = payload["offset"] + payload["returned"]
    payload["has_more"] = payload["next_offset"] < payload["total"]
    _note(payload, SanitizeReason.SIZE_LIMIT, items=drop)
    return True


def _list_fallback(payload: dict) -> dict:
    out = {**payload, "items": [], "returned": 0, "omitted_items": payload["omitted_items"] + len(payload["items"]),
           "size_limited": True, "next_offset": payload["offset"],
           "has_more": payload["offset"] < payload["total"]}
    _note(out, SanitizeReason.SIZE_LIMIT, items=len(payload["items"]))
    return out


def render_artifact_list(payload: dict) -> ArtifactListOut:
    return finalize(ArtifactListOut, payload, P.ARTIFACT_LIST_RESPONSE_MAX_BYTES, [_drop_list_tail], _list_fallback)


# artifact detail -------------------------------------------------------------------------------------------------------
def _shrink_content(payload: dict) -> bool:
    return _shrink_sanitized(payload, "content", payload, "content")


def _shorten_error(payload: dict) -> bool:
    error = payload.get("error")
    if not error or len(error) <= 500:
        return False
    payload["error"] = error[:500]
    _note(payload, SanitizeReason.STRING_TRUNCATED, section="error")
    return True


def _detail_fallback(payload: dict) -> dict:
    out = {**payload, "content": None, "error": None, "summary": ""}
    _note(out, SanitizeReason.SIZE_LIMIT, section="content")
    return out


def render_artifact(payload: dict) -> ArtifactOut:
    return finalize(ArtifactOut, payload, P.ARTIFACT_ITEM_MAX_BYTES, [_shrink_content, _shorten_error], _detail_fallback)


# report ------------------------------------------------------------------------------------------------------------------
def _shrink_plan(payload: dict) -> bool:
    plan = payload.get("plan")
    return bool(plan) and _shrink_sanitized(plan, "content", payload, "plan.content")


def _drop_gate_requests(payload: dict) -> bool:
    dropped = False
    for gate in payload["human_gates"]:
        if gate.get("request") is not None:
            gate["request"] = None
            dropped = True
    if dropped:
        _note(payload, SanitizeReason.SIZE_LIMIT, section="human_gates[].request")
    return dropped


def _drop_old_iterations(payload: dict) -> bool:
    items = payload["iterations"]
    if len(items) <= 1:
        return False
    drop = max(1, len(items) // 2)
    del items[:drop]                                           # the oldest rounds go first
    _note(payload, SanitizeReason.SIZE_LIMIT, section=f"iterations[:{drop}]", items=drop)
    return True


def _drop_old_gates(payload: dict) -> bool:
    items = payload["human_gates"]
    if not items:
        return False
    drop = max(1, len(items) // 2)
    del items[:drop]
    _note(payload, SanitizeReason.SIZE_LIMIT, section=f"human_gates[:{drop}]", items=drop)
    return True


def _report_fallback(payload: dict) -> dict:
    """The core of a report: where the task stands, why, and what to do next. Never the bulky sections."""
    out = {**payload, "plan": None, "design_review": None, "implementation": None, "tests": None,
           "code_review": None, "iterations": [], "human_gates": [], "last_transition": None}
    _note(out, SanitizeReason.SIZE_LIMIT, section="all optional sections")
    return out


def render_report(payload: dict) -> ReportOut:
    return finalize(ReportOut, payload, P.REPORT_RESPONSE_MAX_BYTES,
                    [_shrink_plan, _drop_gate_requests, _drop_old_iterations, _drop_old_gates], _report_fallback)
