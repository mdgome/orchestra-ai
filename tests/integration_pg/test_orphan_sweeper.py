"""B2 phase 5 (design 7.4): the orphan sweeper pass on the embedded PostgreSQL. Real rows, real short-lived processes where a row
needs a live or a dead process; the page procedure, the result states, the per-row guarded settlement and the committed cursor."""
import contextlib
import dataclasses
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.infra.process_control import capture_identity
from app.models.tables import Execution
from app.repositories.task_repository import TaskRepository
from app.services import orphan_sweeper as OS
from app.services.lease_service import TrackedVerdict
from app.services.orphan_sweeper import NO_ELIGIBLE_TARGETS, QUERY_FAILED, SCAN_LIMIT_REACHED, TARGETS_READY, OrphanSweeper
from app.services.sweeper_stall import CANDIDATE, PAGE_AFTER, SWEEP_NO_PROGRESS
from app.services.tracked_classifier import RowClass, classify_row
from app.validation import policies as P
from app.validation.validators import build_sweeper_pass_policy
from tests.helpers import make_task
from tests.integration_pg.test_process_schema import ids_where, make_project_task
from tests.integration_pg.test_tracked_takeover import finished_process, runner_process

HOST = socket.gethostname()


@contextlib.asynccontextmanager
async def section():
    yield


def sweeper(db, **kw) -> OrphanSweeper:
    return OrphanSweeper(db, section, incarnation=lambda: "inc-1", **kw)


async def add_row(db, task, *, pid=None, create_time=None, runner=None, detail=None, finished=False, run_id=None) -> uuid.UUID:
    base = {"tracked": True, "v": 1, "checkpoint_step_key": "TEST", "runner_process": runner or runner_process(None)}
    async with db.transaction() as s:
        row = Execution(id=uuid.uuid4(), task_id=task.id, run_id=run_id, role="TESTER", provider="LOCAL", outcome="STARTING",
                        pid=pid, process_create_time=create_time, detail=base if detail is None else detail)
        s.add(row)
        await s.flush()
        if finished:
            await s.execute(text("UPDATE executions SET finished_at = now() WHERE id = :i"), {"i": row.id})
        return row.id


async def tracked(db, row_id):
    async with db.transaction() as s:
        return await TaskRepository(s).tracked_row(row_id)


async def klass(db, row_id) -> RowClass:
    row = await tracked(db, row_id)
    return classify_row(row.id, row.pid, row.finished_at, row.detail_text).row_class


def detail_of(row) -> dict:
    import json
    return json.loads(row.detail_text)


@pytest.fixture
def policy():
    return build_sweeper_pass_policy()


# --- settlement of one row --------------------------------------------------------------------------------------------------
async def test_an_open_row_that_never_spawned_is_closed_with_a_never_ran_proof_and_the_pass_wraps_around(db, policy):
    _, task = await make_project_task(db)
    row_id = await add_row(db, task)
    out = await sweeper(db)(None, policy)
    assert (out.kind, out.wrapped) == ("OK", True)
    row = await tracked(db, row_id)
    assert row.finished_at is not None and detail_of(row)["proof"] == "NEVER_RAN" and detail_of(row)["tree"] == "EMPTY"
    assert await klass(db, row_id) is RowClass.RESOLVED_PROVEN


async def test_a_row_whose_root_has_exited_and_whose_runner_is_gone_gets_the_assumed_proof(db, policy):
    _, task = await make_project_task(db)
    root, owner = finished_process(), finished_process()
    row_id = await add_row(db, task, pid=root.pid, create_time=root.create_time, runner=runner_process(owner))
    await sweeper(db)(None, policy)
    row = await tracked(db, row_id)
    assert detail_of(row)["proof"] == "JOB_KILL_ON_CLOSE_ASSUMED" and row.finished_at is not None
    assert await klass(db, row_id) is RowClass.RESOLVED_PROVEN


