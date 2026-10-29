from sqlalchemy import select, update

from app.models.states import CliOutcome
from app.models.tables import Approval
from app.providers.base import ProviderResult
from tests.harness import GATE, PLAN, Harness, developer_good, structured


def denied(tool="Bash", command="npm test"):
    return ProviderResult(outcome=CliOutcome.PERMISSION_REQUESTED, exit_code=0, native_session_id=None,
                          permission_requests=[{"tool": tool, "input": "{}", "tool_input": {"command": command}}])


def dev_needs_permission_once(command="npm test", after=developer_good):
    def handler(call):
        return denied(command=command) if call.n == 1 else after(call)
    return handler


async def get_approval(db, approval_id):
    async with db.transaction() as s:
        return (await s.execute(select(Approval).where(Approval.id == approval_id))).scalar_one()


async def test_permission_request_opens_a_gate_bound_to_the_execution_and_exact_command(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev_needs_permission_once()})
    task = await h.new_task()
    assert await h.start(task.id) == ["WAITING_APPROVAL"]
    approval = (await h.approvals(task.id))[0]
    ex = [e for e in await h.executions(task.id) if e.role == "DEVELOPER"][0]
    assert approval.type == "PERMISSION" and approval.scope == "COMMAND"
    assert approval.execution_id == ex.id and approval.command_fingerprint
    assert approval.target["pattern"] == "Bash(npm test)" and approval.target["enforceable"] is True
    assert approval.used_at is None
    assert [r.status for r in await h.runs(task.id)] == ["FINISHED"]            # lease returned while waiting
    assert (await h.steps_of(task.id))[-1].status == "WAITING_APPROVAL"


async def test_approval_grants_exactly_one_use_for_that_command_and_is_consumed(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev_needs_permission_once()})
    task = await h.new_task()
    await h.start(task.id)
    approval = (await h.approvals(task.id))[0]
    await h.decide(approval.id, option="allow_once")
    assert await h.run_until_idle() == ["COMPLETED"]

    devs = [c for c in h.claude.calls if c.role == "DEVELOPER"]
    assert devs[0].request.one_off_tool is None
    assert devs[1].request.one_off_tool == "Bash(npm test)"                      # only the approved command
    assert all(c.request.one_off_tool is None for c in h.claude.calls if c.role != "DEVELOPER")
    assert (await get_approval(db, approval.id)).used_at is not None             # consumed at spawn
    # waiting for the permission did not consume the technical-retry budget
    dev_steps = [s for s in await h.steps_of(task.id) if s.step_key == "DEVELOP"]
    assert [(s.step_attempt, s.status) for s in dev_steps] == [(1, "WAITING_APPROVAL"), (2, "SUCCEEDED")]


async def test_consumed_approval_is_not_reusable_even_if_the_cli_then_fails(db, tmp_path):
    def handler(call):
        if call.n == 1:
            return denied()
        if call.n == 2:
            return ProviderResult(outcome=CliOutcome.ERROR, exit_code=1)       # fails AFTER the grant was consumed
        return developer_good(call)
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": handler})
    task = await h.new_task()
    await h.start(task.id)
    approval = (await h.approvals(task.id))[0]
    await h.decide(approval.id, option="allow_once")
    assert await h.run_until_idle() == ["COMPLETED"]
    devs = [c for c in h.claude.calls if c.role == "DEVELOPER"]
    assert len(devs) == 3
    assert devs[1].request.one_off_tool == "Bash(npm test)"
    assert devs[2].request.one_off_tool is None                                  # the grant is gone
    execs = [e for e in await h.executions(task.id) if e.role == "DEVELOPER"]
    assert [e.outcome for e in execs] == ["PERMISSION_REQUESTED", "ERROR", "SUCCESS"]  # failure after use is recorded


async def test_approval_for_a_different_command_cannot_be_used(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev_needs_permission_once()})
    task = await h.new_task()
    await h.start(task.id)
    approval = (await h.approvals(task.id))[0]
    await h.decide(approval.id, option="allow_once")
    async with db.transaction() as s:            # tamper: the approval no longer matches the requested command
        await s.execute(update(Approval).where(Approval.id == approval.id).values(command_fingerprint="other-command"))
    assert await h.run_until_idle() == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "FAIL_CLOSED"
    assert (await get_approval(db, approval.id)).used_at is None
    assert [c.request.one_off_tool for c in h.claude.calls if c.role == "DEVELOPER"] == [None]   # never ran


async def test_already_used_approval_cannot_be_used_again(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev_needs_permission_once()})
    task = await h.new_task()
    await h.start(task.id)
    approval = (await h.approvals(task.id))[0]
    await h.decide(approval.id, option="allow_once")
    async with db.transaction() as s:
        await s.execute(update(Approval).where(Approval.id == approval.id).values(used_at=__import__("sqlalchemy").func.now()))
    assert await h.run_until_idle() == ["FAILED"]
    assert len([c for c in h.claude.calls if c.role == "DEVELOPER"]) == 1


async def test_permission_the_cli_cannot_enforce_is_never_granted(db, tmp_path):
    # PLANNER has no Bash tool at all, so a one-off Bash grant cannot be enforced -> hold, never widen
    def planner(call):
        return denied(command="ls") if call.n == 1 else structured(**PLAN)
    h = Harness(db, tmp_path, claude_handlers={"PLANNER": planner})
    task = await h.new_task()
    assert await h.start(task.id) == ["WAITING_APPROVAL"]
    approval = (await h.approvals(task.id))[0]
    assert approval.target["enforceable"] is False
    await h.decide(approval.id, option="allow_once")
    assert await h.run_until_idle() == []                                        # no resume request exists
    assert (await h.task(task.id)).status == "WAITING_APPROVAL"
    assert len([c for c in h.claude.calls if c.role == "PLANNER"]) == 1


async def test_rejected_permission_fails_the_task(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev_needs_permission_once()})
    task = await h.new_task()
    await h.start(task.id)
    approval = (await h.approvals(task.id))[0]
    await h.decide(approval.id, decision="REJECTED", option=None)
    t = await h.task(task.id)
    assert t.status == "FAILED" and t.failure_reason == "PERMISSION_REJECTED"


async def test_timeout_or_idle_is_never_turned_into_a_permission_gate(db, tmp_path):
    def handler(call):
        return ProviderResult(outcome=CliOutcome.IDLE, termination="TERMINATED")
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": handler})
    task = await h.new_task()
    assert await h.start(task.id) == ["FAILED"]
    assert await h.approvals(task.id) == []
    assert (await h.task(task.id)).failure_reason == "STEP_ATTEMPTS_EXHAUSTED"


async def test_design_approval_does_not_widen_cli_permissions(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={
        "PLANNER": lambda call: structured(**(dict(PLAN, human_gate_request=GATE) if call.n == 1 else PLAN))})
    task = await h.new_task()
    await h.start(task.id)
    await h.decide((await h.approvals(task.id))[0].id, option="A")
    assert await h.run_until_idle() == ["COMPLETED"]
    assert all(c.request.one_off_tool is None for c in h.claude.calls + h.codex.calls)
    assert [a.used_at for a in await h.approvals(task.id)] == [None]
