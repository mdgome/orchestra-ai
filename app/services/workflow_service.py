"""Deterministic Workflow Controller (no LLM): PLAN -> DESIGN_REVIEW -> DEVELOP -> TEST -> CODE_REVIEW.

Rules enforced here
- Design Review PASS is bound to design_review_basis; Development refuses to start on any other basis.
- SUCCEEDED steps are reused only when input_hash and output_hash still match (WorkspaceSnapshot is only
  used to decide whether an *interrupted* step may be re-run).
- Tester results are reused only for identical requirements / code / test-policy fingerprints.
- Technical retries (step_attempt), improvement iterations (workflow_iteration) and replans are separate counters.
- Every state change is validated, recorded append-only, and made together with the step result.
"""
import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.infra.event_bus import EventBus
from app.infra.workspace_guard import WorkspaceGuard
from app.infra.workspace_snapshot import diff_snapshots, take_snapshot
from app.models.states import (
    STEP_CODE_REVIEW, STEP_DESIGN_REVIEW, STEP_DEVELOP, STEP_PLAN, STEP_ROLE, STEP_TASK_STATE, STEP_TEST,
    TERMINAL_TASK_STATES, ApprovalType, CliOutcome, StepState, TaskState,
)
from app.models.tables import Approval, Execution, Task, TaskStep
from app.providers.permissions import authorize_one_off, is_test_path, permissions_for
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.task_repository import TaskRepository
from app.services import artifacts as A
from app.services import context_service as C
from app.services import prompts as PR
from app.services.completion_checker import DoDInput, check_definition_of_done
from app.services.fingerprints import design_review_basis
from app.services.hashing import stable_hash
from app.services.human_gate_service import HumanGateService
from app.services.lease_service import Lease, LeaseService
from app.services.session_service import SessionService
from app.services.state_service import change_step_state, change_task_state
from app.services.step_runner import (
    LimitExceeded, StepExecution, StepHandle, StepRunner, TaskCancelled, TerminationUnconfirmed,
)
from app.services.test_policy_service import TestPolicyService, compute_test_signals
from app.validation.exceptions import AppError, FailClosed, LeaseLost

log = logging.getLogger(__name__)
_EXTERNAL_SIDE_EFFECT = re.compile(r"\b(curl|wget|git\s+push|npm\s+publish|twine|ssh|scp)\b|https?://", re.I)


class RetryableStepFailure(Exception):
    """Technical failure of one step attempt (counts against max_step_attempts)."""


class PolicyViolation(AppError):
    status_code = 409
    code = "POLICY_VIOLATION"

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass
class GateSpec:
    type: ApprovalType
    payload: dict
    target: dict
    scope: str
    checkpoint_step_key: str
    execution_id: uuid.UUID | None = None
    command_fingerprint: str | None = None


@dataclass
class StepDecision:
    output: dict
    mutate: Callable[[AsyncSession, Task, TaskStep], Awaitable[None]] | None = None
    gate: GateSpec | None = None


@dataclass
class RunContext:
    lease: Lease
    workspace: Path
    resume_approval: Approval | None = None
    one_off_used: bool = False
    parked: bool = False       # a gate was opened and the lease released: stop driving


