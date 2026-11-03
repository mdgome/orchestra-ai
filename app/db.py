"""Engine / session helpers. Services own transaction boundaries (repositories never commit)."""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine(database_url: str, pool_size: int = 10, max_overflow: int = 10) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow)


class Database:
    """Thin holder so services can open short, explicit transactions."""

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self._factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """One PostgreSQL transaction: commit on success, roll back on any exception."""
        async with self._factory() as session:
            async with session.begin():
                yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
