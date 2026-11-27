"""E1: a Project Test Run is an ordinary Task with mode TEST_ONLY (D4). There is no public endpoint yet, so these tests drive
TaskService.create_test_run and the Runner exactly the way the API will."""
import asyncio
from pathlib import Path

from sqlalchemy import select, update

from app.models.states import StepState
from app.models.tables import ExecutionRequest, Project, Task
from app.services.project_service import ProjectService
from app.services.report_service import ReportService
from app.validation import policies as P
from tests.harness import Harness

PASSING = {"test_ok.py": "def test_ok():\n    assert True\n", "user.txt": "the user's own file\n"}
FAILING = {"test_bad.py": "def test_bad():\n    assert False\n", "user.txt": "the user's own file\n"}


async def make_project(h: Harness, tmp_path: Path, files: dict, name: str = "svc"):
    root = tmp_path / "projects" / name
    root.mkdir(parents=True)
    for rel, content in files.items():
        (root / rel).write_text(content, encoding="utf-8")
    service = ProjectService(h.db, None, clone_root=tmp_path / "clones", protected=(h.guard.root,))
    return await service.create_project(name=name, workspace_path=str(root)), root


async def project_row(h: Harness, project_id) -> Project:
    async with h.db.transaction() as s:
        return (await s.execute(select(Project).where(Project.id == project_id))).scalar_one()


async def request_of(h: Harness, task_id) -> list[ExecutionRequest]:
    async with h.db.transaction() as s:
        return list((await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task_id))).scalars())


def no_agent_may_run(h: Harness) -> None:
    assert not h.claude.calls and not h.codex.calls, "a test run must never call an agent CLI"


# --- running -----------------------------------------------------------------------------------------------------------------
async def test_a_finished_run_is_replayed_with_its_result_and_a_new_key_may_start_the_next(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    first, _ = await h.tasks.create_test_run(project.id, idempotency_key="k1")
    assert await h.run_until_idle() == ["COMPLETED"]
    again, replayed = await h.tasks.create_test_run(project.id, idempotency_key="k1")
    assert replayed and again.id == first.id and again.status == "COMPLETED"
    second, replayed = await h.tasks.create_test_run(project.id, idempotency_key="k2")     # the first one is finished
    assert not replayed and second.id != first.id


async def test_a_passing_test_run_completes_without_calling_any_agent(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path, PASSING)
    before = await project_row(h, project.id)
    task, _ = await h.tasks.create_test_run(project.id)
    assert await h.run_until_idle() == ["COMPLETED"]
    no_agent_may_run(h)
    row = await h.task(task.id)
    assert row.status == "COMPLETED" and row.failure_reason is None and row.cli_invocations == 0
    assert await h.state_path(task.id) == ["PENDING", "TESTING", "COMPLETED"]
    steps = await h.steps_of(task.id)
    assert [(s.step_key, s.status) for s in steps] == [("TEST", str(StepState.SUCCEEDED))]
    assert steps[0].output["mode"] == "TEST_ONLY" and steps[0].output["test_result"]["passed"] is True
    executions = await h.executions(task.id)
    assert any((e.detail or {}).get("orchestrator_test_run") for e in executions)      # the recorded run ReportService reads
    assert not any(e.provider in ("CLAUDE", "CODEX") for e in executions)
    after = await project_row(h, project.id)
    assert after.write_owner_task_id is None and after.workspace_state == "CLEAN"
    assert after.baseline_fingerprint == before.baseline_fingerprint, "a test run never moves the accepted baseline"


async def test_a_failing_test_run_fails_with_a_reason_and_still_records_its_step(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, FAILING)
    task, _ = await h.tasks.create_test_run(project.id)
    assert await h.run_until_idle() == ["FAILED"]
    no_agent_may_run(h)
    row = await h.task(task.id)
    assert row.status == "FAILED" and row.failure_reason == P.TEST_RUN_TESTS_FAILED
    assert await h.state_path(task.id) == ["PENDING", "TESTING", "FAILED"]            # no RETRYING: nothing improves a test run
    steps = await h.steps_of(task.id)
    assert [(s.step_key, s.status) for s in steps] == [("TEST", str(StepState.SUCCEEDED))]
    assert steps[0].output["test_result"]["passed"] is False and steps[0].output["test_result"]["failures"]


async def test_a_run_that_produces_no_junit_fails_with_no_junit(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, {"user.txt": "x\n"})              # no test file at all
    task, _ = await h.tasks.create_test_run(project.id)
    assert await h.run_until_idle() == ["FAILED"]
    assert (await h.task(task.id)).failure_reason in (P.TEST_RUN_NO_JUNIT, P.TEST_RUN_TESTS_FAILED)


async def test_the_workspace_changes_a_test_run_makes_are_never_adopted_as_the_baseline(db, tmp_path):
    files = {**PASSING, "make_byproduct.py": "", "test_writes.py":
             "def test_writes():\n    open('byproduct.txt', 'w').write('left behind by a test')\n"}
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path, files)
    before = await project_row(h, project.id)
    task, _ = await h.tasks.create_test_run(project.id)
    assert await h.run_until_idle() == ["COMPLETED"]
    assert (root / "byproduct.txt").exists()
    after = await project_row(h, project.id)
    assert after.write_owner_task_id is None
    assert after.workspace_state == "DIRTY_UNRESOLVED", "an untracked by-product must not be adopted silently"
    assert after.baseline_fingerprint == before.baseline_fingerprint
    assert (await h.task(task.id)).status == "COMPLETED"                               # the tests themselves passed


