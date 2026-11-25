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
from app.repositories.task_repository import TaskRepository
from app.services.recovery_effects import EffectRetry
from app.services.tracked_classifier import classify_row
from app.validation import policies as P
from app.validation.exceptions import Conflict


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


REASON_UNRESOLVED = "UNRESOLVED_TRACKED_EXECUTION"
REASON_ANOMALY = "TRACKED_EXECUTION_ANOMALY"


@dataclass(frozen=True)
class TrackedAnomaly:
    """One member of set A, built from the classifier output only: identifiers and fixed codes, never a value of `detail`
    (no raw text, rationale, requested_by, pid or exception message)."""
    execution_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    anomaly_kind: str
    sub_reason: str | None


@dataclass(frozen=True)
class TrackedBlock:
    """The bounded verdict of `has_unresolved_or_anomalous_for_project`: tuples of at most DIAGNOSTIC_MAX_ANOMALIES items."""
    unresolved_ids: tuple[uuid.UUID, ...] = ()
    anomalies: tuple[TrackedAnomaly, ...] = ()
    unresolved_truncated: bool = False
    anomalies_truncated: bool = False
    anomalies_total: int = 0
    anomalies_total_is_exact: bool = True

    @property
    def blocked(self) -> bool:
        """Either set is non-empty: no CLEAN, no automatic continue, no new owner."""
        return bool(self.unresolved_ids or self.anomalies)

    @property
    def reason_code(self) -> str | None:
        """Unresolved rows win the code (the operator path of 7.5 exists only for them); None when nothing blocks."""
        if self.unresolved_ids:
            return REASON_UNRESOLVED
        return REASON_ANOMALY if self.anomalies else None


async def has_unresolved_or_anomalous_for_project(session, project_id: uuid.UUID) -> TrackedBlock:
    """THE common project-wide entry point of the unresolved-or-anomalous rule (design 7.4, I6). `project_id` is required and
    never None (no Task id, no keyword switch). It runs BOTH queries, set U and set A, and a caller cannot ask for one without
    the other; the two queries are private to this function. Plain SELECTs: no row lock, no new lock edge."""
    if project_id is None:
        raise ValueError("project_id is required")
    cap = P.DIAGNOSTIC_MAX_ANOMALIES
    repo = TaskRepository(session)
    unresolved = await repo.unresolved_tracked_executions_for_project(project_id, cap + 1)
    members = await repo.anomalous_tracked_executions_for_project(project_id, cap + 1)
    anomalies: list[TrackedAnomaly] = []
    for row in members[:cap]:
        verdict = classify_row(row.id, row.pid, row.finished_at, row.detail_text)
        if verdict.is_anomaly:
            kind, sub = verdict.anomaly_kind, verdict.sub_reason
        else:       # the SQL says A, the classifier disagrees: never read as clean (fail closed), the kind is the catch-all
            kind, sub = "OTHER_SCHEMA_INVALID", "other"
        anomalies.append(TrackedAnomaly(row.id, row.task_id, project_id, kind, sub))
    truncated = len(members) > cap
    return TrackedBlock(
        unresolved_ids=tuple(row.id for row in unresolved[:cap]), anomalies=tuple(anomalies),
        unresolved_truncated=len(unresolved) > cap, anomalies_truncated=truncated,
        anomalies_total=min(len(members), cap + 1), anomalies_total_is_exact=not truncated)


class ProjectUnresolvedTrackedExecution(Conflict):
    """The `continue` guard (design 7.4 'After DIRTY_UNRESOLVED'): a plain Conflict, never EffectRetry."""
    code = "PROJECT_UNRESOLVED_TRACKED_EXECUTION"


class ProjectTrackedExecutionAnomaly(Conflict):
    code = "PROJECT_TRACKED_EXECUTION_ANOMALY"


async def assert_no_unresolved_or_anomalous_tracked(session, project_id: uuid.UUID) -> None:
    """The guard before `accept_baseline` (design 7.4, 'After DIRTY_UNRESOLVED'): a plain Conflict, never EffectRetry."""
    block = await has_unresolved_or_anomalous_for_project(session, project_id)
    if block.unresolved_ids:
        raise ProjectUnresolvedTrackedExecution("the project still has an unresolved tracked execution: "
                                                + ", ".join(str(i) for i in block.unresolved_ids[:5]),
                       {"reason_code": REASON_UNRESOLVED, "execution_ids": [str(i) for i in block.unresolved_ids]})
    if block.anomalies:
        raise ProjectTrackedExecutionAnomaly("the project has an anomalous tracked execution: "
                                             + ", ".join(f"{a.execution_id} ({a.anomaly_kind})" for a in block.anomalies[:5]),
                       {"reason_code": REASON_ANOMALY,
                        "anomalies": [{"execution_id": str(a.execution_id), "kind": a.anomaly_kind} for a in block.anomalies]})


async def release_on_terminal(session, task: Task, to_state: TaskState, proof) -> None:
    """Releases the write lock held by `task` (if it holds one) together with its write baseline, in the caller's
    transaction, and records whether the workspace is CLEAN or DIRTY_UNRESOLVED.

      * a task that is not the owner (legacy task, never acquired, lock already gone) -> nothing to do;
      * valid proof + COMPLETED -> CLEAN, the final fingerprint becomes the accepted baseline;
      * valid proof + FAILED/CANCELLED -> CLEAN only if nothing differs from the write baseline, else DIRTY;
      * no proof / UNMEASURED / stale version -> never CLEAN: DIRTY_UNRESOLVED;
      * any unresolved or anomalous tracked execution of the project (any Task of it) -> DIRTY_UNRESOLVED whatever the proof
        is (H7); a valid proof is then not consulted and cannot cause FingerprintProofStale.
    """
    if task.project_id is None:
        return
    repo = ProjectRepository(session)
    project = await repo.lock(task.project_id)
    if project is None or project.write_owner_task_id != task.id:
        return
    if (await has_unresolved_or_anomalous_for_project(session, task.project_id)).blocked:
        await repo.release(project.id, state="DIRTY_UNRESOLVED", baseline=None)
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
