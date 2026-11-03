"""Human Gate: DESIGN / RECOVERY / PERMISSION approvals, idempotent decisions, auto-resume requests.

A DESIGN approval is a design decision only; it never widens CLI permissions (PERMISSION approvals are
the sole, separate, single-use path for that).
"""
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.models.states import ApprovalType, StepState, TaskState
from app.models.tables import Approval, ApprovalDecision, ExecutionRequest, Task, TaskStep
from app.repositories.approval_repository import ApprovalRepository, ExecutionRequestRepository
from app.repositories.task_repository import TaskRepository
from app.services.fingerprints import current_basis
from app.services.hashing import stable_hash
from app.services.recovery_effects import EffectRetry, RecoveryEffects
from app.services.state_service import change_step_state, change_task_state
from app.validation import policies as P
from app.validation import validators as V
from app.validation.exceptions import Conflict, Gone, NotFound

RECOVERY_OPTIONS = ("continue", "retry", "abort")


class StaleBasis(Conflict):
    code = "STALE_BASIS"


class IdempotencyConflict(Conflict):
    code = "IDEMPOTENCY_KEY_CONFLICT"


class ProofUnstable(Conflict):
    code = "PROJECT_FINGERPRINT_UNSTABLE"


@dataclass(frozen=True)
class DecisionOutcome:
    decision: ApprovalDecision
    created: bool           # False -> an identical earlier decision was returned (HTTP 200)


def request_fingerprint(decision: str, selected_option: str | None) -> str:
    return stable_hash("decision", decision, selected_option or "")


