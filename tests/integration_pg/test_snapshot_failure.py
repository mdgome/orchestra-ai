"""Snapshot failures never hide or overwrite the recorded CLI result; stored values of an older snapshot schema are
never reused; and a terminal commit followed by a dying Runner is cleaned up by the next startup."""
import uuid
from pathlib import Path

from sqlalchemy import select, update

from app.infra.workspace_snapshot import SnapshotError
from app.models.states import CliOutcome
from app.models.tables import AgentSession, Execution, ExecutionRequest, Task, TaskRun, TaskStep
from app.providers.base import ProviderResult
from app.repositories.approval_repository import ExecutionRequestRepository
from app.repositories.task_repository import TaskRepository
from app.services import step_runner
from app.services.hashing import stable_hash
from app.services.lease_service import AcquireStatus
from tests.harness import BAD_CODE, GOOD_CODE, Harness, developer_good, structured


class SnapshotSwitch:
    """`take_snapshot` as StepRunner sees it: counts calls and raises SnapshotError once while armed."""

    def __init__(self, monkeypatch):
        self.real = step_runner.take_snapshot
        self.armed = False
        self.calls = 0
        self.raised = 0
        monkeypatch.setattr(step_runner, "take_snapshot", self)

    def __call__(self, root):
        self.calls += 1
        if self.armed:
            self.armed = False
            self.raised += 1
            raise SnapshotError("injected snapshot failure", {"root": str(root)})
        return self.real(root)


async def executions_of(h, task_id, role):
    return [e for e in await h.executions(task_id) if e.role == role]


async def unterminated(db, task_id):
    async with db.transaction() as s:
        return await TaskRepository(s).unterminated_executions(task_id)


# --- snapshot failure after the CLI result ----------------------------------------------------------------------------------
async def test_a_snapshot_failure_after_a_clean_cli_run_keeps_the_recorded_result(db, tmp_path, monkeypatch):
    snap = SnapshotSwitch(monkeypatch)

    def dev(call):
        snap.armed = True                                  # the snapshot taken AFTER this CLI run fails
        return developer_good(call)

    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev})
    task = await h.new_task()
    assert await h.start(task.id) == ["FAILED"]

    done = await h.task(task.id)
    assert done.status == "FAILED" and done.failure_reason == "SNAPSHOT_FAILED" and snap.raised == 1
    (execution,) = await executions_of(h, task.id, "DEVELOPER")
    assert execution.outcome == "SUCCESS" and execution.exit_code == 0 and execution.finished_at is not None
    assert await unterminated(db, task.id) == []           # the finished CLI is NOT classified as unterminated
    (step,) = [s for s in await h.steps_of(task.id) if s.step_key == "DEVELOP"]
    assert step.status == "FAILED" and step.output is None and step.output_hash is None     # never SUCCEEDED / reusable
    async with db.transaction() as s:
        session = (await s.execute(select(AgentSession).where(AgentSession.task_id == task.id,
                                                              AgentSession.agent_role == "DEVELOPER"))).scalar_one_or_none()
    assert session is None or session.status == "CLOSED"   # the session result was recorded, then closed with the task
    assert h.roles_called(h.claude).count("TESTER") == 0


async def test_a_failing_snapshot_before_the_step_creates_no_attempt(db, tmp_path, monkeypatch):
    snap = SnapshotSwitch(monkeypatch)
    h = Harness(db, tmp_path)
    real_begin = h.steps.begin

    async def begin(lease, task_id, step_key, *args, **kw):
        if step_key == "DEVELOP":
            snap.armed = True                              # snapshot_before of the Developer step fails
        return await real_begin(lease, task_id, step_key, *args, **kw)

    monkeypatch.setattr(h.steps, "begin", begin)
    task = await h.new_task()
    assert await h.start(task.id) == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "SNAPSHOT_FAILED"
    assert [s for s in await h.steps_of(task.id) if s.step_key == "DEVELOP"] == []         # no attempt row
    assert await executions_of(h, task.id, "DEVELOPER") == [] and "DEVELOPER" not in h.roles_called(h.claude)


