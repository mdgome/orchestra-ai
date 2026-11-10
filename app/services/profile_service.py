"""Versioned project profiles.

A profile is DATA that tunes the prompts and adds completion criteria for one project. It is immutable per version: a change
is a new version, made active by moving `projects.profile_version`, in ONE transaction under the project row lock. A task
copies the active version (number, hash and content) when it is created, so a later change never alters a task that is
already running. A profile carries no permission, tool, model, path or interpreter; those belong to trusted policy.
"""
import copy
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Database
from app.models.tables import ProjectProfile
from app.repositories.profile_repository import ProfileRepository
from app.repositories.project_repository import ProjectRepository
from app.services.hashing import stable_hash
from app.services.prompts import LEGACY_ARCHITECTURE_RULES
from app.validation import validators as V
from app.validation.exceptions import Conflict, NotFound

# v1 of a NEW project that gives no profile: no architecture is imposed on it.
NEUTRAL_CONTENT = {
    "schema_version": 1, "description": "", "languages": [], "frameworks": [], "architecture_rules": [],
    "coding_rules": [], "completion_criteria": [], "test_policy_ref": "project",
}
# v1 of the projects that existed before profiles: the original built-in rules (migration 0003 backfills exactly this).
LEGACY_CONTENT = {**NEUTRAL_CONTENT, "architecture_rules": list(LEGACY_ARCHITECTURE_RULES)}


def hash_profile(content: dict) -> str:
    return stable_hash("project_profile", content)


async def create_initial(session: AsyncSession, project_id: uuid.UUID, content: dict | None) -> ProjectProfile:
    """v1 of a project that is being created, in the caller's transaction. `content` is already validated; None means
    the neutral profile. The caller has just inserted the project (pointer still NULL); this sets it."""
    body = copy.deepcopy(NEUTRAL_CONTENT if content is None else content)
    row = await ProfileRepository(session).add(ProjectProfile(
        id=uuid.uuid4(), project_id=project_id, version=1, content=body, content_hash=hash_profile(body)))
    await ProjectRepository(session).update_fields(project_id, profile_version=1)
    return row


def _view(row: ProjectProfile, active_version: int | None) -> dict:
    return {"version": row.version, "content": row.content, "content_hash": row.content_hash,
            "created_at": row.created_at, "active": row.version == active_version}


class ProfileService:
    def __init__(self, db: Database):
        self.db = db

    async def add_version(self, project_id: uuid.UUID, raw: dict) -> dict:
        """Creates AND activates a new version, unless the content is identical to the active one (then nothing is
        written: `created` is False). The project row is locked, so concurrent calls and task creation are serialised."""
        content = V.validate_profile(raw)
        digest = hash_profile(content)
        async with self.db.transaction() as s:
            project = await ProjectRepository(s).lock(project_id)
            if project is None:
                raise NotFound("project not found")
            if project.status != "ACTIVE":
                raise Conflict("project is not ACTIVE", {"status": project.status})
            repo = ProfileRepository(s)
            active = await repo.get(project_id, project.profile_version) if project.profile_version else None
            if active is not None and active.content_hash == digest:
                return {**_view(active, active.version), "created": False}
            row = await repo.add(ProjectProfile(id=uuid.uuid4(), project_id=project_id,
                                                version=await repo.max_version(project_id) + 1, content=content,
                                                content_hash=digest))
            await ProjectRepository(s).update_fields(project_id, profile_version=row.version)
            return {**_view(row, row.version), "created": True}

    async def active(self, project_id: uuid.UUID) -> dict:
        async with self.db.transaction() as s:
            project = await ProjectRepository(s).get(project_id)
            if project is None:
                raise NotFound("project not found")
            row = await ProfileRepository(s).get(project_id, project.profile_version) if project.profile_version else None
            if row is None:
                raise NotFound("the project has no profile")
            return _view(row, row.version)

    async def versions(self, project_id: uuid.UUID, limit: int, offset: int) -> dict:
        async with self.db.transaction() as s:
            project = await ProjectRepository(s).get(project_id)
            if project is None:
                raise NotFound("project not found")
            rows, total = await ProfileRepository(s).page(project_id, limit, offset)
            return {"items": [_view(r, project.profile_version) for r in rows], "total": total, "limit": limit,
                    "offset": offset}