async def test_a_row_with_a_live_root_is_left_untouched_and_counts_as_decided(db, policy):
    _, task = await make_project_task(db)
    owner = finished_process()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ident = capture_identity(proc.pid)
        row_id = await add_row(db, task, pid=ident.pid, create_time=ident.create_time, runner=runner_process(owner))
        before = await tracked(db, row_id)
        out = await sweeper(db)(None, policy)
        assert (out.kind, out.wrapped) == ("OK", True)                       # no-write proven: decided, the set was read to its end
        assert await tracked(db, row_id) == before and await klass(db, row_id) is RowClass.UNRESOLVED
    finally:
        proc.kill()
        proc.wait()


async def test_a_closed_unproven_row_keeps_its_finished_at_and_gets_the_proof(db, policy):
    _, task = await make_project_task(db)
    row_id = await add_row(db, task, finished=True)
    finished_before = (await tracked(db, row_id)).finished_at
    await sweeper(db)(None, policy)
    row = await tracked(db, row_id)
    assert row.finished_at == finished_before and detail_of(row)["proof"] == "NEVER_RAN"
    assert await klass(db, row_id) is RowClass.RESOLVED_PROVEN


async def test_an_anomaly_with_a_positive_os_proof_is_moved_to_anomaly_original_and_settled(db, policy):
    _, task = await make_project_task(db)
    bad = {"tracked": True, "v": 1, "proof": "NOT_A_PROOF", "runner_process": runner_process(None)}
    row_id = await add_row(db, task, detail=bad, finished=True)
    assert await klass(db, row_id) is RowClass.ANOMALY
    await sweeper(db)(None, policy)
    detail = detail_of(await tracked(db, row_id))
    assert detail["proof"] == "NEVER_RAN" and detail["anomaly_original"] == {"proof": "NOT_A_PROOF", "v": 1}
    assert detail["anomaly_settlement"]["kind"] == "INVALID_PROOF" and detail["anomaly_settlement"]["incarnation"] == "inc-1"
    assert await klass(db, row_id) is RowClass.RESOLVED_PROVEN


async def test_an_unknown_schema_version_anomaly_is_left_untouched(db, policy):
    _, task = await make_project_task(db)
    row_id = await add_row(db, task, detail={"tracked": True, "v": 2, "proof": "NEVER_RAN"}, finished=True)
    before = await tracked(db, row_id)
    out = await sweeper(db)(None, policy)
    assert await tracked(db, row_id) == before and out.wrapped                # not a target; stays in A
    assert await klass(db, row_id) is RowClass.ANOMALY


async def test_a_guarded_write_that_matches_zero_rows_is_reread_and_never_written_twice(db, policy):
    _, task = await make_project_task(db)
    row_id = await add_row(db, task)

    async def settle_first(fn, stale):
        async with db.transaction() as s:                                     # another writer settles the row first
            await s.execute(text("UPDATE executions SET finished_at = now(), detail = detail || CAST(:m AS jsonb) WHERE id = :i"),
                            {"i": row_id, "m": '{"proof": "NEVER_RAN"}'})
        return fn(stale)

    out = await sweeper(db, run_blocking=settle_first)(None, policy)
    assert (out.kind, out.wrapped) == ("OK", True)                            # re-read shows it settled: decided
    assert (await ids_where(db, "detail ->> 'proof' = 'NEVER_RAN' AND detail ? 'abandoned_by_sweeper'")) == set()


async def test_a_row_whose_identity_changed_after_the_judgement_is_not_closed(db, policy):
    _, task = await make_project_task(db)
    owner = finished_process()
    root = finished_process()
    row_id = await add_row(db, task, pid=root.pid, create_time=root.create_time, runner=runner_process(owner))

    async def change_identity(fn, stale):
        verdict = fn(stale)
        async with db.transaction() as s:
            await s.execute(text("UPDATE executions SET process_create_time = process_create_time + 5 WHERE id = :i"), {"i": row_id})
        return verdict

    out = await sweeper(db, run_blocking=change_identity)(None, policy)
    assert out.kind == "NO_PROGRESS" and out.code == SWEEP_NO_PROGRESS and out.position == row_id and out.position_kind == CANDIDATE
    assert "proof" not in detail_of(await tracked(db, row_id))


