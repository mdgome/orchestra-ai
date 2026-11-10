"""Input fingerprints. Each step hashes only what it depends on (plan P0-3), never a global version."""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.states import STEP_PLAN, StepState
from app.models.tables import Task, TaskStep
from app.services.hashing import stable_hash
from app.validation import policies as P

REVIEW_POLICY_HASH = stable_hash(P.DESIGN_REVIEW_CHECKLIST)


def profile_part(task: Task) -> tuple:
    """What a task's profile adds to an input hash: nothing for a task without one, so its hashes (and the reuse of its
    stored steps) stay exactly as they were before profiles existed."""
    return (task.profile_hash,) if task.profile_hash else ()


def profile_criteria_of(task: Task) -> tuple:
    """The completion criteria of the task's profile snapshot ((), when it has no profile or none are defined)."""
    return tuple((task.profile_snapshot or {}).get("completion_criteria") or ()) if task.profile_hash else ()


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
    basis = {"requirements_hash": task.requirements_hash,
             "plan_hash": await current_plan_hash(session, task.id),
             "decisions_hash": task.decisions_hash}
    if task.profile_hash:                    # a task without a profile keeps its original basis, byte for byte
        basis["profile_hash"] = task.profile_hash
    return basis


def design_review_basis(requirements_hash: str, plan_hash: str, decisions_hash: str,
                        review_policy_hash: str = REVIEW_POLICY_HASH, profile_hash: str | None = None) -> dict:
    """design_review_basis: every input a Design Review PASS depends on. `profile_hash` is part of it only for a task
    that has a profile, so the basis (and every reuse decision) of a task without one is unchanged."""
    parts = {"requirements_hash": requirements_hash, "plan_hash": plan_hash,
             "decisions_hash": decisions_hash, "review_policy_hash": review_policy_hash}
    if profile_hash:
        parts["profile_hash"] = profile_hash
    return {**parts, "basis_hash": stable_hash(parts)}
