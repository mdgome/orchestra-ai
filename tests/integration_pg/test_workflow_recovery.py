import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from app.infra.process_control import TerminationResult
from app.models.states import CliOutcome
from app.models.tables import Execution, ExecutionRequest, TaskRun
from app.providers.base import ProviderResult
from tests.harness import TEST_CODE, Harness, design_pass, developer_good, structured, write_tests_ok, wait_for


class Crash(RuntimeError):
    """Simulates the server dying in the middle of a CLI call."""


def crash_once(then, before=lambda call: None):
    state = {"crashed": False}

    def handler(call):
        if not state["crashed"]:
            state["crashed"] = True
            before(call)
            raise Crash("server died")
        return then(call)
    return handler


async def expire_everything(db, task_id):
    """Make the dead runner's lease and its claimed request look old (a restart some minutes later)."""
    async with db.transaction() as s:
        await s.execute(update(TaskRun).where(TaskRun.task_id == task_id)
                        .values(lease_expires_at=text("now() - interval '1 minute'")))
        await s.execute(update(ExecutionRequest).where(ExecutionRequest.task_id == task_id,
                                                       ExecutionRequest.status == "CLAIMED")
                        .values(claimed_at=text("now() - interval '1 hour'")))


async def crash_during(h, task):
    await h.tasks.request_execution(task.id)
    with pytest.raises(Crash):
        await h.run_until_idle()


# ----------------------------------------------------------------------------------------------------------
async def test_restart_resumes_an_interrupted_step_when_nothing_changed(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"TESTER": crash_once(write_tests_ok)})
    task = await h.new_task()
    await crash_during(h, task)
    mid = await h.steps_of(task.id)
    assert [(s.step_key, s.status) for s in mid][-1] == ("TEST", "RUNNING")      # left behind by the crash
    assert (await h.task(task.id)).status == "TESTING"

    await expire_everything(db, task.id)
    assert (await h.runner.recover())["requeued"] == 1
    assert await h.run_until_idle() == ["COMPLETED"]

    steps = await h.steps_of(task.id)
    tests = [s for s in steps if s.step_key == "TEST"]
    assert [(s.step_attempt, s.status) for s in tests] == [(1, "INTERRUPTED"), (2, "SUCCEEDED")]
    # earlier SUCCEEDED steps were NOT run again (checkpoint resume)
    assert h.roles_called(h.claude).count("PLANNER") == 1 and h.roles_called(h.claude).count("DEVELOPER") == 1
    assert h.roles_called(h.codex).count("DESIGN_REVIEWER") == 1
    runs = await h.runs(task.id)
    assert sorted(r.status for r in runs) == ["ABANDONED", "FINISHED"]          # one RUNNING at a time, ever


async def test_interrupted_file_changing_step_with_unknown_changes_is_never_re_run_automatically(db, tmp_path):
    def partial_write(call):
        (Path(call.cwd) / "tests").mkdir(exist_ok=True)
        (Path(call.cwd) / "tests" / "test_half.py").write_text("def test_half():\n    assert True\n")
    h = Harness(db, tmp_path, claude_handlers={"TESTER": crash_once(write_tests_ok, before=partial_write)})
    task = await h.new_task()
    await crash_during(h, task)
    await expire_everything(db, task.id)
    await h.runner.recover()
    tester_calls_before = h.roles_called(h.claude).count("TESTER")
    assert await h.run_until_idle() == ["WAITING_APPROVAL"]

    t = await h.task(task.id)
    steps = await h.steps_of(task.id)
    approvals = await h.approvals(task.id)
    assert t.status == "WAITING_APPROVAL"
    assert steps[-1].step_key == "TEST" and steps[-1].status == "NEEDS_MANUAL_RECOVERY"
    assert [a.type for a in approvals] == ["RECOVERY"] and approvals[0].status == "PENDING"
    assert h.roles_called(h.claude).count("TESTER") == tester_calls_before      # nothing re-ran


async def test_recovery_continue_keeps_the_changes_and_resumes_from_the_checkpoint(db, tmp_path):
    def partial_write(call):
        (Path(call.cwd) / "tests").mkdir(exist_ok=True)
        (Path(call.cwd) / "tests" / "test_calc.py").write_text(TEST_CODE)
    h = Harness(db, tmp_path, claude_handlers={"TESTER": crash_once(write_tests_ok, before=partial_write)})
    task = await h.new_task()
    await crash_during(h, task)
    await expire_everything(db, task.id)
    await h.runner.recover()
    assert await h.run_until_idle() == ["WAITING_APPROVAL"]
    approval = (await h.approvals(task.id))[0]
    await h.decide(approval.id, option="continue")
    assert await h.run_until_idle() == ["COMPLETED"]
    assert h.roles_called(h.claude).count("PLANNER") == 1                      # PLAN was not repeated


