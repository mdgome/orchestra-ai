"""Artifact and completion-report views over what is ALREADY stored (task steps, executions, approvals, transitions).

Nothing here calls an LLM, runs a command, or writes. Every answer is derived deterministically from stored facts; a fact
that was never stored (the Definition-of-Done detail of a past completion, a past workspace state) is reported as
UNKNOWN / UNVERIFIED, never guessed. Free text and JSON go through the one recursive sanitiser before they leave.

The service returns a PAYLOAD (plain typed dicts); app/schemas/reports.py validates it and enforces the size contract.
"""
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.db import Database
from app.infra.response_sanitizer import LOSSY, TRUNCATION_MARK, Sanitizer, SanitizeReason
from app.models.states import (STEP_CODE_REVIEW, STEP_DESIGN_REVIEW, STEP_DEVELOP, STEP_PLAN, STEP_TEST, StepState,
                               TaskState)
from app.models.tables import Execution, Task, TaskStep
from app.repositories.approval_repository import ExecutionRequestRepository
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.task_repository import TaskRepository
from app.services.completion_checker import DoDInput, evaluate_definition_of_done
from app.services.fingerprints import design_review_basis, profile_criteria_of
from app.services.runtime_status import RuntimeSnapshot, sweeper_status_dict
from app.validation import policies as P
from app.validation.exceptions import NotFound, ValidationFailed
from app.validation.validators import validate_artifact_kind

KINDS = (STEP_PLAN, STEP_DESIGN_REVIEW, STEP_DEVELOP, STEP_TEST, STEP_CODE_REVIEW)
OUTCOME_NOTE = {
    TaskState.COMPLETED: ("The orchestrator's completion conditions were met. This does NOT mean the result was accepted "
                          "by you, merged or deployed: review the changes yourself first."),
    TaskState.FAILED: "The task stopped without meeting the completion conditions; see failure_reason and next_actions.",
    TaskState.CANCELLED: "The task was cancelled; nothing was completed.",
}
HISTORICAL_NOTE = ("Only the stored COMPLETED transition is shown. The Definition-of-Done detail of that moment was not "
                   "stored, so it is UNKNOWN and is not reconstructed here.")
RE_EVALUATION_NOTE = ("The conditions evaluated now against the stored artifacts. This neither confirms nor cancels a past "
                      "COMPLETED, and it does not compare the live workspace.")
IN_PROGRESS = (TaskState.PLANNING, TaskState.DESIGN_REVIEWING, TaskState.DEVELOPING, TaskState.TESTING,
               TaskState.REVIEWING, TaskState.RETRYING)


class _Clean:
    """The Sanitizer of ONE response plus the record of every reason it raised while that response was built. The
    response's top-level `sanitization` is made from this record, so a truncated or redacted structured string and a
    SanitizedJson field are both reported there (plan B-2). Nothing here is shared between responses."""

    def __init__(self, sanitizer: Sanitizer):
        self._sanitizer = sanitizer
        self._reasons: dict[str, None] = {}                    # an ordered set: first-seen order, no duplicates

    def add(self, reasons) -> None:
        for reason in reasons:
            self._reasons[str(reason)] = None

    def text(self, value: str | None, *, max_chars: int | None = None) -> tuple[str, tuple[str, ...]]:
        out, reasons = self._sanitizer.text(value, max_chars=max_chars)
        self.add(reasons)
        return out, reasons

    def tail(self, value: str | None, max_chars: int) -> str:
        """A text whose END matters (a command's output): keeps the last `max_chars` characters, marks the cut."""
        value = value or ""
        if len(value) > max_chars:
            self.add((SanitizeReason.STRING_TRUNCATED.value,))
            value = TRUNCATION_MARK + value[-max_chars:]
        return self.text(value, max_chars=max_chars + len(TRUNCATION_MARK))[0]

    def json(self, value, *, max_bytes: int | None = None):
        cut = self._sanitizer.json(value, max_bytes=max_bytes)
        self.add(cut.reasons)
        return cut

    def sanitization(self) -> dict:
        """The response-level `sanitization` (the size-limit fields stay for `finalize` to merge into)."""
        reasons = list(self._reasons)
        return {"truncated": any(SanitizeReason(r) in LOSSY for r in reasons), "reasons": reasons,
                "omitted_items": 0, "omitted_sections": []}


