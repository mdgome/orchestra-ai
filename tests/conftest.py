import asyncio
import uuid

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from app.db import Database, create_engine
from tests.pg import EmbeddedPg


def alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def pg():
    server = EmbeddedPg()
    yield server
    server.stop()


async def _admin(pg: EmbeddedPg):
    return await asyncpg.connect(pg.sync_uri)


@pytest_asyncio.fixture(scope="session")
async def template_db(pg):
    """Schema built by the real Alembic migration, then cloned per test."""
    conn = await _admin(pg)
    await conn.execute("CREATE DATABASE orch_template")
    await conn.close()
    await asyncio.to_thread(command.upgrade, alembic_config(pg.async_url("orch_template")), "head")
    return "orch_template"


@pytest_asyncio.fixture
async def db(pg, template_db):
    name = f"orch_{uuid.uuid4().hex[:12]}"
    conn = await _admin(pg)
    await conn.execute(f'CREATE DATABASE {name} TEMPLATE {template_db}')
    await conn.close()
    database = Database(create_engine(pg.async_url(name)))
    yield database
    await database.dispose()
    conn = await _admin(pg)
    await conn.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')
    await conn.close()