async def test_recovery_retry_is_refused_when_the_workspace_differs_from_the_step_start(db, tmp_path):
    def partial_write(call):
        (Path(call.cwd) / "tests").mkdir(exist_ok=True)
        (Path(call.cwd) / "tests" / "test_half.py").write_text("def test_half():\n    assert True\n")
    h = Harness(db, tmp_path, claude_handlers={"TESTER": crash_once(write_tests_ok, before=partial_write)})
    task = await h.new_task()
    await crash_during(h, task)
    await expire_everything(db, task.id)
    await h.runner.recover()
    await h.run_until_idle()
    first = (await h.approvals(task.id))[0]
    await h.decide(first.id, option="retry")                  # "retry" demands the original starting state
    assert await h.run_until_idle() == ["WAITING_APPROVAL"]    # re-verification fails: a NEW gate, nothing runs
    approvals = await h.approvals(task.id)
    assert len(approvals) == 2 and approvals[1].status == "PENDING" and approvals[1].type == "RECOVERY"
    assert "workspace differs" in approvals[1].request_payload["problem"]


# ---- process termination cannot be proven -----------------------------------------------------------------
async def test_unconfirmed_cli_termination_never_creates_a_new_run_even_after_recovery_approval(db, tmp_path):
    verdict = {"v": TerminationResult.ACCESS_DENIED}
    h = Harness(db, tmp_path, claude_handlers={"TESTER": crash_once(write_tests_ok)},
                terminator=lambda ident: verdict["v"])
    task = await h.new_task()
    await crash_during(h, task)
    await expire_everything(db, task.id)
    await h.runner.recover()

    assert await h.run_until_idle() == ["UNCERTAIN"]
    t = await h.task(task.id)
    assert t.status == "WAITING_APPROVAL"
    assert [a.type for a in await h.approvals(task.id)] == ["RECOVERY"]
    assert [s.status for s in await h.steps_of(task.id)][-1] == "NEEDS_MANUAL_RECOVERY"
    runs = await h.runs(task.id)
    assert [r.status for r in runs] == ["RUNNING"]                              # the old run was NOT abandoned
    async with db.transaction() as s:
        req = (await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task.id))).scalars().one()
    assert req.status == "NEEDS_REVIEW"

    # the human approves "continue", but approval alone does not prove the process is gone
    await h.decide((await h.approvals(task.id))[0].id, option="continue")
    assert await h.run_until_idle() == ["UNCERTAIN"]
    assert len(await h.runs(task.id)) == 1 and (await h.runs(task.id))[0].status == "RUNNING"
    approvals = await h.approvals(task.id)
    assert len(approvals) == 2 and approvals[1].status == "PENDING"            # a new RECOVERY gate, never a silent run
    assert h.roles_called(h.claude).count("TESTER") == 1

    # only once termination is verified can a new run exist
    verdict["v"] = TerminationResult.TERMINATED
    await h.decide(approvals[1].id, option="continue", key="k2")
    assert await h.run_until_idle() == ["COMPLETED"]
    assert sorted(r.status for r in await h.runs(task.id)) == ["ABANDONED", "FINISHED"]


async def test_spawn_with_unknown_process_identity_is_treated_as_possibly_running(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"TESTER": crash_once(write_tests_ok)})
    h.claude.identity = None                         # STARTING was committed but no pid was ever learned
    task = await h.new_task()
    await crash_during(h, task)
    await expire_everything(db, task.id)
    await h.runner.recover()
    assert await h.run_until_idle() == ["UNCERTAIN"]
    assert (await h.task(task.id)).status == "WAITING_APPROVAL"
    assert h.roles_called(h.claude).count("TESTER") == 1


# ---- lease ownership --------------------------------------------------------------------------------------------
async def test_result_arriving_after_lease_loss_is_not_recorded_as_a_completion(db, tmp_path):
    async def slow_dev(call):
        async with db.transaction() as s:             # another runner took the task over while the CLI ran
            await s.execute(update(TaskRun).where(TaskRun.status == "RUNNING").values(status="ABANDONED"))
        return developer_good(call)
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": slow_dev})
    task = await h.new_task()
    await h.tasks.request_execution(task.id)
    async with db.transaction() as s:
        from app.repositories.approval_repository import ExecutionRequestRepository
        request = await ExecutionRequestRepository(s).claim_next(h.runner.runner_id)
    assert await h.runner.process(request) == "LEASE_LOST"

    t = await h.task(task.id)
    assert t.status == "DEVELOPING"                                            # NOT advanced to TESTING
    steps = await h.steps_of(task.id)
    assert steps[-1].step_key == "DEVELOP" and steps[-1].status == "INTERRUPTED"
    ex = [e for e in await h.executions(task.id) if e.role == "DEVELOPER"][0]
    assert ex.discarded_result is not None and "lease lost" in ex.discarded_result["reason"]
    assert "TESTING" not in await h.state_path(task.id)
    assert h.roles_called(h.claude).count("TESTER") == 0

    # The request was re-queued (not marked DONE). The discarded CLI had already written files, so the
    # interrupted Developer step must NOT be re-run automatically: a RECOVERY gate is opened instead.
    assert await h.run_until_idle() == ["WAITING_APPROVAL"]
    assert [a.type for a in await h.approvals(task.id)] == ["RECOVERY"]
    assert h.roles_called(h.claude).count("DEVELOPER") == 1


