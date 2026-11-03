"""A bounded pool: exact checkout capacity, waiting (not failing) beyond it, and a Runner that still works when the
pool is as small as the settings allow. Every connection, task and engine is released in `finally`."""
import asyncio

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.db import Database, create_engine
from app.services.runner import Runner
from tests.harness import Harness, wait_for

LOCK_KEY = 987_654_321
BOUND = 15


async def lock_is_free(db_engine) -> bool:
    """True when nobody holds the Runner advisory lock. The probe lock is released again at once."""
    conn = await db_engine.connect()
    try:
        got = (await conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})).scalar()
        if got:
            assert (await conn.execute(text("select pg_advisory_unlock(:k)"), {"k": LOCK_KEY})).scalar() is True
        return bool(got)
    finally:
        await conn.close()


async def checkout(engine):
    return await engine.connect()


async def test_checkout_is_bounded_by_pool_size_plus_overflow_and_waits_beyond_it(db):
    engine = create_engine(db.engine.url.render_as_string(hide_password=False), pool_size=2, max_overflow=1)
    held, waiting = [], None
    try:
        for _ in range(3):                                              # pool_size + max_overflow
            held.append(await asyncio.wait_for(engine.connect(), BOUND))
        waiting = asyncio.create_task(checkout(engine))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not waiting.done()                                       # the 4th checkout waits
        await held.pop().close()                                        # one connection back ...
        extra = await asyncio.wait_for(waiting, BOUND)                  # ... and it proceeds
        held.append(extra)
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        for conn in held:
            await asyncio.wait_for(conn.close(), BOUND)
        await asyncio.wait_for(engine.dispose(), BOUND)


async def test_a_runner_finishes_a_task_on_the_smallest_valid_pool(db, tmp_path):
    engine = create_engine(db.engine.url.render_as_string(hide_password=False), pool_size=1, max_overflow=1)
    small = Database(engine)                                            # capacity 2: the lock + one working connection
    h = Harness(small, tmp_path)
    task = await h.new_task()
    runner = Runner(small, h.leases, h.workflow, h.gates, [h.claude, h.codex], advisory_key=LOCK_KEY,
                    poll_seconds=0.05)
    probe = create_engine(db.engine.url.render_as_string(hide_password=False), pool_size=1, max_overflow=0)
    try:
        await runner.start()
        await h.tasks.request_execution(task.id)
        runner.notify()

        async def done():
            return (await h.task(task.id)).status == "COMPLETED"

        assert await wait_for(done, timeout=120)                        # sequential progress, no pool deadlock
    finally:
        try:
            await asyncio.wait_for(runner.stop(), BOUND * 2)
            assert await asyncio.wait_for(lock_is_free(probe), BOUND)   # the advisory lock did not leak
        finally:
            await asyncio.wait_for(small.dispose(), BOUND)
            await asyncio.wait_for(probe.dispose(), BOUND)