def _s(sanitizer: _Clean, text: str | None, max_chars: int = 500) -> str:
    return sanitizer.text(text or "", max_chars=max_chars)[0]


def _issues(sanitizer: _Clean, output: dict | None) -> list[dict]:
    raw = (output or {}).get("issues") or []
    return [{"severity": str(i.get("severity", ""))[:16], "description": _s(sanitizer, i.get("description"))}
            for i in raw[:P.REPORT_ISSUES_MAX] if isinstance(i, dict)]


def _sj(sanitizer: _Clean, value, max_bytes: int | None = None) -> dict:
    cut = sanitizer.json(value, max_bytes=max_bytes)
    return {"value": cut.value, "truncated": cut.truncated, "reasons": list(cut.reasons),
            "omitted_paths": list(cut.omitted_paths)}


def _latest_succeeded(steps: list[TaskStep]) -> dict[str, TaskStep]:
    """Per kind, the most recently produced SUCCEEDED artifact (steps arrive ordered by created_at, id)."""
    out: dict[str, TaskStep] = {}
    for step in steps:
        if step.status == str(StepState.SUCCEEDED) and step.step_key in KINDS:
            out[step.step_key] = step
    return out


class _Validity:
    """Is a stored artifact valid for the CURRENT code, requirements and policy? Decided from stored hashes only.

    CURRENT   = all stored bases match and the artifact does not depend on workspace content.
    STALE     = a stored basis no longer matches (reasons say which).
    UNVERIFIED= every stored basis matches, but the artifact describes workspace content and the live workspace is not
                compared here, so it can never be called CURRENT.
    """

    def __init__(self, task: Task, steps: list[TaskStep]):
        self.task = task
        self.latest = _latest_succeeded(steps)
        latest_in_revision: dict[tuple[str, int], TaskStep] = {}
        latest_in_iteration: dict[tuple[str, int, int], TaskStep] = {}
        for step in steps:
            if step.status == str(StepState.SUCCEEDED):
                latest_in_revision[(step.step_key, step.plan_revision)] = step
                latest_in_iteration[(step.step_key, step.plan_revision, step.workflow_iteration)] = step
        self.in_revision, self.in_iteration = latest_in_revision, latest_in_iteration
        plan = latest_in_revision.get((STEP_PLAN, task.plan_revision))
        self.plan_hash = plan.output_hash if plan else ""
        self.basis = design_review_basis(task.requirements_hash, self.plan_hash, task.decisions_hash,
                                         profile_hash=task.profile_hash)

    def of(self, step: TaskStep) -> dict:
        if step.status != str(StepState.SUCCEEDED) or step.step_key not in KINDS:
            return {"state": "NOT_APPLICABLE", "reasons": [f"step status is {step.status}"]}
        t, out = self.task, step.output or {}
        reasons: list[str] = []
        if step.plan_revision != t.plan_revision:
            reasons.append(f"superseded by plan revision {t.plan_revision}")
        elif self.in_revision.get((step.step_key, step.plan_revision)) is not step and step.step_key in (STEP_PLAN, STEP_DESIGN_REVIEW):
            reasons.append("superseded by a later attempt in the same plan revision")
        if step.step_key == STEP_DESIGN_REVIEW and step.plan_revision == t.plan_revision:
            stored = out.get("basis") or {}
            for part in [k for k in self.basis if k != "basis_hash"]:
                if stored.get(part) != self.basis[part]:
                    reasons.append(f"the design review basis changed ({part})")
        if step.step_key in (STEP_DEVELOP, STEP_TEST, STEP_CODE_REVIEW):
            if step.workflow_iteration != t.workflow_iteration:
                reasons.append(f"superseded by workflow iteration {t.workflow_iteration}")
            elif self.in_iteration.get((step.step_key, step.plan_revision, step.workflow_iteration)) is not step:
                reasons.append("superseded by a later attempt in the same iteration")
        if step.step_key == STEP_DEVELOP and out.get("plan_hash") != self.plan_hash:
            reasons.append("implemented against a plan that is no longer the current one")
        if step.step_key == STEP_TEST:
            if out.get("requirements_hash") != t.requirements_hash:
                reasons.append("tests ran under different requirements")
            if out.get("test_policy_hash") != t.test_policy_hash:
                reasons.append("tests ran under a different test policy")
        if step.step_key == STEP_CODE_REVIEW:
            test = self.in_iteration.get((STEP_TEST, step.plan_revision, step.workflow_iteration))
            if test is not None and (test.output or {}).get("code_fingerprint") != out.get("code_fingerprint"):
                reasons.append("the review is not for the tested code")
        if reasons:
            return {"state": "STALE", "reasons": reasons}
        if step.step_key in (STEP_PLAN, STEP_DESIGN_REVIEW):
            return {"state": "CURRENT", "reasons": []}
        return {"state": "UNVERIFIED", "reasons": ["the live workspace is not compared with this artifact"]}


