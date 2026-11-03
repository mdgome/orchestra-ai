"""Project write lock: proof measurement, pre-step acquisition / verification, reconcile.

Terminal release does NOT live here (project_terminal.release_on_terminal owns it). This service never changes a
task's state and does not import state_service.

Order, always:  claim -> Task lease -> [pre-step project gate: acquire / verify / DIRTY gate] -> step attempt -> CLI.
The gate runs BEFORE a step attempt exists, so waiting for the lock or for a PROJECT_DIRTY decision consumes no
step_attempt, no cli_invocation and no workflow_iteration.

Optimistic retry: the workspace is fingerprinted outside any transaction; a short transaction then locks the
project row and compares owner and write_epoch with the proof. A mismatch persists nothing (rollback) and the whole
operation is retried after a fresh measurement. The project row lock is never held while hashing.
"""
import asyncio
import logging
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select

from app.db import Database
from app.infra.workspace_fingerprint import FingerprintError, compute_fingerprint, is_current_version
from app.infra.workspace_guard import resolve_project_root
from app.models.tables import Project, Task, TaskRun
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.services.lease_service import Lease, LeaseService
from app.services.project_terminal import UNMEASURED, FingerprintProof, FingerprintProofStale, proof_matches
from app.validation import policies as P
from app.validation.exceptions import FailClosed

log = logging.getLogger("orchestrator.project_lock")


class StepGate(StrEnum):
    READY = "READY"                  # this task owns the project write lock: the step may start
    BLOCKED = "BLOCKED"              # another task owns it (or the state keeps changing): defer, consume nothing
    GATE_OPENED = "GATE_OPENED"      # a PROJECT_DIRTY recovery gate was opened and the lease released