async def test_session_missing_then_a_snapshot_failure_leaves_a_consistent_execution_history(db, tmp_path, monkeypatch):
    snap = SnapshotSwitch(monkeypatch)

    def dev(call):
        if call.n == 2:
            h.claude.live_ids.discard(call.resume_id)      # the CLI forgot its session: call 2 -> SESSION_MISSING
        if call.n == 3:
            snap.armed = True                              # the fresh retry runs fine, its snapshot then fails
        (Path(call.cwd) / "calc.py").write_text(GOOD_CODE if call.n >= 3 else BAD_CODE, encoding="utf-8")
        return structured(summary=f"call {call.n}", files_changed=["calc.py"], human_gate_request=None)

    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev})
    task = await h.new_task()
    assert await h.start(task.id) == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "SNAPSHOT_FAILED" and snap.raised == 1
    runs = await executions_of(h, task.id, "DEVELOPER")
    assert [e.outcome for e in runs] == ["SUCCESS", "SESSION_MISSING", "SUCCESS"]
    assert all(e.finished_at is not None for e in runs) and await unterminated(db, task.id) == []
    develops = [s for s in await h.steps_of(task.id) if s.step_key == "DEVELOP"]
    assert [(s.workflow_iteration, s.step_attempt, s.status) for s in develops] == [(0, 1, "SUCCEEDED"), (1, 1, "FAILED")]


async def test_an_unconfirmed_termination_wins_over_a_snapshot_failure(db, tmp_path, monkeypatch):
    snap = SnapshotSwitch(monkeypatch)

    def hung(call):
        snap.armed = True                                  # a snapshot failure is waiting ...
        return ProviderResult(outcome=CliOutcome.TIMEOUT, termination="STILL_ALIVE")   # ... but the CLI is not gone

    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": hung})
    task = await h.new_task()
    assert await h.start(task.id) == ["WAITING_APPROVAL"]
    t = await h.task(task.id)
    assert t.status == "WAITING_APPROVAL" and t.failure_reason is None          # NOT failed with SNAPSHOT_FAILED
    assert [a.type for a in await h.approvals(task.id)] == ["RECOVERY"]         # the existing Recovery meaning
    assert snap.raised == 0                                # the snapshot was never reached: the result came first
    (execution,) = await executions_of(h, task.id, "DEVELOPER")
    assert execution.outcome == "TIMEOUT" and execution.finished_at is None      # still unterminated: unchanged


async def test_a_result_after_lease_loss_is_discarded_and_no_snapshot_is_taken(db, tmp_path, monkeypatch):
    snap = SnapshotSwitch(monkeypatch)
    seen = {}

    async def slow_dev(call):
        seen["calls"] = snap.calls                         # snapshots taken so far (the one BEFORE the step)
        async with db.transaction() as s:                  # another runner took the task over while the CLI ran
            await s.execute(update(TaskRun).where(TaskRun.status == "RUNNING").values(status="ABANDONED"))
        return developer_good(call)

    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": slow_dev})
    task = await h.new_task()
    await h.tasks.request_execution(task.id)
    async with db.transaction() as s:
        request = await ExecutionRequestRepository(s).claim_next(h.runner.runner_id)
    assert await h.runner.process(request) == "LEASE_LOST"
    assert snap.calls == seen["calls"]                     # LeaseLost surfaced before any snapshot was attempted
    (execution,) = await executions_of(h, task.id, "DEVELOPER")
    assert execution.discarded_result is not None and execution.finished_at is not None
    assert (await h.steps_of(task.id))[-1].status == "INTERRUPTED"              # never a normal completion