async def test_a_workflow_task_still_adopts_its_result_as_the_baseline(db, tmp_path):
    from tests.harness import developer_good
    h = Harness(db, tmp_path / "ws", claude_handlers={"DEVELOPER": developer_good})
    project, _root = await make_project(h, tmp_path, {"user.txt": "x\n"})
    before = await project_row(h, project.id)
    task = await h.tasks.create_task(requirements="implement add(a, b)", project_id=project.id)
    assert await h.start(task.id) == ["COMPLETED"]
    after = await project_row(h, project.id)
    assert after.workspace_state == "CLEAN" and after.baseline_fingerprint != before.baseline_fingerprint


# --- the project write lock --------------------------------------------------------------------------------------------------
async def test_a_test_run_defers_while_a_workflow_task_owns_the_project_and_then_runs(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path, PASSING)
    other = await h.tasks.create_task(requirements="holds the lock", project_id=project.id)
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(
            write_owner_task_id=other.id, write_baseline_fingerprint=(await project_row(h, project.id)).baseline_fingerprint,
            write_epoch=Project.write_epoch + 1))
    run, _ = await h.tasks.create_test_run(project.id)
    assert await h.run_until_idle() == ["BLOCKED"]
    # PENDING -> TESTING happens before the lock is asked for (a gate needs a state that may wait); no step attempt exists yet
    assert (await h.task(run.id)).status == "TESTING" and await h.steps_of(run.id) == []
    [req] = await request_of(h, run.id)
    assert req.status == "PENDING" and req.defer_count == 1


async def test_a_test_run_gives_up_waiting_for_a_busy_project_after_the_limit(db, tmp_path, monkeypatch):
    monkeypatch.setattr(P, "TEST_RUN_MAX_DEFERS", 2)
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    other = await h.tasks.create_task(requirements="holds the lock", project_id=project.id)
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(
            write_owner_task_id=other.id, write_baseline_fingerprint=(await project_row(h, project.id)).baseline_fingerprint,
            write_epoch=Project.write_epoch + 1))
    run, _ = await h.tasks.create_test_run(project.id)
    outcomes = []
    for _ in range(4):
        async with db.transaction() as s:
            await s.execute(update(ExecutionRequest).where(ExecutionRequest.task_id == run.id,
                                                           ExecutionRequest.status == "PENDING").values(not_before=None))
        outcomes += await h.run_until_idle()
        if (await h.task(run.id)).status == "FAILED":
            break
    assert outcomes == ["BLOCKED", "BLOCKED", "FAILED"]
    assert (await h.task(run.id)).failure_reason == P.TEST_RUN_PROJECT_BUSY
    assert (await project_row(h, project.id)).write_owner_task_id == other.id          # the other task keeps its lock


async def test_a_workflow_task_keeps_deferring_without_a_limit(db, tmp_path, monkeypatch):
    monkeypatch.setattr(P, "TEST_RUN_MAX_DEFERS", 1)
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    owner = await h.tasks.create_task(requirements="holds the lock", project_id=project.id)
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(
            write_owner_task_id=owner.id, write_baseline_fingerprint=(await project_row(h, project.id)).baseline_fingerprint,
            write_epoch=Project.write_epoch + 1))
    waiting = await h.tasks.create_task(requirements="wants the lock", project_id=project.id)
    await h.tasks.request_execution(waiting.id)
    for _ in range(3):
        async with db.transaction() as s:
            await s.execute(update(ExecutionRequest).where(ExecutionRequest.task_id == waiting.id,
                                                           ExecutionRequest.status == "PENDING").values(not_before=None))
        assert await h.run_until_idle() == ["BLOCKED"]
    assert (await h.task(waiting.id)).status not in ("FAILED", "COMPLETED", "CANCELLED")      # still waiting, never failed