class HumanGateService:
    def __init__(self, db: Database, clock=lambda: datetime.now(UTC), effects: RecoveryEffects | None = None,
                 measure=None):
        """`effects`: recovery kinds with their own effect (looked up by the stable reason_code).
        `measure`: async task_id -> fingerprint proof (or None); used before a decision that may end the task."""
        self.db = db
        self._now = clock
        self.effects = effects
        self._measure = measure

    # --- opening gates (always inside the caller's transaction) -----------------------------
    async def open_gate(self, session: AsyncSession, task: Task, step: TaskStep | None, *, type_: ApprovalType,
                        checkpoint_step_key: str, request_payload: dict, target: dict, scope: str,
                        execution_id: uuid.UUID | None = None, command_fingerprint: str | None = None,
                        enter_waiting: bool = True) -> Approval:
        """Creates the approval, the step WAITING_APPROVAL and the task WAITING_APPROVAL together."""
        repo = ApprovalRepository(session)
        approval = await repo.add(Approval(
            id=uuid.uuid4(), task_id=task.id, step_id=step.id if step else None, type=str(type_), status="PENDING",
            target=target, scope=scope, request_payload=request_payload, basis=await current_basis(session, task),
            checkpoint_step_key=checkpoint_step_key,
            valid_until=self._now() + timedelta(seconds=P.APPROVAL_TTL_SECONDS),
            execution_id=execution_id, command_fingerprint=command_fingerprint))
        if step is not None and StepState(step.status) is StepState.RUNNING:
            await change_step_state(session, step, StepState.WAITING_APPROVAL)
        if enter_waiting and TaskState(task.status) is not TaskState.WAITING_APPROVAL:
            await session.execute(update(Task).where(Task.id == task.id).values(resume_step_key=checkpoint_step_key))
            await change_task_state(session, task, TaskState.WAITING_APPROVAL, reason=f"gate_{type_.lower()}",
                                    event={"approval_id": str(approval.id), "type": str(type_)})
        return approval

    async def open_recovery(self, session: AsyncSession, task: Task, step: TaskStep | None, *, reason: str,
                            checkpoint_step_key: str, detail: dict | None = None,
                            reason_code: str = P.RECOVERY_REASON_EXECUTION_UNCERTAIN,
                            options: tuple[str, ...] = RECOVERY_OPTIONS, target_extra: dict | None = None) -> Approval:
        """Task=WAITING_APPROVAL + Approval(RECOVERY) + Step=NEEDS_MANUAL_RECOVERY in ONE transaction.
        No separate recovery state machine: only existing states are used.

        `reason_code` is the stable kind of recovery (stored in request_payload AND target); it, never the message
        text, selects the effect. `step` may be None (a gate opened before any step attempt exists)."""
        if step is not None:
            if StepState(step.status) is StepState.RUNNING:
                await change_step_state(session, step, StepState.INTERRUPTED)
            if StepState(step.status) is StepState.INTERRUPTED:
                await change_step_state(session, step, StepState.NEEDS_MANUAL_RECOVERY, error=reason)
        payload = {"problem": reason, "reason_code": reason_code, "options": [{"id": o} for o in options],
                   "recommendation": "abort", "detail": detail or {}}
        target = {"reason": reason, "reason_code": reason_code, "step_id": str(step.id) if step else None,
                  "resume_step_key": checkpoint_step_key, **(target_extra or {})}
        return await self.open_gate(
            session, task, step, type_=ApprovalType.RECOVERY, checkpoint_step_key=checkpoint_step_key,
            request_payload=payload, target=target, scope="STEP")

    # --- decisions --------------------------------------------------------------------------
    async def decide(self, approval_id: uuid.UUID, *, decision: str, selected_option: str | None,
                     rationale: str | None, idempotency_key: str | None) -> DecisionOutcome:
        V.validate_decision(decision, idempotency_key, selected_option)
        fp = request_fingerprint(decision, selected_option)
        # Optimistic retry: anything that needs the workspace measured is measured OUTSIDE the transaction. If the
        # proof is stale when it is checked under the project lock, the whole transaction rolled back (no decision,
        # no status change, no RESUME request was committed) and the decision is retried as ONE logical decision
        # (same idempotency key) with a fresh measurement.
        for _ in range(P.PROOF_MAX_ATTEMPTS):
            prepared = await self._prepare(approval_id, decision, selected_option)
            try:
                return await self._decide_once(approval_id, decision, selected_option, rationale, idempotency_key,
                                               fp, prepared)
            except EffectRetry:
                continue
        raise ProofUnstable("the project workspace kept changing while the decision was being applied; nothing was "
                            "recorded, please retry")

    async def _prepare(self, approval_id: uuid.UUID, decision: str, option: str | None):
        async with self.db.transaction() as s:
            approval = await ApprovalRepository(s).get(approval_id)
        if approval is None:
            return None
        effect = self._effect_for(approval)
        if effect is not None:
            return await effect.prepare(approval, decision, option)
        ends_task = decision != "APPROVED" or (approval.type == str(ApprovalType.RECOVERY) and option == "abort")
        if ends_task and self._measure is not None:
            return await self._measure(approval.task_id)       # proof for a possible terminal transition
        return None

    def _effect_for(self, approval: Approval):
        if self.effects is None or approval.type != str(ApprovalType.RECOVERY):
            return None
        return self.effects.get((approval.target or {}).get("reason_code"))

    async def _decide_once(self, approval_id, decision, selected_option, rationale, idempotency_key, fp, prepared):
        async with self.db.transaction() as s:
            repo = ApprovalRepository(s)
            approval = await repo.lock(approval_id)                     # (1) serialise concurrent decisions
            if approval is None:
                raise NotFound("approval not found")
            existing = await repo.get_decision(approval_id)
            if existing is not None:                                    # (2) compare with the stored decision FIRST
                if existing.request_fingerprint == fp:
                    return DecisionOutcome(existing, created=False)
                if existing.idempotency_key == idempotency_key:
                    raise IdempotencyConflict("Idempotency-Key was already used with a different request")
                raise Conflict("approval already decided differently",
                               {"existing_decision": existing.decision, "existing_option": existing.selected_option})
            # (3) only a NEW decision is checked for expiry and staleness
            now = self._now()
            if approval.valid_until <= now:
                raise Gone("approval expired")
            task = await TaskRepository(s).lock(approval.task_id)
            if approval.basis != await current_basis(s, task):
                raise StaleBasis("the plan/requirements/decisions changed after this approval was requested",
                                 {"approval_basis": approval.basis})
            options = [o.get("id") for o in approval.request_payload.get("options", []) if isinstance(o, dict)]
            if decision == "APPROVED" and options and selected_option not in options:
                raise V.ValidationFailed("selected_option must be one of the offered options", {"options": options})
            row = ApprovalDecision(
                id=uuid.uuid4(), approval_id=approval_id, decision=decision, selected_option=selected_option,
                rationale=rationale, idempotency_key=idempotency_key, request_fingerprint=fp, basis=approval.basis)
            if not await repo.insert_decision_if_absent(row):
                raise Conflict("approval was decided concurrently")
            await repo.mark_decided(approval_id)
            await self._apply(s, repo, approval, task, row, prepared)   # (4) effects, same transaction
            stored = await repo.get_decision(approval_id)
            return DecisionOutcome(stored, created=True)

    async def _apply(self, s: AsyncSession, repo: ApprovalRepository, approval: Approval, task: Task,
                     decision: ApprovalDecision, prepared=None) -> None:
        approved = decision.decision == "APPROVED"
        type_ = ApprovalType(approval.type)
        resume = approved
        effect = self._effect_for(approval)
        if effect is not None:
            result = await effect.apply(s, repo, approval, task, decision, prepared)
            if result.resume:
                await ExecutionRequestRepository(s).add(ExecutionRequest(
                    id=uuid.uuid4(), task_id=task.id, kind="RESUME", status="PENDING", approval_id=approval.id))
            return
        if type_ is ApprovalType.DESIGN:
            summary = f"{decision.decision}: {decision.selected_option or '-'}: {decision.rationale or ''}"
            await repo.add_design_decision(task.id, approval.id, summary)
            decisions = await repo.design_decisions(task.id)
            await s.execute(update(Task).where(Task.id == task.id).values(
                decisions_hash=stable_hash([d.summary for d in decisions])))
            resume = True        # approved or rejected, planning continues with the decision on record
        elif type_ is ApprovalType.RECOVERY:
            resume = approved and decision.selected_option in ("continue", "retry")
            if approved and decision.selected_option == "abort":
                await change_task_state(s, task, TaskState.FAILED, reason="recovery_aborted",
                                        failure_reason="RECOVERY_ABORTED", project_proof=prepared)
                return
        elif type_ is ApprovalType.PERMISSION:
            if approved and not approval.target.get("enforceable", False):
                return           # the CLI cannot enforce a one-off grant: hold, never widen permissions
        if not resume:
            await change_task_state(s, task, TaskState.FAILED, reason=f"{approval.type.lower()}_rejected",
                                    failure_reason=f"{approval.type}_REJECTED", project_proof=prepared)
            return
        # Approval decision and the RESUME request are one transaction; approval_id is UNIQUE.
        await ExecutionRequestRepository(s).add(ExecutionRequest(
            id=uuid.uuid4(), task_id=task.id, kind="RESUME", status="PENDING", approval_id=approval.id))
