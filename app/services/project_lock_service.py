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
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select

from app.db import Database
from app.infra import changes_manifest as CM
from app.infra.deadline import Deadline, DeadlineExceeded
from app.infra.workspace_fingerprint import FingerprintError, compute_fingerprint, is_current_version
from app.infra.workspace_guard import resolve_project_root
from app.models.states import STEP_DEVELOP, STEP_TEST
from app.models.tables import Project, Task, TaskRun
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.baseline_repository import BaselineConflict, TaskBaselineRepository
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


@dataclass(frozen=True)
class SupplementalOk:
    measured: CM.Measured
    encoded: CM.Encoded


@dataclass(frozen=True)
class SupplementalFailure:
    """The Changes manifest could not be collected. Never a reason to stop a task: the lock safety rests on the fingerprint
    proof, not on this; the baseline row is simply recorded as UNAVAILABLE with this reason."""
    reason: str


@dataclass(frozen=True)
class LockMeasurement:
    """Two results that fail differently, kept apart. `proof` (the WORKSPACE_FINGERPRINT_V1 proof) decides whether the task
    may own the project at all (fail closed); `supplemental` only decides whether Changes can be shown for it."""
    proof: object                                   # FingerprintProof | UNMEASURED
    supplemental: SupplementalOk | SupplementalFailure


class _BaselineRecoveryNeeded(Exception):
    """The task already owns the lock but has no baseline row and has not changed anything yet."""


_FINGERPRINT_VALUE = re.compile(r"WORKSPACE_FINGERPRINT_V[0-9]+:[0-9a-f]{64}")
MISSING_AT_REENTRY = "BASELINE_MISSING_AT_REENTRY"