async def test_runner_start_refuses_a_second_runner_process(db, tmp_path):
    from app.services.runner import RunnerAlreadyActive
    first = Harness(db, tmp_path / "a")
    second = Harness(db, tmp_path / "b")
    await first.runner.start()
    try:
        with pytest.raises(RunnerAlreadyActive):
            await second.runner.start()
    finally:
        await first.runner.stop()
    await second.runner.start()                       # the lock was released by stop()
    await second.runner.stop()


async def test_runner_loop_consumes_requests_started_through_lifespan(db, tmp_path):
    h = Harness(db, tmp_path)
    await h.runner.start()
    try:
        task = await h.new_task()
        await h.tasks.request_execution(task.id)
        h.runner.notify()
        assert await wait_for(lambda: _is(h, task.id, "COMPLETED"), timeout=60)
    finally:
        await h.runner.stop()


async def _is(h, task_id, status):
    return (await h.task(task_id)).status == status


async def test_startup_recovery_requeues_claimed_requests_of_a_dead_runner(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    await h.tasks.request_execution(task.id)
    async with db.transaction() as s:      # a previous process claimed it and died right away
        await s.execute(update(ExecutionRequest).where(ExecutionRequest.task_id == task.id)
                        .values(status="CLAIMED", runner_id="dead", claimed_at=text("now()")))
    await h.runner.start()                 # startup: we hold the runner lock, so every CLAIMED request is stale
    try:
        assert await wait_for(lambda: _is(h, task.id, "COMPLETED"), timeout=60)
    finally:
        await h.runner.stop()


# ---- cancellation ----------------------------------------------------------------------------------------------------
def blocking_until_cancelled(provider_ref, termination="TERMINATED"):
    async def handler(call):
        for _ in range(400):
            if provider_ref[0].cancelled:
                return ProviderResult(outcome=CliOutcome.CANCELLED, termination=termination)
            await asyncio.sleep(0.05)
        raise AssertionError("cancel never arrived")
    return handler


async def run_and_cancel(h, task):
    runner_task = asyncio.create_task(h.start(task.id))
    assert await wait_for(lambda: _called(h, "TESTER"))
    await h.tasks.request_cancellation(task.id)          # only a request: nothing is terminated by the API call
    return await asyncio.wait_for(runner_task, 30)


async def _called(h, role):
    return role in h.roles_called(h.claude)


async def test_cancellation_request_stops_the_cli_and_cancels_only_after_termination_is_confirmed(db, tmp_path):
    ref = [None]
    h = Harness(db, tmp_path, claude_handlers={"TESTER": blocking_until_cancelled(ref)})
    ref[0] = h.claude
    task = await h.new_task()
    out = await run_and_cancel(h, task)
    assert out == ["CANCELLED"]
    t = await h.task(task.id)
    assert t.status == "CANCELLED" and t.cancel_requested_at is not None
    run_id = str((await h.runs(task.id))[0].id)
    assert any(k.startswith(run_id) for k in h.claude.cancelled)
    assert (await h.steps_of(task.id))[-1].status == "CANCELLED"
    assert [r.status for r in await h.runs(task.id)] == ["FINISHED"]
    ex = [e for e in await h.executions(task.id) if e.role == "TESTER"][0]
    assert ex.finished_at is not None and ex.terminated_at is not None


async def test_cancellation_with_unconfirmed_termination_does_not_cancel(db, tmp_path):
    ref = [None]
    h = Harness(db, tmp_path, claude_handlers={"TESTER": blocking_until_cancelled(ref, termination="STILL_ALIVE")})
    ref[0] = h.claude
    task = await h.new_task()
    out = await run_and_cancel(h, task)
    assert out == ["WAITING_APPROVAL"]
    t = await h.task(task.id)
    assert t.status == "WAITING_APPROVAL"                                    # NOT CANCELLED
    assert [a.type for a in await h.approvals(task.id)] == ["RECOVERY"]
    assert [r.status for r in await h.runs(task.id)] == ["RUNNING"]
    ex = [e for e in await h.executions(task.id) if e.role == "TESTER"][0]
    assert ex.finished_at is None                                            # still considered possibly running


async def test_cancelling_an_idle_task_finishes_it_without_any_cli(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    await h.tasks.request_cancellation(task.id)
    assert (await h.task(task.id)).status == "PENDING"                       # a request alone changes nothing else
    assert (await h.runner.recover())["cancelled"] == 1
    assert (await h.task(task.id)).status == "CANCELLED"
    assert h.claude.calls == [] and h.codex.calls == []


async def test_cancellation_of_a_task_waiting_for_approval(db, tmp_path):
    from tests.harness import GATE, PLAN
    h = Harness(db, tmp_path, claude_handlers={"PLANNER": lambda c: structured(**dict(PLAN, human_gate_request=GATE))})
    task = await h.new_task()
    await h.start(task.id)
    await h.tasks.request_cancellation(task.id)
    await h.runner.recover()
    t = await h.task(task.id)
    assert t.status == "CANCELLED"
    assert (await h.steps_of(task.id))[0].status == "CANCELLED"
