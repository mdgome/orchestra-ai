from app.models.states import TaskState
from tests.harness import Harness


async def test_full_workflow_completes_and_records_everything(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    outcomes = await h.start(task.id)
    assert outcomes == ["COMPLETED"]

    t = await h.task(task.id)
    assert t.status == "COMPLETED" and t.failure_reason is None

    # strict order of the state machine, every change recorded append-only
    assert await h.state_path(task.id) == [
        "PENDING", "PLANNING", "DESIGN_REVIEWING", "DEVELOPING", "TESTING", "REVIEWING", "COMPLETED"]

    steps = await h.steps_of(task.id)
    assert [s.step_key for s in steps] == ["PLAN", "DESIGN_REVIEW", "DEVELOP", "TEST", "CODE_REVIEW"]
    assert all(s.status == "SUCCEEDED" and s.step_attempt == 1 for s in steps)

    # right provider per role
    assert h.roles_called(h.claude) == ["PLANNER", "DEVELOPER", "TESTER"]
    assert h.roles_called(h.codex) == ["DESIGN_REVIEWER", "CODE_REVIEWER"]

    # tests were run by the Orchestrator (real pytest, JUnit), not taken from the agent's words
    test_out = steps[3].output
    assert test_out["test_result"]["passed"] and test_out["test_result"]["junit_ok"]
    assert test_out["test_result"]["executed"] == 1

    # no run left behind; the lease was returned
    assert [r.status for r in await h.runs(task.id)] == ["FINISHED"]


async def test_each_role_gets_its_own_native_session_and_none_is_shared(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    await h.start(task.id)
    from sqlalchemy import select

    from app.models.tables import AgentSession
    async with db.transaction() as s:
        rows = list((await s.execute(select(AgentSession).where(AgentSession.task_id == task.id))).scalars())
    assert sorted(r.agent_role for r in rows) == ["CODE_REVIEWER", "DESIGN_REVIEWER", "DEVELOPER", "PLANNER", "TESTER"]
    ids = [r.native_session_id for r in rows]
    assert len(set(ids)) == 5 and None not in ids
    assert {r.provider for r in rows if r.agent_role in ("PLANNER", "DEVELOPER", "TESTER")} == {"CLAUDE"}
    assert {r.provider for r in rows if r.agent_role in ("DESIGN_REVIEWER", "CODE_REVIEWER")} == {"CODEX"}
    assert all(r.status == "CLOSED" for r in rows)            # sessions are closed when the task finishes
    # a fresh role never resumed another role's session
    assert all(c.resume_id is None for c in h.claude.calls + h.codex.calls)


async def test_native_session_ids_never_appear_in_audit_history(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    await h.start(task.id)
    secrets = set(h.claude.live_ids | h.codex.live_ids)
    blob = str([t.event for t in await h.transitions(task.id)]) + str(
        [(e.stdout_tail, e.stderr_tail, e.detail) for e in await h.executions(task.id)])
    assert secrets and not any(sid in blob for sid in secrets)
    events = [t.event for t in await h.transitions(task.id) if t.kind == "SESSION_EVENT"]
    assert events and all(e["session"].startswith("sess:") for e in events)


async def test_request_execution_is_idempotent_and_never_runs_the_cli(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    first, created1 = await h.tasks.request_execution(task.id)
    second, created2 = await h.tasks.request_execution(task.id)
    assert created1 and not created2 and first.id == second.id
    assert h.claude.calls == [] and h.codex.calls == []          # only persisted; the Runner executes later
    assert (await h.task(task.id)).status == TaskState.PENDING
