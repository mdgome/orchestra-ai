"""PROJECT_DIRTY recovery: a task wants the project write lock but an earlier task left unresolved changes.

Effects of the decision (all inside the single decide() transaction, after the proof was verified):
  * continue -> the measured current state becomes the accepted baseline, the project is CLEAN again, a RESUME
                request is created (the gate service does that for resume=True). No file is touched.
  * abort / reject -> the task fails through the common terminal path; project files are never reset, cleaned,
                checked out or deleted, and the project stays DIRTY_UNRESOLVED.
This is the orchestration layer: it combines state_service, project_terminal and ProjectLockService; the gate service
itself only sees the neutral RecoveryEffects registry.
"""
import uuid

from app.models.states import TaskState
from app.repositories.project_repository import ProjectRepository
from app.services.project_lock_service import ProjectLockService
from app.services.project_terminal import UNMEASURED, FingerprintProof, FingerprintProofStale, proof_matches
from app.services.recovery_effects import EffectResult
from app.services.state_service import change_task_state
from app.validation.exceptions import Conflict


class StaleProjectGate(Conflict):
    code = "STALE_PROJECT_GATE"


class ProjectDirtyRecoveryEffect:
    def __init__(self, locks: ProjectLockService):
        self.locks = locks

    async def prepare(self, approval, decision: str, option: str | None):
        """Outside any transaction: measure the workspace when the decision can accept it as the new baseline;
        otherwise (abort / reject) only a proof for a possible terminal transition."""
        project_id = uuid.UUID(approval.target["project_id"])
        if decision == "APPROVED" and option == "continue":
            return await self.locks.measure_project(project_id)
        return await self.locks.measure_for_task(approval.task_id)

    async def apply(self, s, repo, approval, task, decision, prepared) -> EffectResult:
        projects = ProjectRepository(s)
        project = await projects.lock(uuid.UUID(approval.target["project_id"]))
        if project is None or task.project_id != project.id:
            raise StaleProjectGate("the task no longer belongs to the project this gate was opened for")
        if not (decision.decision == "APPROVED" and decision.selected_option == "continue"):
            await change_task_state(
                s, task, TaskState.FAILED,
                reason="project_dirty_aborted" if decision.decision == "APPROVED" else "project_dirty_rejected",
                failure_reason="PROJECT_DIRTY_ABORTED" if decision.decision == "APPROVED" else "PROJECT_DIRTY_REJECTED",
                project_proof=prepared)
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
        await projects.accept_baseline(project.id, prepared.fingerprint)
        return EffectResult(resume=True)
