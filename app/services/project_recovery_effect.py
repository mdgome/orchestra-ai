"""PROJECT_DIRTY recovery: a task wants the project write lock but an earlier task left unresolved changes.

Effects of the decision (all inside the single decide() transaction, after the proof was verified):
  * continue -> the measured current state becomes the accepted baseline, the project is CLEAN again, a RESUME
                request is created (the gate service does that for resume=True). No file is touched.
  * abort / reject -> the task fails through the common terminal path; project files are never reset, cleaned,
                checked out or deleted, and the project stays DIRTY_UNRESOLVED.
  * accept_unproven_risk / recheck_processes -> ONE process_risk_requests row with the final, locked target snapshot, no
                RESUME and no OS call (the worker does the verification, design 7.5).
  * refresh_gate -> a stale gate of a still-DIRTY project: a new gate at the current epoch (lineage `supersedes_approval_id`),
                resume=False.
This is the orchestration layer: it combines state_service, project_terminal and ProjectLockService; the gate service
itself only sees the neutral RecoveryEffects registry. It is the only dynamic effect: `allowed_options` (decision) and
`allowed_options_batch` (reads) run the one computation of `dirty_gate_options`.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.models.states import TaskState
from app.models.tables import Approval, Project, Task
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.process_risk_repository import ProcessRiskRequestRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import RiskLockFailure, TaskRepository
from app.services.approval_lineage import lineage_keys
from app.services.dirty_gate_options import RISK_OPTION_KIND, ProviderResult, compute_gate_options
from app.services.project_lock_service import ProjectLockService
from app.services.project_terminal import (
    UNMEASURED, FingerprintProof, FingerprintProofStale, assert_no_unresolved_or_anomalous_tracked, proof_matches,
)
from app.services.recovery_effects import EffectResult
from app.services.state_service import change_task_state
from app.services.tracked_classifier import classify_row, risk_acceptance_pid_valid
from app.validation import policies as P
from app.validation.exceptions import Conflict


class StaleProjectGate(Conflict):
    code = "STALE_PROJECT_GATE"


class OptionNotAvailable(Conflict):
    code = "OPTION_NOT_AVAILABLE"

    def __init__(self, message: str, reason_code: str | None, retryable: bool):
        super().__init__(message, {"reason_code": reason_code, "retryable": retryable})
        self.reason_code, self.retryable = reason_code, retryable


class ProjectDirtyRecoveryEffect:
    def __init__(self, locks: ProjectLockService):
        self.locks = locks
        self.gates = None                       # bound by HumanGateService (refresh_gate opens the new gate through it)

    def bind(self, gates) -> None:
        self.gates = gates

    # --- the one computation, two entry points ---------------------------------------------------------------------------
    async def allowed_options(self, session, approval: Approval, task: Task, *, only: str | None = None,
                              project: Project | None = None) -> ProviderResult:
        if project is None:
            project = await ProjectRepository(session).get(uuid.UUID(approval.target["project_id"]))
        return await compute_gate_options(session, approval, task, project, now=datetime.now(UTC), only=only)

    async def allowed_options_batch(self, session, approvals: list[Approval]) -> dict[uuid.UUID, ProviderResult]:
        """One batch for the approvals of a read: the Tasks and projects are loaded in bulk and the stage-1 read of a project
        is shared; each approval runs in its own savepoint so a failed scan cannot poison the others."""
        task_ids = {a.task_id for a in approvals}
        project_ids = {uuid.UUID(a.target["project_id"]) for a in approvals if (a.target or {}).get("project_id")}
        tasks = {t.id: t for t in (await session.execute(select(Task).where(Task.id.in_(task_ids)))).scalars()}
        projects = {p.id: p for p in (await session.execute(select(Project).where(Project.id.in_(project_ids)))).scalars()}
        cache: dict = {}
        out: dict[uuid.UUID, ProviderResult] = {}
        for approval in approvals:
            project = projects.get(uuid.UUID(approval.target["project_id"])) if (approval.target or {}).get("project_id") else None
            try:
                async with session.begin_nested():
                    out[approval.id] = await compute_gate_options(
                        session, approval, tasks.get(approval.task_id), project, now=datetime.now(UTC), stage1_cache=cache)
            except Exception:  # noqa: BLE001 - fail closed: nothing is offered, the client retries
                out[approval.id] = ProviderResult([], [])
        return out

    async def prepare(self, approval, decision: str, option: str | None):
        """Outside any transaction: measure the workspace when the decision can accept it as the new baseline;
        otherwise (abort / reject) only a proof for a possible terminal transition. The two risk options, refresh_gate and an
        unknown option measure nothing."""
        project_id = uuid.UUID(approval.target["project_id"])
        if decision == "APPROVED" and option == "continue":
            return await self.locks.measure_project(project_id)
        if decision == "APPROVED" and (option in RISK_OPTION_KIND or option == "refresh_gate"
                                       or (option is not None and option != "abort")):
            return None
        return await self.locks.measure_for_task(approval.task_id)

    async def apply(self, s, repo, approval, task, decision, prepared, requested_by: str | None = None) -> EffectResult:
        projects = ProjectRepository(s)
        project = await projects.lock(uuid.UUID(approval.target["project_id"]))
        if project is None or task.project_id != project.id:
            raise StaleProjectGate("the task no longer belongs to the project this gate was opened for")
        option = decision.selected_option
        approved = decision.decision == "APPROVED"
        if approved:                 # every submitted option of an APPROVED decision is validated; a REJECTED one is not
            offered = await self.allowed_options(s, approval, task, only=option, project=project)
            if option not in offered.offered_ids:
                hidden = offered.hidden_for(option) if option else None
                raise OptionNotAvailable("the option is not currently available", hidden.reason_code if hidden else None,
                                         hidden.retryable if hidden else False)
        else:
            offered = None
        if not (approved and option in ("continue", "accept_unproven_risk", "recheck_processes", "refresh_gate")):
            await change_task_state(
                s, task, TaskState.FAILED,
                reason="project_dirty_aborted" if approved else "project_dirty_rejected",
                failure_reason="PROJECT_DIRTY_ABORTED" if approved else "PROJECT_DIRTY_REJECTED",
                project_proof=prepared)
            return EffectResult(resume=False)
        if option in RISK_OPTION_KIND:
            await self._create_risk_request(s, repo, approval, task, project, decision, offered, requested_by)
            return EffectResult(resume=False)
        if option == "refresh_gate":
            await self._refresh_gate(s, approval, task, project)
            return EffectResult(resume=False)
        if project.workspace_state == "CLEAN":
            return EffectResult(resume=True)            # already resolved by another decision: just resume
        if project.write_epoch != approval.target.get("dirty_epoch"):
            raise StaleProjectGate("the project changed after this gate was opened",
                                   {"dirty_epoch": approval.target.get("dirty_epoch")})
        if prepared is UNMEASURED or not isinstance(prepared, FingerprintProof):
            raise FingerprintProofStale("no valid measurement of the workspace")      # -> retry, then 409
        if not proof_matches(project, prepared):
            raise FingerprintProofStale("project owner or write_epoch changed since the measurement")
        # H7: a baseline is never accepted over an unresolved or anomalous tracked execution of the project (plain Conflict)
        await assert_no_unresolved_or_anomalous_tracked(s, project.id)
        await projects.accept_baseline(project.id, prepared.fingerprint)
        return EffectResult(resume=True)

    async def _create_risk_request(self, s, repo: ApprovalRepository, approval, task, project, decision, offered, requested_by):
        """Design 7.5 item 4: the decision-time predicate, the final snapshot under row locks, and the request INSERT."""
        option = decision.selected_option
        if task.status != str(TaskState.WAITING_APPROVAL) or any(
                p.id != approval.id for p in await repo.pending_for_task(task.id)):
            raise OptionNotAvailable("the task is not waiting on this gate", None, True)
        requests = ProcessRiskRequestRepository(s)
        if await requests.get(approval.id) is not None:
            raise Conflict("a risk request of this approval already exists")
        candidates = list(offered.candidates.get(option, ()))
        try:
            rows = await TaskRepository(s).lock_risk_targets(project.id, candidates)
        except RiskLockFailure as exc:
            raise OptionNotAvailable("the target rows could not be locked", exc.reason, True) from exc
        survivors = [r.id for r in rows if classify_row(r.id, r.pid, r.finished_at, r.detail_text).is_unresolved]
        if not survivors:                                     # an empty snapshot is never inserted
            raise OptionNotAvailable("the option set changed under the decision", None, True)
        if option == "accept_unproven_risk" and not all(risk_acceptance_pid_valid(r.pid) for r in rows if r.id in survivors):
            raise OptionNotAvailable("a target has no pid, so no valid risk acceptance can be recorded", "TARGET_WITHOUT_PID", False)
        await requests.insert(approval_id=approval.id, kind=RISK_OPTION_KIND[option], task_id=task.id, project_id=project.id,
                              dirty_epoch=project.write_epoch, target_execution_ids=survivors,
                              rationale=decision.rationale, requested_by=requested_by)

    async def _refresh_gate(self, s, approval, task, project) -> None:
        """A new gate at the CURRENT epoch; the stale one is already DECIDED. At most one direct successor per approval."""
        if await ApprovalRepository(s).successor_of(approval.id) is not None:
            return
        await self.gates.open_recovery(
            s, task, None, reason="the project workspace has unresolved changes from an earlier task",
            checkpoint_step_key=approval.checkpoint_step_key, reason_code=P.RECOVERY_REASON_PROJECT_DIRTY,
            options=P.PROJECT_DIRTY_OPTIONS,
            target_extra={"project_id": str(project.id), "dirty_epoch": project.write_epoch,
                          "baseline_ref": (project.baseline_fingerprint or "")[-16:],
                          **lineage_keys(approval, project.write_epoch, "REFRESH_GATE")},
            detail={"project": project.name})