class ProjectLockService:
    def __init__(self, db: Database, leases: LeaseService, clock=lambda: datetime.now(UTC)):
        self.db, self.leases, self._now = db, leases, clock

    # --- measuring (outside any transaction) ---------------------------------------------------------------
    async def _measure(self, project: Project, owner: uuid.UUID | None, epoch: int):
        try:
            root = resolve_project_root(project.workspace_path)
            fp = await asyncio.to_thread(compute_fingerprint, root)
        except (FingerprintError, FailClosed, OSError) as exc:
            log.warning("project %s could not be fingerprinted: %s", project.id, exc)
            return UNMEASURED
        return FingerprintProof(project.id, owner, epoch, fp.value, fp.mode)

    async def measure_project(self, project_id: uuid.UUID):
        """Proof of the project's CURRENT workspace, bound to the owner and write_epoch seen at the start."""
        async with self.db.transaction() as s:
            project = await ProjectRepository(s).get(project_id)
        if project is None:
            return UNMEASURED
        return await self._measure(project, project.write_owner_task_id, project.write_epoch)

    async def measure_for_task(self, task_id: uuid.UUID):
        """Proof for a terminal transition. None when there is nothing to prove: no project, or the task is not
        the write owner (the cheap check happens before any hashing)."""
        async with self.db.transaction() as s:
            task = await TaskRepository(s).get(task_id)
            if task is None or task.project_id is None:
                return None
            project = await ProjectRepository(s).get(task.project_id)
        if project is None or project.write_owner_task_id != task_id:
            return None
        return await self._measure(project, task_id, project.write_epoch)

    # --- read-only steps -------------------------------------------------------------------------------------
    async def read_state(self, task_id: uuid.UUID) -> tuple[uuid.UUID | None, int, bool] | None:
        """(foreign_owner_task_id or None, write_epoch, foreign_owner_is_running) for a project task; None otherwise.
        `running` = the other owner holds a live lease, i.e. it may be changing the workspace right now."""
        async with self.db.transaction() as s:
            task = await TaskRepository(s).get(task_id)
            if task is None or task.project_id is None:
                return None
            project = await ProjectRepository(s).get(task.project_id)
            owner = project.write_owner_task_id
            if owner is None or owner == task_id:
                return None, project.write_epoch, False
            live = (await s.execute(select(TaskRun.id).where(
                TaskRun.task_id == owner, TaskRun.status == "RUNNING", TaskRun.lease_expires_at > self._now()))).first()
            return owner, project.write_epoch, live is not None

    # --- ownership checks (inside the caller's transaction) ------------------------------------------------------
    async def assert_owner(self, session, task: Task) -> None:
        """Every workspace-changing command is issued by the write owner and nobody else."""
        if task.project_id is None:
            return
        project = await ProjectRepository(session).get(task.project_id)
        if project is None or project.write_owner_task_id != task.id:
            raise FailClosed("this task does not hold the project write lock", {"project_id": str(task.project_id)})

    async def on_mutating_begin(self, session, task: Task) -> None:
        """Called inside the step-begin transaction: verifies ownership and starts a new write generation."""
        if task.project_id is None:
            return
        repo = ProjectRepository(session)
        project = await repo.lock(task.project_id)
        if project is None or project.write_owner_task_id != task.id:
            raise FailClosed("this task does not hold the project write lock", {"project_id": str(task.project_id)})
        await repo.bump_epoch(project.id)

    # --- pre-step gate (T3a) ---------------------------------------------------------------------------------------
    async def ensure_for_step(self, lease: Lease, task_id: uuid.UUID, step_key: str, gates) -> StepGate:
        """Acquire-or-verify the project write lock before a workspace-changing step. `gates` is the
        HumanGateService used to open the PROJECT_DIRTY recovery gate."""
        for _ in range(P.PROOF_MAX_ATTEMPTS):
            async with self.db.transaction() as s:
                task = await TaskRepository(s).get(task_id)
                project = await ProjectRepository(s).get(task.project_id) if task and task.project_id else None
            if project is None:
                return StepGate.READY                                    # legacy task: private workspace, no lock
            if project.write_owner_task_id not in (None, task_id):
                return StepGate.BLOCKED
            proof = None
            if project.write_owner_task_id is None and project.workspace_state == "CLEAN":
                proof = await self.measure_project(project.id)           # outside any transaction
                if proof is UNMEASURED:
                    await self._mark_dirty_unmeasured(project.id)        # cannot prove a baseline: fail closed
            try:
                return await self._acquire_once(lease, task_id, step_key, gates, proof)
            except FingerprintProofStale:
                continue                                                 # owner/epoch moved: re-measure, retry
        return StepGate.BLOCKED

    async def _mark_dirty_unmeasured(self, project_id: uuid.UUID) -> None:
        async with self.db.transaction() as s:
            project = await ProjectRepository(s).lock(project_id)
            if project.write_owner_task_id is None and project.workspace_state == "CLEAN":
                await ProjectRepository(s).mark_dirty(project_id)

    async def _acquire_once(self, lease: Lease, task_id: uuid.UUID, step_key: str, gates, proof) -> StepGate:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, lease)
            repo = ProjectRepository(s)
            project = await repo.lock(task.project_id)
            if project.write_owner_task_id == task_id:
                return StepGate.READY                                    # re-entry: baselines are never overwritten
            if project.write_owner_task_id is not None:
                return StepGate.BLOCKED
            if project.workspace_state != "CLEAN":
                await self._open_dirty_gate(s, gates, lease, task, project, step_key)
                return StepGate.GATE_OPENED
            if not isinstance(proof, FingerprintProof) or not proof_matches(project, proof):
                raise FingerprintProofStale("project changed while the workspace was being measured")
            await repo.set_owner(project.id, task_id, proof.fingerprint)
            return StepGate.READY

    async def _open_dirty_gate(self, s, gates, lease: Lease, task: Task, project: Project, step_key: str) -> None:
        """T8: PROJECT_DIRTY recovery gate (idempotent), task WAITING_APPROVAL, lease returned. No step attempt."""
        pending = [a for a in await ApprovalRepository(s).pending_for_task(task.id)
                   if (a.target or {}).get("reason_code") == P.RECOVERY_REASON_PROJECT_DIRTY]
        if not pending:
            await gates.open_recovery(
                s, task, None, reason="the project workspace has unresolved changes from an earlier task",
                checkpoint_step_key=step_key, reason_code=P.RECOVERY_REASON_PROJECT_DIRTY,
                options=P.PROJECT_DIRTY_OPTIONS,
                target_extra={"project_id": str(project.id), "dirty_epoch": project.write_epoch,
                              "baseline_ref": (project.baseline_fingerprint or "")[-16:]},
                detail={"project": project.name, "baseline_version_ok": is_current_version(project.baseline_fingerprint)})
        await self.leases.release(lease, "WAITING_APPROVAL", session=s)

    # --- reconcile (T7) ---------------------------------------------------------------------------------------------
    async def reconcile(self) -> int:
        """A write lock whose owner task is already terminal can only be a crash leftover. Release it with the same
        CLEAN/DIRTY rule as a normal terminal (optimistic retry; never CLEAN without a valid measurement)."""
        async with self.db.transaction() as s:
            stuck = [(p.id, p.write_owner_task_id) for p in await ProjectRepository(s).owned_by_finished_tasks()]
        done = 0
        for project_id, owner in stuck:
            for _ in range(P.PROOF_MAX_ATTEMPTS):
                proof = await self.measure_project(project_id)
                if await self._reconcile_once(project_id, owner, proof):
                    done += 1
                    break
            else:
                await self._reconcile_once(project_id, owner, UNMEASURED)
                done += 1
        return done

    async def _reconcile_once(self, project_id: uuid.UUID, owner: uuid.UUID, proof) -> bool:
        async with self.db.transaction() as s:
            repo = ProjectRepository(s)
            project = await repo.lock(project_id)
            if project.write_owner_task_id != owner:
                return True                                              # somebody else already released it
            if isinstance(proof, FingerprintProof):
                if not proof_matches(project, proof):
                    return False                                         # stale proof: caller re-measures
                clean = is_current_version(project.write_baseline_fingerprint) \
                    and proof.fingerprint == project.write_baseline_fingerprint
            else:
                clean = False
            await repo.release(project_id, state="CLEAN" if clean else "DIRTY_UNRESOLVED", baseline=None)
            return True