# --- stored values of an older snapshot schema are never reused -----------------------------------------------------------------
async def rewind_to_testing(h, task_id):
    async with h.db.transaction() as s:                    # test setup only: let the finished task re-enter TESTING
        await s.execute(update(Task).where(Task.id == task_id).values(status="TESTING", resume_step_key=None))
        await s.execute(update(AgentSession).where(AgentSession.task_id == task_id).values(status="ACTIVE"))  # were CLOSED


async def test_a_stored_test_result_of_an_older_snapshot_schema_is_not_reused(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    assert await h.start(task.id) == ["COMPLETED"]
    testers = h.roles_called(h.claude).count("TESTER")

    await rewind_to_testing(h, task.id)                    # control: identical, current-version fingerprint -> reuse
    assert await h.start(task.id) == ["COMPLETED"]
    assert h.roles_called(h.claude).count("TESTER") == testers

    await rewind_to_testing(h, task.id)                    # the stored value now has the OLD unversioned schema
    async with db.transaction() as s:
        step = (await s.execute(select(TaskStep).where(TaskStep.task_id == task.id, TaskStep.step_key == "TEST")
                                .order_by(TaskStep.created_at.desc()).limit(1))).scalar_one()
        assert step.output["code_fingerprint"].startswith("STEP_SNAPSHOT_V1:")
        old = {**step.output, "code_fingerprint": "a" * 64}
        await s.execute(update(TaskStep).where(TaskStep.id == step.id).values(output=old, output_hash=stable_hash(old)))
    assert await h.start(task.id) == ["COMPLETED"]
    assert h.roles_called(h.claude).count("TESTER") == testers + 1              # NOT reused: the Tester ran again
    fresh = [s for s in await h.steps_of(task.id) if s.step_key == "TEST"][-1]
    assert fresh.output["code_fingerprint"].startswith("STEP_SNAPSHOT_V1:")


# --- terminal commit, then the Runner dies before it finishes the request ----------------------------------------------------
async def test_terminal_commit_then_runner_death_is_cleaned_up_by_the_next_startup(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    await h.tasks.request_execution(task.id)
    async with db.transaction() as s:
        request = await ExecutionRequestRepository(s).claim_next(h.runner.runner_id)
    acquired = await h.leases.acquire(task.id, fence=(request.id, request.claim_token))
    assert acquired.status is AcquireStatus.ACQUIRED
    assert await h.workflow.run_task(acquired.lease, task.id, request_id=request.id) == "COMPLETED"
    # ... the Runner dies HERE: the terminal transaction is committed, Runner.process never finished the request
    async with db.transaction() as s:
        row = (await s.execute(select(ExecutionRequest).where(ExecutionRequest.id == request.id))).scalar_one()
    assert row.status == "CLAIMED" and row.claim_token == request.claim_token      # a separate, later transaction
    assert (await h.task(task.id)).status == "COMPLETED"
    calls = len(h.claude.calls) + len(h.codex.calls)

    async with db.transaction() as s:                      # a coroutine with ANOTHER claim token cannot finish it
        assert await ExecutionRequestRepository(s).finish(request.id, "DONE", "late", token=uuid.uuid4()) is False
        row = (await s.execute(select(ExecutionRequest).where(ExecutionRequest.id == request.id))).scalar_one()
    assert row.status == "CLAIMED"

    second = Harness(db, tmp_path)                         # the next process starts: startup recovery
    await second.runner.start()
    try:
        async with db.transaction() as s:
            row = (await s.execute(select(ExecutionRequest).where(ExecutionRequest.id == request.id))).scalar_one()
        assert row.status == "DONE" and row.note == "task already finished"          # eventual recovery
        assert await second.run_until_idle() == []                                  # the terminal task is not run again
    finally:
        await second.runner.stop()
    assert len(h.claude.calls) + len(h.codex.calls) == calls and not second.claude.calls and not second.codex.calls
    assert (await h.task(task.id)).status == "COMPLETED"
    async with db.transaction() as s:                      # the old owner's late finish is fenced by the status/token
        assert await ExecutionRequestRepository(s).finish(request.id, "DONE", "late", token=request.claim_token) is False
