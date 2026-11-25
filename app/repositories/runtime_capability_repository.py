"""runtime_capabilities: the process-tracking capability shared with API-only instances (design 6.2). Queries only; the caller
owns the transaction. The row is the value of one key (`process_tracking`); the incarnation guard keeps a late write of an
older Runner incarnation from overwriting a newer one."""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import RuntimeCapability


class RuntimeCapabilityRepository:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def get(self, key: str) -> RuntimeCapability | None:
        return (await self.s.execute(select(RuntimeCapability).where(RuntimeCapability.key == key))).scalar_one_or_none()

    async def upsert(self, key: str, value: dict, *, expected_incarnation_id: str | None = None,
                     now: datetime | None = None) -> bool:
        """INSERT ... ON CONFLICT (key) DO UPDATE. Without `expected_incarnation_id` the write is unconditional: it is the
        mandatory UNKNOWN upsert of a Runner that just took the advisory lock, which invalidates every earlier value. With it
        the update applies only while the stored `incarnation_id` is still that one (the result upsert of the same
        incarnation, design 6.2 step 4); a row of another incarnation stays untouched and False is returned. A missing row is
        inserted either way. True means this call wrote the row."""
        stamp = func.now() if now is None else now
        stmt = insert(RuntimeCapability).values(key=key, value=value, updated_at=stamp)
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"], set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at},
            where=None if expected_incarnation_id is None
            else RuntimeCapability.value["incarnation_id"].astext == expected_incarnation_id)
        written = (await self.s.execute(stmt.returning(RuntimeCapability.key))).first()
        return written is not None
