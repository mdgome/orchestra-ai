import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models.tables import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # Tests / tooling can pass the URL via -x url=... or config attributes.
    return (context.get_x_argument(as_dictionary=True).get("url")
            or config.get_main_option("sqlalchemy.url")
            or get_settings().database_url)


def _do_run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