def baseline_values(task_id: uuid.UUID, project_id: uuid.UUID, *, origin: str, proof_epoch: int | None,
                    acquired_epoch: int | None, recorded_epoch: int, fingerprint: str,
                    supplemental: SupplementalOk | SupplementalFailure | None, reason: str | None = None) -> dict:
    """The columns of a task_baselines row. AVAILABLE carries the whole manifest; UNAVAILABLE carries a reason and no payload
    (the CHECK constraints reject any mix)."""
    base = {"task_id": task_id, "project_id": project_id, "origin": origin, "proof_epoch": proof_epoch,
            "acquired_epoch": acquired_epoch, "recorded_epoch": recorded_epoch, "proof_fingerprint": fingerprint}
    if isinstance(supplemental, SupplementalOk) and reason is None:
        m, e = supplemental.measured, supplemental.encoded
        return {**base, "status": "AVAILABLE", "mode": m.mode, "head_kind": m.head_kind, "head_sha": m.head_sha,
                "manifest": e.data, "manifest_version": CM.MANIFEST_VERSION, "uncompressed_bytes": e.uncompressed_bytes,
                "manifest_sha256": e.sha256, "file_count": e.file_count, "dirty_paths": list(m.dirty_paths),
                "unavailable_reason": None}
    why = reason or (supplemental.reason if isinstance(supplemental, SupplementalFailure) else "UNKNOWN")
    return {**base, "status": "UNAVAILABLE", "mode": None, "head_kind": None, "head_sha": None, "manifest": None,
            "manifest_version": None, "uncompressed_bytes": None, "manifest_sha256": None, "file_count": None,
            "dirty_paths": None, "unavailable_reason": why}


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

    async def _measure_supplemental(self, project: Project) -> SupplementalOk | SupplementalFailure:
        """The Changes manifest, HEAD and dirty paths: a SEPARATE walk with its own schema (the fingerprint algorithm, entries
        and digest are untouched). Any failure becomes a reason code; it can never raise into the lock path."""
        try:
            root = resolve_project_root(project.workspace_path)
            deadline = Deadline(P.CHANGES_BASELINE_DEADLINE_SECONDS)       # cooperative: checked between entries/chunks
            measured = await asyncio.to_thread(CM.measure, root, deadline=deadline)
            return SupplementalOk(measured, CM.encode(measured))
        except CM.ManifestFailure as exc:
            return SupplementalFailure(exc.reason)
        except DeadlineExceeded:
            return SupplementalFailure("DEADLINE")
        except (FailClosed, OSError):
            return SupplementalFailure("FILESYSTEM_ERROR")
        except Exception:  # noqa: BLE001 - a bug here must not stop a task, but must not hide either
            log.exception("project %s: unexpected error while collecting the Changes baseline", project.id)
            return SupplementalFailure("UNEXPECTED_ERROR")

    async def _measure_acquisition(self, project_id: uuid.UUID) -> LockMeasurement:
        """fingerprint BEFORE -> Changes manifest -> fingerprint AFTER. The owner and write_epoch are read first and bound to
        the proof. If the two fingerprints differ the workspace moved while the manifest was measured: the measurement is
        stale (FingerprintProofStale, the caller's bounded retry re-measures). An unmeasurable fingerprint stays UNMEASURED:
        the existing fail-closed path (DIRTY_UNRESOLVED -> PROJECT_DIRTY gate) is unchanged."""
        async with self.db.transaction() as s:
            project = await ProjectRepository(s).get(project_id)
        if project is None:
            return LockMeasurement(UNMEASURED, SupplementalFailure("FINGERPRINT_UNMEASURED"))
        owner, epoch = project.write_owner_task_id, project.write_epoch
        before = await self._measure(project, owner, epoch)
        if before is UNMEASURED:
            return LockMeasurement(UNMEASURED, SupplementalFailure("FINGERPRINT_UNMEASURED"))
        supplemental = await self._measure_supplemental(project)
        after = await self._measure(project, owner, epoch)
        if after is UNMEASURED:
            return LockMeasurement(UNMEASURED, SupplementalFailure("FINGERPRINT_UNMEASURED"))
        if after.fingerprint != before.fingerprint:
            raise FingerprintProofStale("the workspace changed while the Changes baseline was being measured")
        return LockMeasurement(before, supplemental)

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
            try:
                measurement = None
                if project.write_owner_task_id is None and project.workspace_state == "CLEAN":
                    measurement = await self._measure_acquisition(project.id)    # outside any transaction
                    if measurement.proof is UNMEASURED:
                        await self._mark_dirty_unmeasured(project.id)    # cannot prove a baseline: fail closed
                try:
                    return await self._acquire_once(lease, task_id, step_key, gates, measurement)
                except _BaselineRecoveryNeeded:
                    # the owner has no baseline row yet and changed nothing: measure (outside any transaction), then record
                    return await self._recover_once(lease, task_id, await self._measure_acquisition(project.id))
            except FingerprintProofStale:
                continue                                                 # owner/epoch moved: re-measure, retry
        return StepGate.BLOCKED

    async def _mark_dirty_unmeasured(self, project_id: uuid.UUID) -> None:
        async with self.db.transaction() as s:
            project = await ProjectRepository(s).lock(project_id)
            if project.write_owner_task_id is None and project.workspace_state == "CLEAN":
                await ProjectRepository(s).mark_dirty(project_id)

    async def _acquire_once(self, lease: Lease, task_id: uuid.UUID, step_key: str, gates,
                            measurement: LockMeasurement | None) -> StepGate:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, lease)
            repo = ProjectRepository(s)
            project = await repo.lock(task.project_id)
            if project.write_owner_task_id == task_id:
                await self._ensure_baseline(s, task, project)            # re-entry: never READY without a baseline row
                return StepGate.READY                                    # (baselines are never overwritten)
            if project.write_owner_task_id is not None:
                return StepGate.BLOCKED
            if project.workspace_state != "CLEAN":
                await self._open_dirty_gate(s, gates, lease, task, project, step_key)
                return StepGate.GATE_OPENED
            proof = measurement.proof if measurement is not None else None
            if not isinstance(proof, FingerprintProof) or not proof_matches(project, proof):
                raise FingerprintProofStale("project changed while the workspace was being measured")
            # The fingerprint proof is valid, so the task may own the project. set_owner states which epoch it produced
            # (proof epoch + 1) and the baseline row is written in THIS transaction: owner without baseline cannot commit.
            acquired = await repo.set_owner(project.id, task_id, proof.fingerprint, expected_epoch=proof.write_epoch)
            if acquired is None:
                raise FingerprintProofStale("the write epoch moved before the lock was taken")
            await self._insert_baseline(s, baseline_values(
                task_id, project.id, origin="ACQUISITION", proof_epoch=proof.write_epoch, acquired_epoch=acquired,
                recorded_epoch=acquired, fingerprint=proof.fingerprint, supplemental=measurement.supplemental))
            return StepGate.READY

    # --- baseline rows (inside the caller's transaction, project row locked) ---------------------------------------------
    async def _insert_baseline(self, s, values: dict) -> None:
        try:
            await TaskBaselineRepository(s).insert_once(values)
        except BaselineConflict as exc:
            # raised inside the caller's transaction: set_owner (if any) rolls back with it, the task does not run
            raise FailClosed("a baseline of a different acquisition already exists for this task",
                             {"task_id": str(values["task_id"])}) from exc

    @staticmethod
    def _check_epoch(row, project: Project, mutating: bool) -> None:
        """Before any mutating step the epoch is exactly the acquired one; afterwards it only grows (every mutating step
        begin bumps it). A row of a recovery that could not prove its acquisition epoch is only bounded from below."""
        if row.project_id != project.id:
            raise FailClosed("the task baseline belongs to another project", {"task_id": str(row.task_id)})
        if row.acquired_epoch is None:
            ok = project.write_epoch >= row.recorded_epoch
        else:
            ok = project.write_epoch >= row.acquired_epoch if mutating else project.write_epoch == row.acquired_epoch
        if not ok:
            raise FailClosed("the project write epoch does not fit the task baseline",
                             {"task_id": str(row.task_id), "write_epoch": project.write_epoch})

    async def _missing_at_reentry(self, s, task: Task, project: Project) -> None:
        """The first acquisition epoch cannot be proven (a mutating step already ran): an explicit UNAVAILABLE row. The
        acquired epoch is NOT invented from the current one."""
        fingerprint = project.write_baseline_fingerprint or ""
        if not _FINGERPRINT_VALUE.fullmatch(fingerprint):
            raise FailClosed("the project write baseline is not a known fingerprint", {"task_id": str(task.id)})
        await self._insert_baseline(s, baseline_values(
            task.id, project.id, origin="REENTRY_RECOVERY", proof_epoch=None, acquired_epoch=None,
            recorded_epoch=project.write_epoch, fingerprint=fingerprint, supplemental=None, reason=MISSING_AT_REENTRY))

    async def _ensure_baseline(self, s, task: Task, project: Project) -> None:
        row = await TaskBaselineRepository(s).get(task.id)
        mutating = await TaskRepository(s).has_steps(task.id, (STEP_DEVELOP, STEP_TEST))
        if row is not None:
            self._check_epoch(row, project, mutating)
            return
        if mutating:
            await self._missing_at_reentry(s, task, project)
            return
        raise _BaselineRecoveryNeeded()

    async def _recover_once(self, lease: Lease, task_id: uuid.UUID, measurement: LockMeasurement) -> StepGate:
        """The owner has no baseline and has changed nothing: if the freshly measured fingerprint still equals the write
        baseline of the acquisition the workspace is provably unchanged, so the manifest measured now is the start state."""
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            await self.leases.verify(s, lease)
            project = await ProjectRepository(s).lock(task.project_id)
            if project.write_owner_task_id != task_id:
                return StepGate.BLOCKED
            row = await TaskBaselineRepository(s).get(task_id)
            mutating = await TaskRepository(s).has_steps(task_id, (STEP_DEVELOP, STEP_TEST))
            if row is not None:                                          # a concurrent call already recorded it
                self._check_epoch(row, project, mutating)
                return StepGate.READY
            if mutating:
                await self._missing_at_reentry(s, task, project)
                return StepGate.READY
            proof = measurement.proof
            if proof is UNMEASURED:                  # the owner already holds the lock: it must not be stuck behind an
                await self._missing_at_reentry(s, task, project)     # unreadable workspace; an explicit UNAVAILABLE row
                return StepGate.READY
            if not isinstance(proof, FingerprintProof) or not proof_matches(project, proof):
                raise FingerprintProofStale("project changed while the baseline was being recovered")
            if proof.fingerprint != project.write_baseline_fingerprint:
                await self._missing_at_reentry(s, task, project)         # moved since the acquisition: not recoverable
                return StepGate.READY
            await self._insert_baseline(s, baseline_values(
                task_id, project.id, origin="REENTRY_RECOVERY", proof_epoch=project.write_epoch,
                acquired_epoch=project.write_epoch, recorded_epoch=project.write_epoch, fingerprint=proof.fingerprint,
                supplemental=measurement.supplemental))
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
