import asyncio
import uuid

import asyncpg
import pytest
from alembic import command

from tests.conftest import alembic_config


async def test_migration_up_down_up_and_model_matches(pg):
    name = f"mig_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg.sync_uri)
    await admin.execute(f"CREATE DATABASE {name}")
    await admin.close()
    cfg = alembic_config(pg.async_url(name))
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.downgrade, cfg, "base")
        await asyncio.to_thread(command.upgrade, cfg, "head")
        # alembic check raises if the ORM models and the migration disagree
        await asyncio.to_thread(command.check, cfg)
    finally:
        admin = await asyncpg.connect(pg.sync_uri)
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()


async def test_messages_table_exists_and_has_no_inbound_foreign_keys(db):
    from sqlalchemy import text
    async with db.transaction() as s:
        exists = (await s.execute(text("select to_regclass('public.messages')"))).scalar()
        inbound = (await s.execute(text(
            "select count(*) from information_schema.table_constraints tc "
            "join information_schema.constraint_column_usage ccu on tc.constraint_name = ccu.constraint_name "
            "where tc.constraint_type='FOREIGN KEY' and ccu.table_name='messages'"))).scalar()
    assert exists == "messages"
    assert inbound == 0  # deleting messages can never touch tasks/executions/approvals


async def test_state_transitions_are_append_only(db):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    async with db.transaction() as s:
        cid, tid = uuid.uuid4(), uuid.uuid4()
        await s.execute(text("insert into conversations(id,status) values (:c,'ACTIVE')"), {"c": cid})
        await s.execute(text(
            "insert into tasks(id,conversation_id,requirements,requirements_hash,status,limits,test_policy,"
            "test_policy_hash,plan_revision,workflow_iteration,replan_count,cli_invocations,run_seconds,decisions_hash,row_version)"
            " values (:t,:c,'r','h','PENDING','{}','{}','h',0,0,0,0,0,'',0)"), {"t": tid, "c": cid})
        await s.execute(text("insert into state_transitions(task_id,kind,to_state) values (:t,'TASK_STATE','PENDING')"), {"t": tid})
    with pytest.raises(DBAPIError):
        async with db.transaction() as s:
            await s.execute(text("update state_transitions set to_state='X' where task_id=:t"), {"t": tid})
    with pytest.raises(DBAPIError):
        async with db.transaction() as s:
            await s.execute(text("delete from state_transitions where task_id=:t"), {"t": tid})