class ReportService:
    def __init__(self, db: Database, *, get_runtime: Callable[[], RuntimeSnapshot] | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.db, self._get_runtime, self._now = db, get_runtime, now

    # --- loading ---------------------------------------------------------------------------------------------
    async def _task(self, s, task_id: uuid.UUID) -> Task:
        task = await TaskRepository(s).get(task_id)
        if task is None:
            raise NotFound("task not found")
        return task

    def _summary(self, step: TaskStep, validity: _Validity, sanitizer: _Clean) -> dict:
        latest = validity.latest.get(step.step_key)
        return {"id": step.id, "kind": step.step_key, "plan_revision": step.plan_revision,
                "workflow_iteration": step.workflow_iteration, "step_attempt": step.step_attempt,
                "status": step.status, "is_latest_succeeded": latest is not None and latest.id == step.id,
                "validity": validity.of(step), "created_at": step.created_at, "finished_at": step.finished_at,
                "summary": _s(sanitizer, (step.output or {}).get("summary") or (step.output or {}).get("agent_summary"), 300)}

    # --- artifacts ------------------------------------------------------------------------------------------
    async def artifact_list(self, task_id: uuid.UUID, kind: str | None, limit: int, offset: int) -> dict:
        validate_artifact_kind(kind, KINDS)
        async with self.db.transaction() as s:
            task = await self._task(s, task_id)
            repo = ArtifactRepository(s)
            page, total = await repo.steps_page(task_id, (kind,) if kind else KINDS, limit, offset)
            every = await repo.all_steps(task_id)
            sanitizer = _Clean(Sanitizer(await repo.known_session_ids(task_id)))
        validity = _Validity(task, every)
        items = [self._summary(step, validity, sanitizer) for step in page]
        end = offset + len(items)
        return {"items": items, "total": total, "returned": len(items), "omitted_items": 0, "has_more": end < total,
                "size_limited": False, "next_offset": end if end < total else None, "limit": limit, "offset": offset,
                "sanitization": sanitizer.sanitization()}

    async def artifact_detail(self, task_id: uuid.UUID, artifact_id: uuid.UUID) -> dict:
        async with self.db.transaction() as s:
            task = await self._task(s, task_id)
            repo = ArtifactRepository(s)
            step = await repo.step_of_task(task_id, artifact_id)
            if step is None:
                raise NotFound("artifact not found")
            every = await repo.all_steps(task_id)
            sanitizer = _Clean(Sanitizer(await repo.known_session_ids(task_id)))
        content = _sj(sanitizer, step.output) if step.output is not None else None
        error, _ = sanitizer.text(step.error, max_chars=2000)
        return {**self._summary(step, _Validity(task, every), sanitizer), "content": content,
                "error": error if step.error else None, "sanitization": sanitizer.sanitization()}

    # --- report -----------------------------------------------------------------------------------------------
    async def report(self, task_id: uuid.UUID) -> dict:
        async with self.db.transaction() as s:
            task = await self._task(s, task_id)
            repo = ArtifactRepository(s)
            steps = await repo.all_steps(task_id)
            sanitizer = _Clean(Sanitizer(await repo.known_session_ids(task_id)))
            test_step = _latest_succeeded(steps).get(STEP_TEST)
            test_run = await repo.orchestrator_test_run(test_step.id) if test_step else None
            gates = await repo.gates(task_id)
            completed = await repo.last_transition_to(task_id, str(TaskState.COMPLETED))
            last = await repo.last_task_transition(task_id)
            open_request = await ExecutionRequestRepository(s).open_for_task(task_id)
        validity = _Validity(task, steps)
        latest = validity.latest
        pending = [g for g, d in gates if g.status == "PENDING"]
        payload = {
            "task": {"id": task.id, "project_id": task.project_id, "status": task.status,
                     "plan_revision": task.plan_revision, "workflow_iteration": task.workflow_iteration,
                     "replan_count": task.replan_count, "cli_invocations": task.cli_invocations,
                     "run_seconds": float(task.run_seconds), "failure_reason": task.failure_reason,
                     "created_at": task.created_at, "updated_at": task.updated_at},
            "outcome": task.status,
            "outcome_note": OUTCOME_NOTE.get(TaskState(task.status), "The task has not reached a final state."),
            "plan": self._plan(latest.get(STEP_PLAN), validity, sanitizer),
            "design_review": self._review(latest.get(STEP_DESIGN_REVIEW), validity, sanitizer),
            "implementation": self._implementation(latest.get(STEP_DEVELOP), validity, sanitizer),
            "tests": self._tests(task, latest.get(STEP_TEST), test_run, validity, sanitizer),
            "code_review": self._review(latest.get(STEP_CODE_REVIEW), validity, sanitizer),
            "iterations": self._iterations(steps, sanitizer),
            "human_gates": [self._gate(g, d, sanitizer) for g, d in gates[-P.REPORT_GATES_MAX:]],
            "historical_completion": self._historical(completed),
            "current_re_evaluation": self._re_evaluation(task, validity, len(pending)),
            "last_transition": self._transition(last, sanitizer),
            "next_actions": self._next_actions(task, pending, open_request is not None),
            "runtime": self._runtime(),
        }
        payload["task"]["failure_reason"] = _s(sanitizer, task.failure_reason, 64) if task.failure_reason else None
        payload["sanitization"] = sanitizer.sanitization()          # last: every section above has been built
        return payload

    def _plan(self, step, validity, sanitizer):
        if step is None:
            return None
        return {"artifact_id": step.id, "plan_revision": step.plan_revision, "validity": validity.of(step),
                "content": _sj(sanitizer, step.output)}

    def _review(self, step, validity, sanitizer):
        if step is None:
            return None
        out = step.output or {}
        return {"artifact_id": step.id, "plan_revision": step.plan_revision,
                "workflow_iteration": step.workflow_iteration, "validity": validity.of(step),
                "verdict": str(out.get("verdict", ""))[:32], "summary": _s(sanitizer, out.get("summary")),
                "issues": _issues(sanitizer, out)}

    def _implementation(self, step, validity, sanitizer):
        if step is None:
            return None
        out = step.output or {}
        return {"artifact_id": step.id, "plan_revision": step.plan_revision,
                "workflow_iteration": step.workflow_iteration, "validity": validity.of(step),
                "summary": _s(sanitizer, out.get("summary")),
                "files_changed": [_s(sanitizer, f, 300) for f in (out.get("files_changed") or [])[:P.SANITIZE_MAX_ITEMS]]}

    def _tests(self, task: Task, step, run: Execution | None, validity, sanitizer):
        """The Tester step's stored output and the orchestrator's own run, each only as far as it was RECORDED.

        The pass/fail aggregate is the orchestrator's authoritative result stored in the step output (`test_result`). The
        run itself is the LOCAL `Execution` row written with that step (`detail.orchestrator_test_run`); without it
        `orchestrator_run.recorded` is false and `run_by` is UNKNOWN. The command is not stored on either record: the
        task's test policy is shown as `policy_command` and becomes `command` only when the step recorded the hash of
        that very policy."""
        if step is None:
            return None
        out = step.output or {}
        result = out.get("test_result") or {}
        commands = (task.test_policy or {}).get("commands") or []
        policy_command = [_s(sanitizer, a, 300) for a in commands[0]] if commands else None
        bound = run is not None and bool(task.test_policy_hash) and out.get("test_policy_hash") == task.test_policy_hash
        detail = (run.detail if run is not None else None) or {}
        codes = result.get("exit_codes")
        recorded_run = {
            "recorded": run is not None, "run_by": "ORCHESTRATOR" if run is not None else "UNKNOWN",
            "outcome": run.outcome if run is not None else None, "exit_code": run.exit_code if run is not None else None,
            "exit_codes": ([c for c in codes if c is None or isinstance(c, int)]
                           if run is not None and isinstance(codes, list) else []),
            "finished_at": run.finished_at if run is not None else None,
            "reason": (_s(sanitizer, detail.get("reason")) or None) if run is not None else None,
            "tracking": str(detail["tracking"])[:64] if detail.get("tracking") else None,
            "output_tail": sanitizer.tail(run.stdout_tail, 2000) if run is not None and run.stdout_tail else None}
        return {"artifact_id": step.id, "plan_revision": step.plan_revision,
                "workflow_iteration": step.workflow_iteration, "validity": validity.of(step),
                "command": policy_command if bound else None, "policy_command": policy_command,
                "run_by": recorded_run["run_by"], "orchestrator_run": recorded_run,
                "agent_summary": _s(sanitizer, out.get("agent_summary")),
                "passed": result.get("passed"), "executed": result.get("executed"),
                "skipped": result.get("skipped"), "reason": _s(sanitizer, result.get("reason")) or None,
                "failures": [_s(sanitizer, f, 300) for f in (result.get("failures") or [])[:P.REPORT_ISSUES_MAX]]}

    def _iterations(self, steps: list[TaskStep], sanitizer: _Clean) -> list[dict]:
        rounds: dict[tuple[int, int], list[TaskStep]] = {}
        for step in steps:
            rounds.setdefault((step.plan_revision, step.workflow_iteration), []).append(step)
        out = []
        for (revision, iteration), group in sorted(rounds.items())[-P.REPORT_ITERATIONS_MAX:]:
            attempts: dict[str, int] = {}
            best: dict[str, TaskStep] = {}
            for step in group:
                attempts[step.step_key] = max(attempts.get(step.step_key, 0), step.step_attempt)
                if step.status == str(StepState.SUCCEEDED):
                    best[step.step_key] = step
            design, test, review = (best.get(k) for k in (STEP_DESIGN_REVIEW, STEP_TEST, STEP_CODE_REVIEW))
            result = ((test.output or {}).get("test_result") or {}) if test else {}
            out.append({"plan_revision": revision, "workflow_iteration": iteration, "step_attempts": attempts,
                        "design_review_verdict": (design.output or {}).get("verdict") if design else None,
                        "design_issues": _issues(sanitizer, design.output if design else None),
                        "test_passed": result.get("passed") if test else None,
                        "test_reason": (_s(sanitizer, result.get("reason")) or None) if test else None,
                        "code_review_verdict": (review.output or {}).get("verdict") if review else None,
                        "code_review_issues": _issues(sanitizer, review.output if review else None)})
        return out

    def _gate(self, approval, decision, sanitizer) -> dict:
        return {"id": approval.id, "type": approval.type, "status": approval.status,
                "checkpoint_step_key": approval.checkpoint_step_key, "created_at": approval.created_at,
                "decided_at": decision.decided_at if decision else None,
                "decision": decision.decision if decision else None,
                "selected_option": _s(sanitizer, decision.selected_option, 64) if decision and decision.selected_option else None,
                "rationale": _s(sanitizer, decision.rationale) if decision and decision.rationale else None,
                # the stored payload is the snapshot at the opening of the gate, never the options selectable now (design 7.5 item 2)
                "request_label": "SNAPSHOT_AT_OPENING",
                "request": _sj(sanitizer, {"target": approval.target, "request_payload": approval.request_payload},
                               max_bytes=8 * 1024)}

    def _historical(self, completed) -> dict:
        return {"recorded": completed is not None, "completed_at": completed.created_at if completed else None,
                "transition_reason": completed.reason if completed else None, "conditions": "UNKNOWN",
                "note": HISTORICAL_NOTE}

    def _re_evaluation(self, task: Task, validity: _Validity, pending: int) -> dict:
        latest = validity.latest
        plan, design = latest.get(STEP_PLAN), latest.get(STEP_DESIGN_REVIEW)
        design_ok = bool(design and design.plan_revision == task.plan_revision and (design.output or {}).get("verdict") == "PASS"
                         and (design.output or {}).get("basis", {}).get("basis_hash") == validity.basis["basis_hash"])
        test = validity.in_iteration.get((STEP_TEST, task.plan_revision, task.workflow_iteration))
        review = validity.in_iteration.get((STEP_CODE_REVIEW, task.plan_revision, task.workflow_iteration))
        fingerprint = (test.output or {}).get("code_fingerprint", "") if test else ""
        conditions = evaluate_definition_of_done(DoDInput(
            test_output=test.output if test else None, review=review.output if review else None,
            current_code_fingerprint=fingerprint, requirements_hash=task.requirements_hash,
            test_policy_hash=task.test_policy_hash, design_review_passed=design_ok, pending_approvals=pending,
            profile_hash=task.profile_hash, profile_criteria=profile_criteria_of(task)))
        return {"evaluated_at": self._now(),
                "evaluation_basis": {"plan_artifact_id": str(plan.id) if plan else None,
                                     "design_review_artifact_id": str(design.id) if design else None,
                                     "test_artifact_id": str(test.id) if test else None,
                                     "code_review_artifact_id": str(review.id) if review else None,
                                     "plan_revision": task.plan_revision, "workflow_iteration": task.workflow_iteration,
                                     "pending_approvals": pending},
                "all_met": all(c.met for c in conditions),
                "conditions": [{"id": c.id, "met": c.met, "message": c.message} for c in conditions],
                "note": RE_EVALUATION_NOTE}

    def _transition(self, row, sanitizer):
        if row is None:
            return None
        return {"from_state": row.from_state, "to_state": row.to_state, "reason": row.reason, "at": row.created_at,
                "event": _sj(sanitizer, row.event, max_bytes=8 * 1024) if row.event is not None else None}

    def _runtime(self) -> dict:
        """The runtime part (14.2): a plain dictionary that `RuntimeOut` validates in `finalize`. `warnings` and `sweeper` are
        always present (`[]` and the default object when the state is UNKNOWN); no cursor, no component list."""
        if self._get_runtime is None:
            return {"state": "UNKNOWN", "ready": None, "blockers": [], "warnings": [],
                    "sweeper": sweeper_status_dict(None, None), "background_degraded": False}
        snap = self._get_runtime()
        return {"state": "KNOWN", "ready": snap.ready, "blockers": list(snap.blockers), "warnings": list(snap.warnings),
                "sweeper": snap.sweeper, "background_degraded": snap.background_degraded}

    def _next_actions(self, task: Task, pending: list, has_open_request: bool) -> list[dict]:
        status = TaskState(task.status)
        runtime = self._runtime()
        if status is TaskState.PENDING and not has_open_request:
            return [{"code": "REQUEST_EXECUTION", "text": f"Request execution: POST /tasks/{task.id}/execution-requests"}]
        if has_open_request and runtime["state"] == "KNOWN" and not runtime["ready"]:
            return [{"code": "RUNNER_NOT_READY",
                     "text": "An execution request is waiting but no Runner is ready (" + ", ".join(runtime["blockers"]) +
                             "). It stays PENDING until a Runner starts; check GET /health and the project diagnostics."}]
        if status is TaskState.PENDING:
            return [{"code": "WAIT_FOR_RUNNER", "text": "The execution request is stored and waits for the Runner."}]
        if status in IN_PROGRESS:
            return [{"code": "WAIT", "text": "The task is running; poll GET /tasks/{id} or follow the event stream."}]
        if status is TaskState.WAITING_APPROVAL:
            return [{"code": "DECIDE_GATE", "text": f"Decide the open Human Gate: POST /approvals/{g.id}/decisions"}
                    for g in pending] or [{"code": "WAIT", "text": "A Human Gate is being processed."}]
        if status is TaskState.COMPLETED:
            return [{"code": "REVIEW_CHANGES", "text": "Review the changes yourself, then accept, merge or deploy them. "
                                                         "COMPLETED is not acceptance."}]
        if status is TaskState.FAILED:
            return [{"code": "INSPECT_FAILURE", "text": "Read the iteration issues and failure_reason, then create a follow-up "
                                                        "task with adjusted requirements."}]
        return [{"code": "CREATE_NEW_TASK", "text": "The task was cancelled. Create a new task if the work is still needed."}]
