"""The ONE place a project's write lock and write baseline are cleaned up when a task reaches a terminal state.

Low level on purpose: it works on an already-open session with the task row (and, here, the project row) locked by
the caller. It opens no transaction, measures no fingerprint and imports neither state_service nor
ProjectLockService (state_service calls it, so the dependency points one way only).

A FingerprintProof is measured OUTSIDE any transaction (owner and write_epoch are read first, then the workspace is
hashed). Here, under the project row lock, owner and write_epoch must still equal the proof's; otherwise the proof
is stale, nothing is persisted and FingerprintProofStale makes the caller roll back and re-measure.
"""
import uuid
from dataclasses import dataclass

from app.infra.workspace_fingerprint import is_current_version
from app.models.states import TaskState
from app.models.tables import Task
from app.repositories.project_repository import ProjectRepository
from app.services.recovery_effects import EffectRetry


@dataclass(frozen=True)
class FingerprintProof:
    project_id: uuid.UUID
    owner_task_id: uuid.UUID | None      # who owned the write lock when the measurement started
    write_epoch: int                     # the project's write_epoch when the measurement started
    fingerprint: str
    mode: str


class _Unmeasured:
    """The workspace could not be measured (or the retries ran out): never evidence for CLEAN."""
    def __repr__(self) -> str:
        return "UNMEASURED"


UNMEASURED = _Unmeasured()


class FingerprintProofStale(EffectRetry):
    """Owner or write_epoch changed between measuring and locking the project row."""


def proof_matches(project, proof: FingerprintProof) -> bool:
    return (proof.project_id == project.id and proof.owner_task_id == project.write_owner_task_id
            and proof.write_epoch == project.write_epoch)


async def release_on_terminal(session, task: Task, to_state: TaskState, proof) -> None:
    """Releases the write lock held by `task` (if it holds one) together with its write baseline, in the caller's
    transaction, and records whether the workspace is CLEAN or DIRTY_UNRESOLVED.

      * a task that is not the owner (legacy task, never acquired, lock already gone) -> nothing to do;
      * valid proof + COMPLETED -> CLEAN, the final fingerprint becomes the accepted baseline;
      * valid proof + FAILED/CANCELLED -> CLEAN only if nothing differs from the write baseline, else DIRTY;
      * no proof / UNMEASURED / stale version -> never CLEAN: DIRTY_UNRESOLVED.
    """
    if task.project_id is None:
        return
    repo = ProjectRepository(session)
    project = await repo.lock(task.project_id)
    if project is None or project.write_owner_task_id != task.id:
        return
    fingerprint = None
    if isinstance(proof, FingerprintProof):
        if not proof_matches(project, proof) or proof.owner_task_id != task.id:
            raise FingerprintProofStale("project owner or write_epoch changed since the fingerprint was measured")
        fingerprint = proof.fingerprint
    write_baseline = project.write_baseline_fingerprint
    if fingerprint is None:
        state, baseline = "DIRTY_UNRESOLVED", None
    elif to_state is TaskState.COMPLETED:
        state, baseline = "CLEAN", fingerprint
    elif is_current_version(write_baseline) and fingerprint == write_baseline:
        state, baseline = "CLEAN", None
    else:
        state, baseline = "DIRTY_UNRESOLVED", None
    await repo.release(project.id, state=state, baseline=baseline)