# --- a gate on a Task without a plan ---------------------------------------------------------------------------------------
async def dirty_project_with_a_run(h: Harness, tmp_path, files=PASSING):
    project, root = await make_project(h, tmp_path, files)
    async with h.db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(workspace_state="DIRTY_UNRESOLVED"))
    run, _ = await h.tasks.create_test_run(project.id)
    assert await h.run_until_idle() == ["WAITING_APPROVAL"]
    return project, root, run


async def test_a_dirty_project_opens_the_gate_and_continue_resumes_the_run_in_testing(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root, run = await dirty_project_with_a_run(h, tmp_path)
    task = await h.task(run.id)
    assert task.status == "WAITING_APPROVAL" and await h.steps_of(run.id) == []             # the gate came before any attempt
    [approval] = await h.approvals(run.id)
    assert approval.checkpoint_step_key == "TEST" and approval.request_payload["reason_code"] == P.RECOVERY_REASON_PROJECT_DIRTY
    outcome = await h.decide(approval.id, "APPROVED", "continue", key="k-continue")     # a Task without a plan is decidable
    assert outcome.created
    assert await h.run_until_idle() == ["COMPLETED"]
    no_agent_may_run(h)
    assert await h.state_path(run.id) == ["PENDING", "TESTING", "WAITING_APPROVAL", "TESTING", "COMPLETED"]
    assert (await project_row(h, project.id)).write_owner_task_id is None


async def test_aborting_the_gate_fails_the_run_and_leaves_the_project_untouched(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root, run = await dirty_project_with_a_run(h, tmp_path)
    [approval] = await h.approvals(run.id)
    await h.decide(approval.id, "APPROVED", "abort", key="k-abort")
    task = await h.task(run.id)
    assert task.status == "FAILED" and task.failure_reason == "PROJECT_DIRTY_ABORTED"
    assert (await project_row(h, project.id)).workspace_state == "DIRTY_UNRESOLVED"
    assert (root / "user.txt").exists()


# --- cancellation ---------------------------------------------------------------------------------------------------------------
async def test_a_cancelled_idle_run_is_cancelled_at_once(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root, run = await dirty_project_with_a_run(h, tmp_path)
    await h.tasks.request_cancellation(run.id)
    assert await h.runner._cancel_idle_tasks() == 1
    assert (await h.task(run.id)).status == "CANCELLED"
    assert (await project_row(h, project.id)).write_owner_task_id is None


async def test_a_run_cancelled_before_it_starts_never_runs_a_command(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path, PASSING)
    run, _ = await h.tasks.create_test_run(project.id)
    await h.tasks.request_cancellation(run.id)
    assert await h.run_until_idle() == ["CANCELLED"]
    assert (await h.task(run.id)).status == "CANCELLED" and not (root / "reports").exists()
    no_agent_may_run(h)


async def test_a_run_cancelled_while_its_command_runs_is_cancelled_after_the_tree_is_gone(db, tmp_path):
    slow = {"test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(60)\n", "user.txt": "x\n"}
    h = Harness(db, tmp_path / "ws", cancel_poll=0.05)
    project, root = await make_project(h, tmp_path, slow)
    run, _ = await h.tasks.create_test_run(project.id)
    worker = asyncio.create_task(h.run_until_idle())

    async def command_started():
        return any(e.outcome == "STARTING" for e in await h.executions(run.id))
    from tests.harness import wait_for
    assert await wait_for(command_started, timeout=30)
    await h.tasks.request_cancellation(run.id)
    assert await asyncio.wait_for(worker, timeout=60) == ["CANCELLED"]
    task = await h.task(run.id)
    assert task.status == "CANCELLED"
    assert all(e.finished_at is not None for e in await h.executions(run.id))        # nothing is left unproven
    row = await project_row(h, project.id)
    assert row.write_owner_task_id is None
    no_agent_may_run(h)


# --- the report does not break for a Task that has no plan ----------------------------------------------------------------
async def test_the_report_and_the_artifact_list_answer_for_a_finished_test_run(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path, PASSING)
    task, _ = await h.tasks.create_test_run(project.id)
    assert await h.run_until_idle() == ["COMPLETED"]
    reports = ReportService(db)
    report = await reports.report(task.id)
    assert report["task"]["status"] == "COMPLETED"
    assert report["tests"]["passed"] is True                               # the test step is read like any other TEST step
    page = await reports.artifact_list(task.id, None, 20, 0)
    assert [item["kind"] for item in page["items"]] == ["TEST"]