# --- judgeable rows (W3) ---------------------------------------------------------------------------------------------------
async def test_a_row_of_a_live_run_is_not_a_candidate_until_the_lease_expires(db, policy):
    from app.models.tables import Task
    from sqlalchemy import update
    _, task = await make_project_task(db)
    async with db.transaction() as s:
        run = await TaskRepository(s).insert_run(task.id, "r", datetime.now(UTC), 30)
        await s.execute(update(Task).where(Task.id == task.id).values(status="RUNNING"))
        run_id = run.id
    row_id = await add_row(db, task, run_id=run_id)
    assert (await sweeper(db).collect(None, 50)).state == NO_ELIGIBLE_TARGETS
    async with db.transaction() as s:
        await s.execute(text("UPDATE task_runs SET lease_expires_at = now() - interval '1 second' WHERE id = :i"), {"i": run_id})
    scan = await sweeper(db).collect(None, 50)
    assert scan.state == TARGETS_READY and [r.id for r, _ in scan.targets] == [row_id]


# --- the page procedure and the result states -------------------------------------------------------------------------------
async def stuck_rows(db, task, n):
    return [await add_row(db, task, detail={"tracked": True, "v": 2, "proof": "NEVER_RAN"}, finished=True) for _ in range(n)]


async def test_a_short_page_is_the_end_and_a_full_budget_of_full_pages_is_scan_limit_reached(db, monkeypatch):
    _, task = await make_project_task(db)
    await stuck_rows(db, task, 3)
    monkeypatch.setattr(P, "CANDIDATE_PAGE_ROWS", 2)
    monkeypatch.setattr(P, "CANDIDATE_MAX_SCANNED_ROWS", 4)
    scan = await sweeper(db).collect(None, 50)
    assert (scan.state, scan.scanned) == (NO_ELIGIBLE_TARGETS, 3)             # page 2 had one row: a short page shows the end
    await stuck_rows(db, task, 1)                                             # now exactly the budget, with full pages
    scan = await sweeper(db).collect(None, 50)
    assert (scan.state, scan.cap, scan.scanned) == (SCAN_LIMIT_REACHED, "ROW", 4)


async def test_the_batch_size_ends_the_collection_with_targets_ready_and_the_cursor_passes_only_decided_rows(db, policy):
    _, task = await make_project_task(db)
    ids = sorted([await add_row(db, task, pid=4242) for _ in range(5)])        # pid but no identity: UNCLEAR, never provable
    small = dataclasses.replace(policy, batch_rows=2)
    out1 = await sweeper(db)(None, small)
    assert (out1.kind, out1.committed, out1.advanced, out1.wrapped) == ("OK", ids[1], True, False)
    out2 = await sweeper(db)(out1.committed, small)
    assert (out2.committed, out2.wrapped) == (ids[3], False)
    out3 = await sweeper(db)(out2.committed, small)
    assert (out3.kind, out3.wrapped) == ("OK", True) and out3.committed is None   # a short page: the end, wrap-around


async def test_decode_error_rows_are_skipped_never_written_and_the_scan_cursor_passes_them(db, monkeypatch):
    _, task = await make_project_task(db)
    a, b, c = sorted([await add_row(db, task, pid=1), await add_row(db, task, pid=2), await add_row(db, task, pid=3)])
    real = OS.classify_row

    def fake(row_id, pid, finished_at, detail_text):
        if row_id == b:
            return type(real(row_id, pid, finished_at, detail_text))(RowClass.ANOMALY, "OTHER_SCHEMA_INVALID", "decode_error")
        return real(row_id, pid, finished_at, detail_text)

    monkeypatch.setattr(OS, "classify_row", fake)
    scan = await sweeper(db).collect(None, 50)
    assert [r.id for r, _ in scan.targets] == [a, c] and scan.decode_errors == 1 and scan.scanned == 3