class WorkflowService:
    def __init__(self, db: Database, leases: LeaseService, steps: StepRunner, gates: HumanGateService,
                 test_policy: TestPolicyService, guard: WorkspaceGuard, sessions: SessionService,
                 events: EventBus | None = None):
        self.db, self.leases, self.steps, self.gates = db, leases, steps, gates
        self.tests, self.guard, self.sessions = test_policy, guard, sessions
        self.events = events or steps.events

    # ================================================================================================
    # driver
    # ================================================================================================
    async def run_task(self, lease: Lease, task_id: uuid.UUID, *, resume_approval_id: uuid.UUID | None = None) -> str:
        task = await self._get_task(task_id)
        workspace = self.guard.resolve_inside(task.workspace_path, must_exist=True)
        ctx = RunContext(lease, workspace)
        try:
            if resume_approval_id is not None:
                await self._resume_from_approval(ctx, task_id, resume_approval_id)
            while not ctx.parked:
                task = await self._get_task(task_id)
                state = TaskState(task.status)
                if state in TERMINAL_TASK_STATES:
                    await self._finish_run(ctx, task, "terminal")
                    return str(state)
                if state is TaskState.WAITING_APPROVAL:
                    return str(state)
                await self._handlers[state](self, ctx, task)
            return str(TaskState.WAITING_APPROVAL)
        except LeaseLost:
            log.warning("lease lost for task %s; stopping without recording results", task_id)
            return "LEASE_LOST"
        except TaskCancelled:
            await self._guarded(self._finalize_cancel(ctx, task_id))
        except (LimitExceeded, PolicyViolation) as exc:
            await self._guarded(self._fail(ctx, task_id, exc.reason, str(exc)))
        except TerminationUnconfirmed as exc:
            await self._guarded(self._park_for_recovery(ctx, task_id, f"CLI termination not confirmed: {exc.message}"))
        except FailClosed as exc:
            await self._guarded(self._fail(ctx, task_id, "FAIL_CLOSED", exc.message))
        return str(TaskState((await self._get_task(task_id)).status))

    @staticmethod
    async def _guarded(coro) -> None:
        try:
            await coro
        except LeaseLost:
            log.warning("lease lost while finishing a task; nothing was recorded")

    async def _get_task(self, task_id: uuid.UUID) -> Task:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).get(task_id)
        if task is None:
            raise LookupError("task not found")
        return task

    # ================================================================================================
    # terminal paths
    # ================================================================================================
    async def _finish_run(self, ctx: RunContext, task: Task, reason: str) -> None:
        async with self.db.transaction() as s:
            await self.sessions.close_all_for_task(s, task.id)
            await self.leases.release(ctx.lease, reason, session=s)
        self.events.publish(task.id, {"event": "task.finished", "status": task.status})

    async def _fail(self, ctx: RunContext, task_id: uuid.UUID, reason: str, detail: str = "") -> None:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, ctx.lease)
            if TaskState(task.status) not in TERMINAL_TASK_STATES:
                await change_task_state(s, task, TaskState.FAILED, reason=reason, failure_reason=reason,
                                        event={"detail": detail[:500]})
            for step in await TaskRepository(s).steps_for_task(task_id):
                if StepState(step.status) is StepState.RUNNING:
                    await change_step_state(s, step, StepState.FAILED, error=reason)
            await self.sessions.close_all_for_task(s, task_id)
            await self.leases.release(ctx.lease, f"failed:{reason}", session=s)
        self.events.publish(task_id, {"event": "task.finished", "status": "FAILED", "reason": reason})

    async def _finalize_cancel(self, ctx: RunContext, task_id: uuid.UUID) -> None:
        """Cancellation *request* != termination: CANCELLED only once every CLI process is proven gone."""
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, ctx.lease)
            open_exec = [e for e in await TaskRepository(s).unterminated_executions(task_id)
                         if e.role in STEP_ROLE.values()]
            if open_exec:
                step = next((st for st in reversed(await TaskRepository(s).steps_for_task(task_id))
                             if StepState(st.status) is StepState.RUNNING), None)
                await self.gates.open_recovery(
                    s, task, step, reason="cancellation requested but CLI termination is unconfirmed",
                    checkpoint_step_key=STEP_PLAN, detail={"unterminated_executions": len(open_exec)})
                ctx.parked = True
                return
            for step in await TaskRepository(s).steps_for_task(task_id):
                if StepState(step.status) is StepState.RUNNING:
                    await change_step_state(s, step, StepState.CANCELLED, error="cancelled")
            if TaskState(task.status) not in TERMINAL_TASK_STATES:
                await change_task_state(s, task, TaskState.CANCELLED, reason="cancelled")
            await self.sessions.close_all_for_task(s, task_id)
            await self.leases.release(ctx.lease, "cancelled", session=s)
        self.events.publish(task_id, {"event": "task.finished", "status": "CANCELLED"})

    async def _park_for_recovery(self, ctx: RunContext, task_id: uuid.UUID, reason: str) -> None:
        """Task=WAITING_APPROVAL + RECOVERY approval + Step=NEEDS_MANUAL_RECOVERY in one transaction.
        The run/lease is deliberately NOT released: its processes are not proven stopped."""
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, ctx.lease)
            step = next((st for st in reversed(await TaskRepository(s).steps_for_task(task_id))
                         if StepState(st.status) in (StepState.RUNNING, StepState.INTERRUPTED)), None)
            key = step.step_key if step else STEP_PLAN
            await self.gates.open_recovery(s, task, step, reason=reason, checkpoint_step_key=key)
        ctx.parked = True

    async def _enter_gate(self, ctx: RunContext, s: AsyncSession, task: Task, step: TaskStep, gate: GateSpec) -> None:
        """Called inside StepRunner.complete: open the approval, then return the lease (no Heartbeat while waiting)."""
        await self.gates.open_gate(
            s, task, step, type_=gate.type, checkpoint_step_key=gate.checkpoint_step_key,
            request_payload=gate.payload, target=gate.target, scope=gate.scope, execution_id=gate.execution_id,
            command_fingerprint=gate.command_fingerprint)
        await self.leases.release(ctx.lease, "WAITING_APPROVAL", session=s)
        ctx.parked = True

    # ================================================================================================
    # resume
    # ================================================================================================
    async def _resume_from_approval(self, ctx: RunContext, task_id: uuid.UUID, approval_id: uuid.UUID) -> None:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, ctx.lease)
            repo = ApprovalRepository(s)
            approval = await repo.get(approval_id)
            decision = await repo.get_decision(approval_id) if approval else None
            if approval is None or decision is None or approval.task_id != task_id:
                raise FailClosed("resume request has no decided approval")
            if TaskState(task.status) is not TaskState.WAITING_APPROVAL:
                return                                   # already resumed by an earlier consumption
            if approval.type == str(ApprovalType.RECOVERY):
                problem = await self._recovery_unsafe(s, task, approval, decision.selected_option, ctx.workspace)
                if problem:
                    await self.gates.open_recovery(s, task, None, reason=problem,
                                                   checkpoint_step_key=approval.checkpoint_step_key)
                    await self.leases.release(ctx.lease, "WAITING_APPROVAL", session=s)
                    ctx.parked = True
                    return
            stage = STEP_TASK_STATE[approval.checkpoint_step_key]
            if approval.type == str(ApprovalType.DESIGN) and approval.checkpoint_step_key == STEP_PLAN:
                await s.execute(update(Task).where(Task.id == task.id).values(plan_revision=Task.plan_revision + 1))
            if approval.type == str(ApprovalType.PERMISSION):
                ctx.resume_approval = approval
            await s.execute(update(Task).where(Task.id == task.id).values(resume_step_key=None))
            await change_task_state(s, task, stage, reason="resumed_after_approval",
                                    event={"approval_id": str(approval.id), "type": approval.type})

    async def _recovery_unsafe(self, s: AsyncSession, task: Task, approval: Approval, option: str | None,
                               workspace: Path) -> str | None:
        """Execution-safety re-verification. A RECOVERY approval never waives these checks."""
        if not workspace.exists():
            return "workspace no longer exists"
        if [e for e in await TaskRepository(s).unterminated_executions(task.id) if e.role in STEP_ROLE.values()]:
            return "a CLI execution is still not proven terminated"
        if option == "retry":
            step_id = approval.target.get("step_id")
            step = await TaskRepository(s).get_step(uuid.UUID(step_id)) if step_id else None
            if step is not None and step.snapshot_before is not None:
                now = await asyncio.to_thread(take_snapshot, workspace)
                if diff_snapshots(step.snapshot_before, now).changed:
                    return "retry requested but the workspace differs from the step's starting snapshot"
        return None

    # ================================================================================================
    # generic agent step
    # ================================================================================================
    async def _agent_step(self, ctx: RunContext, task: Task, *, step_key: str, input_hash: str, prompt: str,
                          schema: dict, mutating: bool, allow_resume: bool,
                          evaluate: Callable[[StepHandle, StepExecution, dict], Awaitable[StepDecision]]) -> None:
        role = STEP_ROLE[step_key]
        if await self._interrupted_needs_human(ctx, task, step_key, mutating):
            return
        while True:
            handle = await self.steps.begin(ctx.lease, task.id, step_key, role, input_hash, ctx.workspace)
            one_off, before_spawn = self._one_off_for(ctx, role, step_key)
            try:
                ex = await self.steps.execute(ctx.lease, handle, prompt=prompt, schema=schema, workspace=ctx.workspace,
                                              allow_resume=allow_resume, one_off_tool=one_off, before_spawn=before_spawn)
            except TaskCancelled:
                await self._mark_step(ctx, handle, StepState.CANCELLED, "cancelled")
                raise
            result = ex.result
            if result.outcome is CliOutcome.CANCELLED:
                await self._mark_step(ctx, handle, StepState.CANCELLED, "cancelled")
                raise TaskCancelled("cancelled")
            if result.outcome is CliOutcome.PERMISSION_REQUESTED:
                await self._permission_gate(ctx, task, handle, ex, role, step_key)
                return
            if result.outcome is not CliOutcome.SUCCESS:
                # TIMEOUT / IDLE / ERROR / SESSION_MISSING: never reinterpreted as a permission request
                await self._mark_step(ctx, handle, StepState.FAILED, f"cli {result.outcome}")
                continue
            try:
                structured = result.structured or self._json_or_none(result.text)
                if structured is None:
                    raise RetryableStepFailure("agent returned no structured output")
                decision = await evaluate(handle, ex, structured)
            except RetryableStepFailure as exc:
                await self._mark_step(ctx, handle, StepState.FAILED, str(exc))
                continue
            except PolicyViolation as exc:
                await self._mark_step(ctx, handle, StepState.FAILED, exc.reason)
                raise
            if decision.gate is not None:
                gate = decision.gate

                async def mutate(s, t, st, _g=gate):
                    await self._enter_gate(ctx, s, t, st, _g)
                await self.steps.complete(ctx.lease, handle, StepState.WAITING_APPROVAL, output=decision.output,
                                          snapshot_after=ex.snapshot_after, mutate=mutate)
                return
            await self.steps.complete(ctx.lease, handle, StepState.SUCCEEDED, output=decision.output,
                                      snapshot_after=ex.snapshot_after, mutate=decision.mutate)
            return

    @staticmethod
    def _json_or_none(text: str):
        import json
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except (TypeError, ValueError):
            return None

    async def _mark_step(self, ctx: RunContext, handle: StepHandle, status: StepState, error: str) -> None:
        await self.steps.complete(ctx.lease, handle, status, error=error)

    # --- permission (one-off, execution+command bound) -----------------------------------------------
    @staticmethod
    def _pattern_for(request: dict) -> str | None:
        tool, tin = request.get("tool"), request.get("tool_input") or {}
        if tool == "Bash" and isinstance(tin.get("command"), str) and tin["command"].strip():
            return f"Bash({tin['command'].strip()})"
        if tool in ("Edit", "Write") and isinstance(tin.get("file_path"), str):
            return f"{tool}({tin['file_path']})"
        return None

    async def _permission_gate(self, ctx: RunContext, task: Task, handle: StepHandle, ex: StepExecution,
                               role: str, step_key: str) -> None:
        request = ex.result.permission_requests[0]
        pattern = self._pattern_for(request)
        enforceable = False
        if pattern:
            try:
                authorize_one_off(role, pattern)
                enforceable = True
            except FailClosed:
                enforceable = False
        gate = GateSpec(
            type=ApprovalType.PERMISSION, scope="COMMAND", checkpoint_step_key=step_key,
            payload={"problem": f"{role} requested permission for {request.get('tool')}",
                     "options": [{"id": "allow_once", "title": "Allow this exact command once"}],
                     "request": {"tool": request.get("tool"), "input": request.get("input")}},
            target={"tool": request.get("tool"), "pattern": pattern, "step_key": step_key, "role": role,
                    "enforceable": enforceable},
            execution_id=ex.execution_id, command_fingerprint=stable_hash(pattern) if pattern else None)

        async def mutate(s, t, st):
            await self._enter_gate(ctx, s, t, st, gate)
        await self.steps.complete(ctx.lease, handle, StepState.WAITING_APPROVAL, mutate=mutate,
                                  snapshot_after=ex.snapshot_after)

    def _one_off_for(self, ctx: RunContext, role: str, step_key: str):
        approval = ctx.resume_approval
        if approval is None or ctx.one_off_used or approval.target.get("step_key") != step_key:
            return None, None
        pattern = approval.target.get("pattern")
        approval_id = approval.id

        async def before_spawn(s: AsyncSession, task: Task) -> None:
            repo = ApprovalRepository(s)
            row = await repo.lock(approval_id)
            decision = await repo.get_decision(approval_id)
            execution = await TaskRepository(s).get_execution(row.execution_id) if row.execution_id else None
            ok = (row is not None and decision is not None and decision.decision == "APPROVED"
                  and pattern and row.command_fingerprint == stable_hash(pattern) and row.used_at is None
                  and row.valid_until > self.leases._now() and execution is not None and execution.task_id == task.id
                  and row.target.get("enforceable") is True and row.target.get("step_key") == step_key)
            if not ok or not await repo.consume_permission(approval_id):
                raise FailClosed("permission approval does not match this command or was already used")
            ctx.one_off_used = True
        return pattern, before_spawn

    # --- interrupted-step safety ------------------------------------------------------------------------
    async def _interrupted_needs_human(self, ctx: RunContext, task: Task, step_key: str, mutating: bool) -> bool:
        async with self.db.transaction() as s:
            repo = TaskRepository(s)
            latest = await repo.latest_step(task.id, step_key, plan_revision=task.plan_revision,
                                            workflow_iteration=task.workflow_iteration)
            if latest is None or StepState(latest.status) not in (StepState.RUNNING, StepState.INTERRUPTED):
                return False
            await repo.lock(task.id)
            await self.leases.verify(s, ctx.lease)
            if StepState(latest.status) is StepState.RUNNING:
                await change_step_state(s, latest, StepState.INTERRUPTED, error="interrupted by restart")
            if not mutating:
                return False                                    # read-only roles can always be re-run
            problem = None
            if latest.snapshot_before is None:
                problem = "interrupted step has no starting snapshot"
            else:
                now = await asyncio.to_thread(take_snapshot, ctx.workspace)
                if diff_snapshots(latest.snapshot_before, now).changed:
                    problem = "workspace changed during the interrupted step; automatic re-run is unsafe"
            if problem is None:
                tails = " ".join((e.stdout_tail or "") + (e.stderr_tail or "")
                                 for e in await repo.executions_for_task(task.id) if e.step_id == latest.id)
                if _EXTERNAL_SIDE_EFFECT.search(tails):
                    problem = "possible external side effect in the interrupted step"
            if problem is None:
                return False
            locked = await repo.lock(task.id)
            await self.gates.open_recovery(s, locked, latest, reason=problem, checkpoint_step_key=step_key)
            await self.leases.release(ctx.lease, "WAITING_APPROVAL", session=s)
            ctx.parked = True
            return True

    # ================================================================================================
    # reuse
    # ================================================================================================
    async def _reusable(self, task: Task, step_key: str, input_hash: str, *, iteration: int | None = None) -> TaskStep | None:
        """SUCCEEDED step with the same input_hash and an output that still matches its stored output_hash."""
        async with self.db.transaction() as s:
            step = await TaskRepository(s).latest_step(task.id, step_key, plan_revision=task.plan_revision,
                                                       workflow_iteration=iteration)
        if (step is not None and StepState(step.status) is StepState.SUCCEEDED and step.input_hash == input_hash
                and step.output is not None and stable_hash(step.output) == step.output_hash):
            return step
        return None

    async def _decisions(self, task_id: uuid.UUID) -> list[str]:
        async with self.db.transaction() as s:
            return [d.summary for d in await ApprovalRepository(s).design_decisions(task_id)]

    async def _plan_step(self, task: Task) -> TaskStep:
        async with self.db.transaction() as s:
            step = await TaskRepository(s).latest_step(task.id, STEP_PLAN, plan_revision=task.plan_revision)
        if step is None or StepState(step.status) is not StepState.SUCCEEDED:
            raise FailClosed("no accepted plan for this revision")
        return step

    async def _transition(self, ctx: RunContext, task_id: uuid.UUID, to: TaskState, reason: str,
                          values: dict | None = None, event: dict | None = None) -> None:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, ctx.lease)
            if values:
                await s.execute(update(Task).where(Task.id == task_id).values(**values))
            await change_task_state(s, task, to, reason=reason, event=event)

    # ================================================================================================
    # PLAN
    # ================================================================================================
    async def _do_pending(self, ctx: RunContext, task: Task) -> None:
        await self._transition(ctx, task.id, TaskState.PLANNING, "execution_started")

    async def _do_plan(self, ctx: RunContext, task: Task) -> None:
        decisions = await self._decisions(task.id)
        feedback = await self._plan_feedback(task)
        input_hash = stable_hash("plan", task.requirements_hash, task.decisions_hash, feedback, task.plan_revision)
        if await self._reusable(task, STEP_PLAN, input_hash):
            await self._transition(ctx, task.id, TaskState.DESIGN_REVIEWING, "plan_reused")
            return
        prompt = PR.planner_prompt(C.planner_context(task, decisions, feedback, ctx.workspace))

        async def evaluate(handle: StepHandle, ex: StepExecution, data: dict) -> StepDecision:
            changed = diff_snapshots(handle.snapshot_before, ex.snapshot_after)
            if changed.changed:                 # Plan Mode must be read-only; a change means the step cannot succeed
                raise PolicyViolation("PLANNER_MODIFIED_FILES", f"planner changed files: {changed.paths()[:5]}")
            try:
                gate_req = A.validate_gate_request(data.get("human_gate_request"))
                if gate_req is not None:        # a human decision comes before the plan is finished
                    return StepDecision(output={"gate_request": gate_req}, gate=GateSpec(
                        ApprovalType.DESIGN, gate_req, {"decision": gate_req["problem"]}, "STEP", STEP_PLAN))
                artifact = A.validate_plan_artifact(data)
            except A.ArtifactInvalid as exc:
                raise RetryableStepFailure(str(exc)) from exc

            async def mutate(s, t, st):
                await change_task_state(s, t, TaskState.DESIGN_REVIEWING, reason="plan_accepted",
                                        event={"plan_hash": A.plan_hash(artifact)})
            return StepDecision(output=artifact, mutate=mutate)

        await self._agent_step(ctx, task, step_key=STEP_PLAN, input_hash=input_hash, prompt=prompt,
                               schema=A.PLAN_SCHEMA, mutating=False, allow_resume=True, evaluate=evaluate)

    async def _plan_feedback(self, task: Task) -> list:
        """Issues from the design review that triggered this replan (if any)."""
        if task.plan_revision == 0:
            return []
        async with self.db.transaction() as s:
            prev = await TaskRepository(s).latest_step(task.id, STEP_DESIGN_REVIEW, plan_revision=task.plan_revision - 1)
        if prev and prev.output and prev.output.get("verdict") == "NEEDS_REVISION":
            return prev.output.get("issues", [])
        return []

    # ================================================================================================
    # DESIGN REVIEW
    # ================================================================================================
    async def _do_design_review(self, ctx: RunContext, task: Task) -> None:
        plan_step = await self._plan_step(task)
        basis = design_review_basis(task.requirements_hash, plan_step.output_hash, task.decisions_hash)
        input_hash = stable_hash("design_review", basis["basis_hash"])
        reused = await self._reusable(task, STEP_DESIGN_REVIEW, input_hash)
        if reused is not None:
            await self._apply_design_verdict(ctx, task, reused.output)
            return
        decisions = await self._decisions(task.id)
        prompt = PR.design_review_prompt(C.design_review_context(task, plan_step.output, decisions))

        async def evaluate(handle: StepHandle, ex: StepExecution, data: dict) -> StepDecision:
            try:
                review = A.validate_design_review(data)
            except A.ArtifactInvalid as exc:
                raise RetryableStepFailure(str(exc)) from exc
            verdict = review["verdict"]
            if verdict == "PASS" and A.has_critical(review):
                verdict = "NEEDS_REVISION"                      # PASS may never carry a CRITICAL issue
            output = {"verdict": verdict, "issues": review["issues"], "summary": review.get("summary", ""),
                      "basis": basis}
            if verdict == "NEEDS_HUMAN_REVIEW":
                req = review["human_gate_request"]
                return StepDecision(output=output, gate=GateSpec(
                    ApprovalType.DESIGN, req, {"decision": req["problem"]}, "STEP", STEP_PLAN))

            async def mutate(s, t, st):
                await self._verdict_transition(s, t, output)
            return StepDecision(output=output, mutate=mutate)

        await self._agent_step(ctx, task, step_key=STEP_DESIGN_REVIEW, input_hash=input_hash, prompt=prompt,
                               schema=A.DESIGN_REVIEW_SCHEMA, mutating=False, allow_resume=True, evaluate=evaluate)

    async def _apply_design_verdict(self, ctx: RunContext, task: Task, output: dict) -> None:
        async with self.db.transaction() as s:
            locked = await TaskRepository(s).lock(task.id)
            await self.leases.verify(s, ctx.lease)
            await self._verdict_transition(s, locked, output)

    async def _verdict_transition(self, s: AsyncSession, task: Task, output: dict) -> None:
        verdict = output["verdict"]
        if verdict == "PASS":
            await change_task_state(s, task, TaskState.DEVELOPING, reason="design_review_pass",
                                    event={"basis_hash": output["basis"]["basis_hash"]})
        elif verdict == "NEEDS_REVISION":
            if task.replan_count >= task.limits["max_replans"]:
                await change_task_state(s, task, TaskState.FAILED, reason="max_replans_exceeded",
                                        failure_reason="MAX_REPLANS_EXCEEDED", event={"issues": output["issues"][:20]})
                return
            await s.execute(update(Task).where(Task.id == task.id).values(
                replan_count=Task.replan_count + 1, plan_revision=Task.plan_revision + 1))
            await change_task_state(s, task, TaskState.PLANNING, reason="needs_revision",
                                    event={"issues": output["issues"][:20]})
        else:
            raise FailClosed("NEEDS_HUMAN_REVIEW must be handled through a Human Gate")

    # ================================================================================================
    # DEVELOP
    # ================================================================================================
    async def _do_develop(self, ctx: RunContext, task: Task) -> None:
        plan_step = await self._plan_step(task)
        basis = design_review_basis(task.requirements_hash, plan_step.output_hash, task.decisions_hash)
        async with self.db.transaction() as s:
            review = await TaskRepository(s).latest_step(task.id, STEP_DESIGN_REVIEW, plan_revision=task.plan_revision)
        passed = (review is not None and StepState(review.status) is StepState.SUCCEEDED and review.output
                  and review.output.get("verdict") == "PASS"
                  and review.output.get("basis", {}).get("basis_hash") == basis["basis_hash"])
        if not passed:        # the PASS belongs to another basis (or none): Development must not start
            await self._transition(ctx, task.id, TaskState.DESIGN_REVIEWING, "design_review_basis_changed",
                                   event={"current_basis": basis["basis_hash"]})
            return
        decisions = await self._decisions(task.id)
        feedback = await self._develop_feedback(task)
        input_hash = stable_hash("develop", plan_step.output_hash, task.decisions_hash, task.workflow_iteration,
                                 feedback)
        if await self._reusable(task, STEP_DEVELOP, input_hash, iteration=task.workflow_iteration):
            await self._transition(ctx, task.id, TaskState.TESTING, "develop_reused")
            return
        async with self.db.transaction() as s:
            last_dev = await TaskRepository(s).latest_step(task.id, STEP_DEVELOP)
        allow_resume = bool(last_dev and last_dev.output and last_dev.output.get("plan_hash") == plan_step.output_hash)
        prompt = PR.developer_prompt(C.developer_context(plan_step.output, decisions, feedback))

        async def evaluate(handle: StepHandle, ex: StepExecution, data: dict) -> StepDecision:
            try:
                req = A.validate_gate_request(data.get("human_gate_request"))
            except A.ArtifactInvalid as exc:
                raise RetryableStepFailure(str(exc)) from exc
            changed = diff_snapshots(handle.snapshot_before, ex.snapshot_after).paths()
            output = {"summary": str(data.get("summary", "")), "files_changed": changed,
                      "plan_hash": plan_step.output_hash, "code_fingerprint": ex.snapshot_after["code_fingerprint"]}
            if req is not None:
                return StepDecision(output=output, gate=GateSpec(
                    ApprovalType.DESIGN, req, {"decision": req["problem"]}, "STEP", STEP_DEVELOP))

            async def mutate(s, t, st):
                await change_task_state(s, t, TaskState.TESTING, reason="develop_done")
            return StepDecision(output=output, mutate=mutate)

        await self._agent_step(ctx, task, step_key=STEP_DEVELOP, input_hash=input_hash, prompt=prompt,
                               schema=A.DEV_SCHEMA, mutating=True, allow_resume=allow_resume, evaluate=evaluate)

    async def _develop_feedback(self, task: Task) -> list:
        if task.workflow_iteration == 0:
            return []
        it = task.workflow_iteration - 1
        feedback: list = []
        async with self.db.transaction() as s:
            repo = TaskRepository(s)
            test = await repo.latest_step(task.id, STEP_TEST, workflow_iteration=it)
            review = await repo.latest_step(task.id, STEP_CODE_REVIEW, workflow_iteration=it)
        if test and test.output and not test.output["test_result"]["passed"]:
            feedback.append({"source": "tests", "reason": test.output["test_result"]["reason"],
                             "failures": test.output["test_result"]["failures"][:20],
                             "repeated": bool(test.output.get("repeated_failure"))})
        if review and review.output and review.output.get("verdict") != "PASS":
            feedback.append({"source": "code_review", "issues": review.output.get("issues", [])})
        return feedback

    # ================================================================================================
    # TEST
    # ================================================================================================
    async def _do_test(self, ctx: RunContext, task: Task) -> None:
        plan_step = await self._plan_step(task)
        current = await asyncio.to_thread(take_snapshot, ctx.workspace)
        async with self.db.transaction() as s:
            prev = await TaskRepository(s).latest_step(task.id, STEP_TEST, workflow_iteration=task.workflow_iteration)
        if (prev is not None and StepState(prev.status) is StepState.SUCCEEDED and prev.output
                and stable_hash(prev.output) == prev.output_hash
                and prev.output.get("requirements_hash") == task.requirements_hash
                and prev.output.get("test_policy_hash") == task.test_policy_hash
                and prev.output.get("code_fingerprint") == current["code_fingerprint"]):
            await self._after_test(ctx, task, prev.output)          # identical fingerprints: reuse the result
            return
        policy = task.test_policy
        input_hash = stable_hash("test", task.requirements_hash, current["code_fingerprint"], task.test_policy_hash)
        change_summary = await self._latest_dev_summary(task)
        prompt = PR.tester_prompt(C.tester_context(task, plan_step.output, change_summary, policy["commands"][0]))
        ids_before = await self.tests.collect_ids(ctx.workspace, policy)

        async def evaluate(handle: StepHandle, ex: StepExecution, data: dict) -> StepDecision:
            try:
                req = A.validate_gate_request(data.get("human_gate_request"))
            except A.ArtifactInvalid as exc:
                raise RetryableStepFailure(str(exc)) from exc
            changed = diff_snapshots(handle.snapshot_before, ex.snapshot_after).paths()
            non_test = [p for p in changed if not is_test_path(p)]
            if non_test:
                raise PolicyViolation("TESTER_MODIFIED_NON_TEST_FILES", f"tester changed non-test files: {non_test[:5]}")
            if req is not None:
                return StepDecision(output={"gate_request": req}, gate=GateSpec(
                    ApprovalType.DESIGN, req, {"decision": req["problem"]}, "STEP", STEP_TEST))
            result = await self.tests.run(ctx.workspace, policy)       # authoritative: exit codes + JUnit
            ids_after = await self.tests.collect_ids(ctx.workspace, policy)
            baseline = await self._previous_test_ids(task) or ids_before
            signals = compute_test_signals(baseline, ids_after, result, [p for p in changed if is_test_path(p)])
            final = await asyncio.to_thread(take_snapshot, ctx.workspace)
            output = {"agent_summary": str(data.get("summary", "")), "test_result": result.to_dict(),
                      "test_signals": signals, "test_ids": sorted(ids_after or []),
                      "code_fingerprint": final["code_fingerprint"], "requirements_hash": task.requirements_hash,
                      "test_policy_hash": task.test_policy_hash,
                      "repeated_failure": await self._is_repeated_failure(task, result)}

            async def mutate(s, t, st):
                s.add(Execution(id=uuid.uuid4(), task_id=t.id, step_id=st.id, run_id=ctx.lease.run_id, role="TESTER",
                                provider="LOCAL", outcome="SUCCESS" if result.passed else "ERROR",
                                exit_code=next((c for c in result.exit_codes if c), 0), finished_at=datetime.now(UTC),
                                stdout_tail=result.output_tail[-4000:], runner_id=ctx.lease.runner_id,
                                detail={"orchestrator_test_run": True, "reason": result.reason}))
                await self._test_transition(s, t, output)
            return StepDecision(output=output, mutate=mutate)

        await self._agent_step(ctx, task, step_key=STEP_TEST, input_hash=input_hash, prompt=prompt,
                               schema=A.TEST_AGENT_SCHEMA, mutating=True, allow_resume=True, evaluate=evaluate)

    async def _after_test(self, ctx: RunContext, task: Task, output: dict) -> None:
        async with self.db.transaction() as s:
            locked = await TaskRepository(s).lock(task.id)
            await self.leases.verify(s, ctx.lease)
            await self._test_transition(s, locked, output)

    async def _test_transition(self, s: AsyncSession, task: Task, output: dict) -> None:
        if output["test_result"]["passed"]:
            await change_task_state(s, task, TaskState.REVIEWING, reason="tests_passed")
        else:
            await self._to_retry(s, task, "tests_failed", {"reason": output["test_result"]["reason"],
                                                          "repeated_failure": output.get("repeated_failure", False)})

    async def _to_retry(self, s: AsyncSession, task: Task, reason: str, event: dict, replan: bool = False) -> None:
        """Improvement loop: initial run + at most max_workflow_iterations improvements."""
        exhausted = ("MAX_REPLANS_EXCEEDED" if replan and task.replan_count >= task.limits["max_replans"]
                     else "MAX_ITERATIONS_EXCEEDED" if not replan
                     and task.workflow_iteration >= task.limits["max_workflow_iterations"] else None)
        if exhausted:
            await change_task_state(s, task, TaskState.FAILED, reason=exhausted.lower(), failure_reason=exhausted,
                                    event={**event, "iterations_used": task.workflow_iteration})
            return
        if replan:
            await s.execute(update(Task).where(Task.id == task.id).values(
                replan_count=Task.replan_count + 1, plan_revision=Task.plan_revision + 1,
                workflow_iteration=Task.workflow_iteration + 1, resume_step_key=STEP_PLAN))
        else:
            await s.execute(update(Task).where(Task.id == task.id).values(
                workflow_iteration=Task.workflow_iteration + 1))
        await change_task_state(s, task, TaskState.RETRYING, reason=reason, event=event)

    async def _previous_test_ids(self, task: Task) -> set[str] | None:
        if task.workflow_iteration == 0:
            return None
        async with self.db.transaction() as s:
            prev = await TaskRepository(s).latest_step(task.id, STEP_TEST, workflow_iteration=task.workflow_iteration - 1)
        return set(prev.output["test_ids"]) if prev and prev.output and prev.output.get("test_ids") else None

    async def _is_repeated_failure(self, task: Task, result) -> bool:
        if result.passed or task.workflow_iteration == 0:
            return False
        async with self.db.transaction() as s:
            prev = await TaskRepository(s).latest_step(task.id, STEP_TEST, workflow_iteration=task.workflow_iteration - 1)
        prev_fail = (prev.output or {}).get("test_result", {}).get("failures", []) if prev else []
        return bool(prev_fail) and sorted(prev_fail) == sorted(result.failures)

    async def _latest_dev_summary(self, task: Task) -> str:
        async with self.db.transaction() as s:
            dev = await TaskRepository(s).latest_step(task.id, STEP_DEVELOP, workflow_iteration=task.workflow_iteration)
        return (dev.output or {}).get("summary", "") if dev else ""

    # ================================================================================================
    # CODE REVIEW (+ Definition of Done)
    # ================================================================================================
    async def _do_code_review(self, ctx: RunContext, task: Task) -> None:
        plan_step = await self._plan_step(task)
        current = await asyncio.to_thread(take_snapshot, ctx.workspace)
        async with self.db.transaction() as s:
            test_step = await TaskRepository(s).latest_step(task.id, STEP_TEST, workflow_iteration=task.workflow_iteration)
        fresh = (test_step is not None and StepState(test_step.status) is StepState.SUCCEEDED and test_step.output
                 and test_step.output.get("code_fingerprint") == current["code_fingerprint"]
                 and test_step.output.get("test_policy_hash") == task.test_policy_hash)
        if not fresh:   # the Reviewer only ever sees test results produced for the CURRENT code
            await self._transition(ctx, task.id, TaskState.TESTING, "test_result_stale_for_current_code")
            return
        test_out = test_step.output
        input_hash = stable_hash("code_review", task.requirements_hash, current["code_fingerprint"],
                                 stable_hash(test_out["test_result"]), task.decisions_hash)
        reused = await self._reusable(task, STEP_CODE_REVIEW, input_hash, iteration=task.workflow_iteration)
        if reused is not None:
            await self._apply_review(ctx, task, reused.output)
            return
        async with self.db.transaction() as s:
            dev = await TaskRepository(s).latest_step(task.id, STEP_DEVELOP, workflow_iteration=task.workflow_iteration)
        changed = diff_snapshots(dev.snapshot_before, current).paths() if dev and dev.snapshot_before else []
        prompt = PR.code_review_prompt(C.code_review_context(task, plan_step.output, changed, test_out["test_result"],
                                                             test_out["test_signals"]))

        async def evaluate(handle: StepHandle, ex: StepExecution, data: dict) -> StepDecision:
            try:
                review = A.validate_code_review(data)
            except A.ArtifactInvalid as exc:
                raise RetryableStepFailure(str(exc)) from exc
            verdict = review["verdict"]
            if verdict == "PASS" and (A.has_critical(review) or review.get("test_policy_concern")):
                verdict = "FAIL"                      # an unresolved CRITICAL / weakened verification can never PASS
            output = {"verdict": verdict, "issues": review["issues"], "summary": review.get("summary", ""),
                      "plan_defect": bool(review.get("plan_defect")),
                      "test_policy_concern": bool(review.get("test_policy_concern")),
                      "code_fingerprint": current["code_fingerprint"]}
            if verdict == "NEEDS_HUMAN_REVIEW":
                req = review["human_gate_request"]
                return StepDecision(output=output, gate=GateSpec(
                    ApprovalType.DESIGN, req, {"decision": req["problem"]}, "STEP", STEP_DEVELOP))

            async def mutate(s, t, st):
                await self._review_transition(s, t, output)
            return StepDecision(output=output, mutate=mutate)

        await self._agent_step(ctx, task, step_key=STEP_CODE_REVIEW, input_hash=input_hash, prompt=prompt,
                               schema=A.CODE_REVIEW_SCHEMA, mutating=False, allow_resume=True, evaluate=evaluate)

    async def _apply_review(self, ctx: RunContext, task: Task, output: dict) -> None:
        async with self.db.transaction() as s:
            locked = await TaskRepository(s).lock(task.id)
            await self.leases.verify(s, ctx.lease)
            await self._review_transition(s, locked, output)

    async def _review_transition(self, s: AsyncSession, task: Task, output: dict) -> None:
        repo = TaskRepository(s)
        if output["verdict"] == "PASS":
            problems = await self._dod_problems(s, task, output)
            if not problems:
                await change_task_state(s, task, TaskState.COMPLETED, reason="definition_of_done_met")
                return
            output = {**output, "verdict": "FAIL", "issues": output["issues"] + [
                {"severity": "MAJOR", "description": f"Definition of Done: {p}"} for p in problems]}
        test = await repo.latest_step(task.id, STEP_TEST, workflow_iteration=task.workflow_iteration)
        await self._to_retry(s, task, "review_failed",
                             {"issues": output["issues"][:20], "tests_passed": bool(test and test.output
                                                                                    and test.output["test_result"]["passed"])},
                             replan=bool(output.get("plan_defect")))

    async def _dod_problems(self, s: AsyncSession, task: Task, review_out: dict) -> list[str]:
        repo = TaskRepository(s)
        plan = await repo.latest_step(task.id, STEP_PLAN, plan_revision=task.plan_revision)
        design = await repo.latest_step(task.id, STEP_DESIGN_REVIEW, plan_revision=task.plan_revision)
        test = await repo.latest_step(task.id, STEP_TEST, workflow_iteration=task.workflow_iteration)
        basis = design_review_basis(task.requirements_hash, plan.output_hash if plan else "", task.decisions_hash)
        design_ok = bool(design and design.output and design.output.get("verdict") == "PASS"
                         and design.output.get("basis", {}).get("basis_hash") == basis["basis_hash"])
        fingerprint = (test.output or {}).get("code_fingerprint", "") if test else ""
        pending = len(await ApprovalRepository(s).pending_for_task(task.id))
        return check_definition_of_done(DoDInput(
            test_output=test.output if test else None, review=review_out, current_code_fingerprint=fingerprint,
            requirements_hash=task.requirements_hash, test_policy_hash=task.test_policy_hash,
            design_review_passed=design_ok, pending_approvals=pending))

    # ================================================================================================
    # RETRYING
    # ================================================================================================
    async def _do_retrying(self, ctx: RunContext, task: Task) -> None:
        if task.resume_step_key == STEP_PLAN:
            await self._transition(ctx, task.id, TaskState.PLANNING, "replan", values={"resume_step_key": None})
        else:
            await self._transition(ctx, task.id, TaskState.DEVELOPING, "improvement_iteration")



WorkflowService._handlers = {
    TaskState.PENDING: WorkflowService._do_pending,
    TaskState.PLANNING: WorkflowService._do_plan,
    TaskState.DESIGN_REVIEWING: WorkflowService._do_design_review,
    TaskState.DEVELOPING: WorkflowService._do_develop,
    TaskState.TESTING: WorkflowService._do_test,
    TaskState.REVIEWING: WorkflowService._do_code_review,
    TaskState.RETRYING: WorkflowService._do_retrying,
}
