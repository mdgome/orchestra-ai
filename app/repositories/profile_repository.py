"""Project profile versions. Rows are immutable (a database trigger): this repository only ever inserts and reads."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import ProjectProfile


class ProfileRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def add(self, row: ProjectProfile) -> ProjectProfile:
        self.s.add(row)
        await self.s.flush()
        return row

    async def get(self, project_id: uuid.UUID, version: int) -> ProjectProfile | None:
        return (await self.s.execute(select(ProjectProfile).where(
            ProjectProfile.project_id == project_id, ProjectProfile.version == version))).scalar_one_or_none()

    async def max_version(self, project_id: uuid.UUID) -> int:
        return (await self.s.execute(select(func.max(ProjectProfile.version)).where(
            ProjectProfile.project_id == project_id))).scalar() or 0

    async def page(self, project_id: uuid.UUID, limit: int, offset: int) -> tuple[list[ProjectProfile], int]:
        total = (await self.s.execute(select(func.count()).select_from(ProjectProfile).where(
            ProjectProfile.project_id == project_id))).scalar() or 0
        rows = await self.s.execute(select(ProjectProfile).where(ProjectProfile.project_id == project_id)
                                    .order_by(ProjectProfile.version.desc()).limit(limit).offset(offset))
        return list(rows.scalars()), total
