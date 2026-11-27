"""E1 schema: tasks.mode / idempotency_key / request_fingerprint and their constraints (migration 0006)."""
import asyncio
import uuid

import asyncpg
import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import alembic_config

TASK_SQL = ("insert into tasks(id,conversation_id,project_id,requirements,requirements_hash,status,limits,test_policy,"
            "test_policy_hash,plan_revision,workflow_iteration,replan_count,cli_invocations,run_seconds,decisions_hash,"
            "row_version{extra_cols}) values (:t,:c,:p,'r','h','PENDING','{{}}','{{}}','h',0,0,0,0,0,'',0{extra_vals})")


async def _project_and_conversation(db, name):
    pid, cid = uuid.uuid4(), uuid.uuid4()
    async with db.transaction() as s:
        await s.execute(text("insert into projects(id,name,workspace_path,workspace_key) values (:i,:n,:w,:w)"),
                        {"i": pid, "n": name, "w": f"/w/{name}"})
        await s.execute(text("insert into conversations(id,status) values (:c,'ACTIVE')"), {"c": cid})
    return pid, cid


async def _insert(db, cid, pid, *, mode=None, key=None, fingerprint=None):
    cols, vals, params = "", "", {"t": uuid.uuid4(), "c": cid, "p": pid}
    for column, value in (("mode", mode), ("idempotency_key", key), ("request_fingerprint", fingerprint)):
        if value is not None:
            cols, vals, params[column] = cols + f",{column}", vals + f",:{column}", value
    async with db.transaction() as s:
        await s.execute(text(TASK_SQL.format(extra_cols=cols, extra_vals=vals)), params)
    return params["t"]


async def test_a_task_that_names_no_mode_is_a_workflow_task_without_a_key(db):
    pid, cid = await _project_and_conversation(db, "plain")
    task = await _insert(db, cid, pid)
    async with db.transaction() as s:
        row = (await s.execute(text("select mode, idempotency_key, request_fingerprint from tasks where id=:t"),
                               {"t": task})).one()
    assert tuple(row) == ("WORKFLOW", None, None)


async def test_a_test_only_task_may_carry_a_key_and_a_fingerprint(db):
    pid, cid = await _project_and_conversation(db, "run")
    task = await _insert(db, cid, pid, mode="TEST_ONLY", key="k1", fingerprint="f" * 64)
    async with db.transaction() as s:
        row = (await s.execute(text("select mode, idempotency_key from tasks where id=:t"), {"t": task})).one()
    assert tuple(row) == ("TEST_ONLY", "k1")


async def test_the_database_rejects_an_unknown_mode(db):
    pid, cid = await _project_and_conversation(db, "badmode")
    with pytest.raises(IntegrityError, match="ck_tasks_mode"):
        await _insert(db, cid, pid, mode="SOMETHING")


async def test_the_database_rejects_a_test_only_task_without_a_project(db):
    _, cid = await _project_and_conversation(db, "noproject")
    with pytest.raises(IntegrityError, match="ck_tasks_test_only_project"):
        await _insert(db, cid, None, mode="TEST_ONLY")
    await _insert(db, cid, None)                                           # an ordinary Task still needs no project


async def test_only_a_test_only_task_can_carry_an_idempotency_key(db):
    pid, cid = await _project_and_conversation(db, "workflowkey")
    with pytest.raises(IntegrityError, match="ck_tasks_idempotency_test_only"):
        await _insert(db, cid, pid, key="k1")
    with pytest.raises(IntegrityError, match="ck_tasks_idempotency_test_only"):
        await _insert(db, cid, pid, mode="WORKFLOW", key="k1")


async def test_a_key_is_unique_per_project_but_null_keys_and_other_projects_are_free(db):
    p1, cid = await _project_and_conversation(db, "uniq1")
    p2, _ = await _project_and_conversation(db, "uniq2")
    await _insert(db, cid, p1, mode="TEST_ONLY", key="same")
    with pytest.raises(IntegrityError, match="uq_tasks_project_idempotency_key"):
        await _insert(db, cid, p1, mode="TEST_ONLY", key="same")
    await _insert(db, cid, p2, mode="TEST_ONLY", key="same")               # another project: no clash
    await _insert(db, cid, p1, mode="TEST_ONLY")                           # NULL keys never collide
    await _insert(db, cid, p1, mode="TEST_ONLY")


async def test_the_downgrade_refuses_while_a_test_only_task_exists_and_goes_through_without_one(pg):
    name = f"mig_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(pg.sync_uri)
    await admin.execute(f"CREATE DATABASE {name}")
    await admin.close()
    cfg = alembic_config(pg.async_url(name))
    url = pg.sync_uri.rsplit("/", 1)[0] + "/" + name
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
        conn = await asyncpg.connect(url)
        pid, cid, tid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await conn.execute("insert into projects(id,name,workspace_path,workspace_key) values ($1,'p','/w/p','/w/p')", pid)
        await conn.execute("insert into conversations(id,status) values ($1,'ACTIVE')", cid)
        await conn.execute(
            "insert into tasks(id,conversation_id,project_id,requirements,requirements_hash,status,limits,test_policy,"
            "test_policy_hash,plan_revision,workflow_iteration,replan_count,cli_invocations,run_seconds,decisions_hash,"
            "row_version,mode) values ($1,$2,$3,'r','h','PENDING','{}','{}','h',0,0,0,0,0,'',0,'TEST_ONLY')", tid, cid, pid)
        await conn.close()
        with pytest.raises(Exception, match="TEST_ONLY tasks exist"):
            await asyncio.to_thread(command.downgrade, cfg, "0005")
        conn = await asyncpg.connect(url)
        assert await conn.fetchval("select mode from tasks where id=$1", tid) == "TEST_ONLY"      # nothing was dropped
        await conn.execute("delete from tasks where id=$1", tid)
        await conn.close()
        await asyncio.to_thread(command.downgrade, cfg, "0005")
        conn = await asyncpg.connect(url)
        columns = {r["column_name"] for r in await conn.fetch(
            "select column_name from information_schema.columns where table_name='tasks'")}
        assert not {"mode", "idempotency_key", "request_fingerprint"} & columns
        await conn.close()
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        admin = await asyncpg.connect(pg.sync_uri)
        await admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        await admin.close()
