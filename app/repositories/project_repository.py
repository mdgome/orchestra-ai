"""Projects. Repositories never commit; locking reads are explicit (`lock`)."""
import uuid

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.states import TERMINAL_TASK_STATES
from app.models.tables import Project, Task

# Serialises registration so duplicate / nested workspaces cannot be inserted concurrently.
_REGISTRATION_LOCK_KEY = 748_201_077


class ProjectRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def add(self, project: Project) -> Project:
        self.s.add(project)
        await self.s.flush()
        return project

    async def get(self, project_id: uuid.UUID) -> Project | None:
        return (await self.s.execute(select(Project).where(Project.id == project_id))).scalar_one_or_none()

    async def lock(self, project_id: uuid.UUID) -> Project | None:
        """SELECT ... FOR UPDATE: serialises task creation, archiving and write-lock changes."""
        return (await self.s.execute(
            select(Project).where(Project.id == project_id).with_for_update())).scalar_one_or_none()

    async def list(self, *, status: str | None = None, limit: int = 50, offset: int = 0) -> list[Project]:
        stmt = select(Project).order_by(Project.created_at.desc()).limit(limit).offset(offset)
        if status:
            stmt = stmt.where(Project.status == status)
        return list((await self.s.execute(stmt)).scalars())

    async def lock_registration(self) -> None:
        await self.s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _REGISTRATION_LOCK_KEY})

    async def workspace_keys(self) -> list[str]:
        return list((await self.s.execute(select(Project.workspace_key))).scalars())

    async def count_open_tasks(self, project_id: uuid.UUID) -> int:
        terminal = [str(s) for s in TERMINAL_TASK_STATES]
        return (await self.s.execute(select(func.count()).select_from(Task).where(
            Task.project_id == project_id, Task.status.notin_(terminal)))).scalar_one()

    # --- write lock / baselines (callers hold the project row lock) -----------------------------------------
    async def set_owner(self, project_id: uuid.UUID, task_id: uuid.UUID, fingerprint: str) -> None:
        """First acquisition: owner, write baseline AND the accepted baseline are the measured state (CLEAN)."""
        await self.s.execute(update(Project).where(Project.id == project_id).values(
            write_owner_task_id=task_id, write_baseline_fingerprint=fingerprint, baseline_fingerprint=fingerprint,
            workspace_state="CLEAN", write_epoch=Project.write_epoch + 1, updated_at=func.now()))

    async def release(self, project_id: uuid.UUID, *, state: str, baseline: str | None) -> None:
        """Owner and write baseline are always cleared together (ck_projects_write_owner_baseline)."""
        values: dict = {"write_owner_task_id": None, "write_baseline_fingerprint": None, "workspace_state": state,
                        "write_epoch": Project.write_epoch + 1, "updated_at": func.now()}
        if baseline is not None:
            values["baseline_fingerprint"] = baseline
        await self.s.execute(update(Project).where(Project.id == project_id).values(**values))

    async def mark_dirty(self, project_id: uuid.UUID) -> None:
        await self.s.execute(update(Project).where(Project.id == project_id).values(
            workspace_state="DIRTY_UNRESOLVED", write_epoch=Project.write_epoch + 1, updated_at=func.now()))

    async def accept_baseline(self, project_id: uuid.UUID, fingerprint: str) -> None:
        await self.s.execute(update(Project).where(Project.id == project_id).values(
            baseline_fingerprint=fingerprint, workspace_state="CLEAN", write_epoch=Project.write_epoch + 1,
            updated_at=func.now()))

    async def bump_epoch(self, project_id: uuid.UUID) -> None:
        await self.s.execute(update(Project).where(Project.id == project_id).values(
            write_epoch=Project.write_epoch + 1, updated_at=func.now()))

    async def owned_by_finished_tasks(self) -> list[Project]:
        """Projects whose write owner is already terminal: only possible through a crash window (reconcile)."""
        terminal = [str(s) for s in TERMINAL_TASK_STATES]
        rows = await self.s.execute(select(Project).join(Task, Task.id == Project.write_owner_task_id)
                                    .where(Task.status.in_(terminal)))
        return list(rows.scalars())

    async def update_fields(self, project_id: uuid.UUID, **values) -> None:
        await self.s.execute(update(Project).where(Project.id == project_id).values(
            updated_at=func.now(), row_version=Project.row_version + 1, **values))
