"""Input fingerprints. Each step hashes only what it depends on (plan P0-3), never a global version."""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.states import STEP_PLAN, StepState
from app.models.tables import Task, TaskStep
from app.services.hashing import stable_hash
from app.validation import policies as P

REVIEW_POLICY_HASH = stable_hash(P.DESIGN_REVIEW_CHECKLIST)


async def current_plan_hash(session: AsyncSession, task_id: uuid.UUID, plan_revision: int | None = None) -> str:
    """Hash of the latest SUCCEEDED Plan Artifact ('' when none exists yet)."""
    stmt = select(TaskStep.output_hash).where(
        TaskStep.task_id == task_id, TaskStep.step_key == STEP_PLAN, TaskStep.status == str(StepState.SUCCEEDED))
    if plan_revision is not None:
        stmt = stmt.where(TaskStep.plan_revision == plan_revision)
    stmt = stmt.order_by(TaskStep.plan_revision.desc(), TaskStep.created_at.desc()).limit(1)
    return (await session.execute(stmt)).scalar() or ""


async def current_basis(session: AsyncSession, task: Task) -> dict:
    """What a human decision was made *against*. A decision made on a different basis is stale."""
    return {"requirements_hash": task.requirements_hash,
            "plan_hash": await current_plan_hash(session, task.id),
            "decisions_hash": task.decisions_hash}


def design_review_basis(requirements_hash: str, plan_hash: str, decisions_hash: str,
                        review_policy_hash: str = REVIEW_POLICY_HASH) -> dict:
    """design_review_basis: every input a Design Review PASS depends on."""
    parts = {"requirements_hash": requirements_hash, "plan_hash": plan_hash,
             "decisions_hash": decisions_hash, "review_policy_hash": review_policy_hash}
    return {**parts, "basis_hash": stable_hash(parts)}
