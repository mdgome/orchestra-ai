"""Task baselines: insert-once rows. Queries only; the caller owns the transaction (and holds the project row lock)."""
import enum
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import TaskBaseline

log = logging.getLogger("orchestrator.baseline")


class BaselineWriteResult(enum.Enum):
    INSERTED = "INSERTED"
    IDEMPOTENT = "IDEMPOTENT"                                  # the same acquisition was already recorded
    STATUS_CONFLICT_PRESERVED = "STATUS_CONFLICT_PRESERVED"    # same acquisition, different status: the existing row stays


class BaselineConflict(Exception):
    """A row exists for this task but describes a DIFFERENT acquisition: never treated as success."""


def same_identity(existing: TaskBaseline, wanted: dict) -> bool:
    """identity = (task_id, project_id, proof_epoch, acquired_epoch, proof_fingerprint). status, the manifest payload and
    recorded_epoch are NOT part of it. A row whose acquired_epoch is NULL (a recovery that could not prove the original
    acquisition epoch) has its own identity: (task_id, project_id, proof_fingerprint, origin, reason); it never equals a
    row that has epochs."""
    if existing.project_id != wanted["project_id"] or existing.proof_fingerprint != wanted["proof_fingerprint"]:
        return False
    if (existing.acquired_epoch is None) != (wanted["acquired_epoch"] is None):
        return False
    if existing.acquired_epoch is None:
        return (existing.origin == wanted["origin"] == "REENTRY_RECOVERY"
                and existing.unavailable_reason == wanted["unavailable_reason"] == "BASELINE_MISSING_AT_REENTRY")
    return existing.proof_epoch == wanted["proof_epoch"] and existing.acquired_epoch == wanted["acquired_epoch"]


class TaskBaselineRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def get(self, task_id: uuid.UUID) -> TaskBaseline | None:
        return (await self.s.execute(select(TaskBaseline).where(TaskBaseline.task_id == task_id))).scalar_one_or_none()

    async def insert_once(self, values: dict) -> BaselineWriteResult:
        """INSERT ... ON CONFLICT (task_id) DO NOTHING, then the row that is really there is compared with what was meant:
        the same identity is idempotent success (an existing AVAILABLE is never replaced by UNAVAILABLE, an existing
        UNAVAILABLE is never promoted), a different identity raises BaselineConflict (the caller's transaction rolls back)."""
        inserted = (await self.s.execute(insert(TaskBaseline).values(**values)
                                         .on_conflict_do_nothing(index_elements=["task_id"])
                                         .returning(TaskBaseline.task_id))).first()
        if inserted is not None:
            return BaselineWriteResult.INSERTED
        existing = await self.get(values["task_id"])
        if existing is None or not same_identity(existing, values):
            raise BaselineConflict("a baseline of a different acquisition already exists for this task")
        if existing.status != values["status"]:
            log.warning("baseline_status_conflict task=%s kept=%s ignored=%s", values["task_id"], existing.status,
                        values["status"])
            return BaselineWriteResult.STATUS_CONFLICT_PRESERVED
        return BaselineWriteResult.IDEMPOTENT