async def test_a_page_failure_discards_the_pass_and_names_the_boundary_before_the_unread_page(db, policy, monkeypatch):
    _, task = await make_project_task(db)
    await add_row(db, task, pid=4242)
    calls = []
    real = TaskRepository.sweep_page

    async def failing(self, last_seen_id, page_size):
        calls.append(last_seen_id)
        raise type("E", (Exception,), {})("x")

    class Timeout(Exception):
        orig = SimpleNamespace(sqlstate="57014")

    async def timing_out(self, last_seen_id, page_size):
        raise Timeout()

    monkeypatch.setattr(TaskRepository, "sweep_page", failing)
    out = await sweeper(db)(None, policy)
    assert (out.kind, out.code, out.position, out.position_kind, out.committed) == (
        "FAILED", OS.SWEEP_QUERY_FAILED, None, PAGE_AFTER, None)
    monkeypatch.setattr(TaskRepository, "sweep_page", timing_out)
    out = await sweeper(db)(None, policy)
    assert out.code == OS.SWEEP_PAGE_TIMEOUT
    monkeypatch.setattr(TaskRepository, "sweep_page", real)


async def test_the_page_runs_under_a_transaction_local_statement_timeout_that_does_not_leak(db):
    _, task = await make_project_task(db)
    seen = []
    real = TaskRepository.set_local_statement_timeout

    async def spy(self, ms):
        seen.append(ms)
        await real(self, ms)

    TaskRepository.set_local_statement_timeout = spy
    try:
        await sweeper(db).collect(None, 50)
    finally:
        TaskRepository.set_local_statement_timeout = real
    assert seen and 1 <= seen[0] <= int(P.CANDIDATE_PAGE_STATEMENT_TIMEOUT_SECONDS * 1000)
    async with db.transaction() as s:
        assert (await s.execute(text("select current_setting('statement_timeout')"))).scalar_one() in ("0", "0ms")


async def test_processing_is_in_ascending_id_order_one_row_at_a_time_and_a_failure_keeps_the_committed_prefix(db, policy, monkeypatch):
    _, task = await make_project_task(db)
    ids = sorted([await add_row(db, task) for _ in range(3)])
    order = []
    real = OrphanSweeper._settle

    async def settle(self, row, cls):
        order.append(row.id)
        if row.id == ids[2]:
            raise RuntimeError("boom")
        return await real(self, row, cls)

    monkeypatch.setattr(OrphanSweeper, "_settle", settle)
    out = await sweeper(db)(None, policy)
    assert order == ids                                                       # ascending, serial
    assert (out.kind, out.committed, out.advanced, out.code, out.position, out.position_kind) == (
        "FAILED", ids[1], True, OS.SWEEP_EXCEPTION, ids[2], CANDIDATE)         # t1, t2 committed, t3 the failure candidate
    assert await klass(db, ids[0]) is RowClass.RESOLVED_PROVEN and await klass(db, ids[2]) is RowClass.UNRESOLVED


async def test_zero_decided_rows_is_a_pass_without_progress_and_the_budget_stops_new_processing(db, policy):
    _, task = await make_project_task(db)
    ids = sorted([await add_row(db, task) for _ in range(2)])
    now = {"t": 0.0}
    sw = OrphanSweeper(db, section, clock=lambda: now["t"])
    real = sw.collect

    async def collect_then_spend_the_budget(start, batch_rows):
        scan = await real(start, batch_rows)
        now["t"] = 10_000.0                                                    # the pass budget is spent before the first row
        return scan

    sw.collect = collect_then_spend_the_budget
    out = await sw(None, policy)
    assert (out.kind, out.position, out.committed) == ("NO_PROGRESS", ids[0], None)
    assert await klass(db, ids[0]) is RowClass.UNRESOLVED


async def test_a_collection_failure_never_processes_gathered_targets(db, policy, monkeypatch):
    _, task = await make_project_task(db)
    for _ in range(3):
        await add_row(db, task)
    real = TaskRepository.sweep_page
    state = {"n": 0}

    async def second_page_fails(self, last_seen_id, page_size):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("late failure")
        return await real(self, last_seen_id, page_size)

    monkeypatch.setattr(P, "CANDIDATE_PAGE_ROWS", 2)
    monkeypatch.setattr(TaskRepository, "sweep_page", second_page_fails)
    out = await sweeper(db)(None, policy)
    assert out.kind == "FAILED" and out.committed is None                     # discarded; the cursor is the pass start
    assert await ids_where(db, "detail ? 'proof'") == set()                   # nothing was processed
