"""finalize_task_with_proof: the single entry point for terminal transitions of project tasks.

It owns the optimistic-retry protocol:
  1. (outside any transaction) measure a FingerprintProof for the task's project workspace;
  2. short transaction: lock the task row, then the project row;
  3. run the caller's `work` (it calls change_task_state(..., project_proof=proof); project_terminal verifies the
     proof under the project lock and cleans up the project);
  4. commit.
`FingerprintProofStale` rolls the whole transaction back; the proof is measured again and everything is retried.
After PROOF_MAX_ATTEMPTS the UNMEASURED path runs ONCE in a single short transaction: the terminal change, the
project cleanup (DIRTY_UNRESOLVED), the lease release and the request completion all commit together, so no
intermediate state is visible. A task that is already terminal is a no-op (idempotent).
change_task_state / project_terminal never open a transaction and never measure.
"""
import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.models.states import TERMINAL_TASK_STATES, TaskState
from app.models.tables import Task
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.services.project_lock_service import ProjectLockService
from app.services.project_terminal import UNMEASURED, FingerprintProofStale
from app.validation import policies as P

log = logging.getLogger("orchestrator.finalizer")
Work = Callable[[AsyncSession, Task, object], Awaitable[None]]


class TaskFinalizer:
    def __init__(self, db: Database, locks: ProjectLockService):
        self.db, self.locks = db, locks

    async def finalize_task_with_proof(self, task_id: uuid.UUID, work: Work) -> None:
        for _ in range(P.PROOF_MAX_ATTEMPTS):
            proof = await self.locks.measure_for_task(task_id)          # None = nothing to prove (legacy / not owner)
            try:
                await self._attempt(task_id, work, proof)
                return
            except FingerprintProofStale:
                log.info("fingerprint proof for task %s went stale; measuring again", task_id)
        log.warning("task %s could not be measured consistently; finishing it with an UNMEASURED proof", task_id)
        await self._attempt(task_id, work, UNMEASURED)

    async def _attempt(self, task_id: uuid.UUID, work: Work, proof) -> None:
        async with self.db.transaction() as s:
            task = await TaskRepository(s).lock(task_id)
            if task is None:
                raise LookupError("task not found")
            if task.project_id is not None:
                await ProjectRepository(s).lock(task.project_id)        # lock order: task row, then project row
            if TaskState(task.status) in TERMINAL_TASK_STATES:
                return
            await work(s, task, proof)
