"""Project write lock, workspace baselines, the PROJECT_DIRTY recovery gate and the terminal proof protocol."""
import asyncio
import dataclasses
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.infra.workspace_fingerprint import compute_fingerprint
from app.models.states import StepState, TaskState
from app.models.tables import Approval, ApprovalDecision, ExecutionRequest, Project, Task, TaskStep
from app.repositories.project_repository import ProjectRepository
from app.services.human_gate_service import ProofUnstable
from app.services.project_service import ProjectService
from app.services.project_terminal import UNMEASURED
from app.services.state_service import change_task_state
from app.validation import policies as P
from app.validation.exceptions import ValidationFailed
from tests.harness import GATE, GOOD_CODE, Harness, developer_good, structured, recover_for_test

TESTER_GATE = lambda call: structured(summary="need a decision", tests_added=[], human_gate_request=GATE)  # noqa: E731


# --- helpers -----------------------------------------------------------------------------------------------------------
async def make_project(h: Harness, tmp_path: Path, name: str = "svc", files: dict | None = None):
    root = tmp_path / "projects" / name
    root.mkdir(parents=True)
    for rel, content in (files or {"user.txt": "the user's own file\n"}).items():
        (root / rel).write_text(content, encoding="utf-8")
    service = ProjectService(h.db, None, clone_root=tmp_path / "clones", protected=(h.guard.root,))
    return await service.create_project(name=name, workspace_path=str(root)), root


async def new_task(h: Harness, project, text_="implement add(a, b)", **kw) -> Task:
    return await h.tasks.create_task(requirements=text_, project_id=project.id, **kw)


async def project_row(h: Harness, project_id) -> Project:
    async with h.db.transaction() as s:
        return (await s.execute(select(Project).where(Project.id == project_id))).scalar_one()


async def requests_of(h: Harness, task_id) -> list[ExecutionRequest]:
    async with h.db.transaction() as s:
        return list((await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task_id))).scalars())


async def claim_and_process(h: Harness, task_id, *, ready=True) -> str:
    """Claims this task's request (clearing a backoff first when `ready`) and runs it the way the Runner would."""
    await h.tasks.request_execution(task_id)
    async with h.db.transaction() as s:
        if ready:
            await s.execute(update(ExecutionRequest).where(ExecutionRequest.task_id == task_id,
                                                           ExecutionRequest.status == "PENDING")
                            .values(not_before=None))
    from app.repositories.approval_repository import ExecutionRequestRepository
    async with h.db.transaction() as s:
        request = await ExecutionRequestRepository(s).claim_next(h.runner.runner_id)
    assert request is not None and request.task_id == task_id
    return await h.runner.process(request)


async def resume_pending(h: Harness) -> list[str]:
    return await h.run_until_idle()


def fp(path: Path) -> str:
    return compute_fingerprint(path).value


async def steps_of_key(h, task_id, key) -> list[TaskStep]:
    return [s for s in await h.steps_of(task_id) if s.step_key == key]


async def make_owner(h: Harness, project, task, root: Path) -> str:
    """Puts the project into 'task owns the write lock' directly (unit-level setup for the terminal protocol)."""
    baseline = fp(root)
    async with h.db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(
            write_owner_task_id=task.id, write_baseline_fingerprint=baseline, baseline_fingerprint=baseline,
            write_epoch=Project.write_epoch + 1))
    return baseline


async def fail_with(h: Harness, task_id) -> None:
    async def work(s, task, proof):
        await change_task_state(s, task, TaskState.FAILED, reason="test", failure_reason="TEST", project_proof=proof)
    await h.finalizer.finalize_task_with_proof(task_id, work)


# --- lifecycle -----------------------------------------------------------------------------------------------------------
async def test_lock_is_taken_at_the_first_changing_step_and_released_clean_on_completion(db, tmp_path):
    seen = {}

    async def dev(call):
        seen["during"] = await project_row(h, project.id)
        return developer_good(call)

    h = Harness(db, tmp_path / "ws", claude_handlers={"DEVELOPER": dev})
    project, root = await make_project(h, tmp_path)
    before = await project_row(h, project.id)
    assert before.write_owner_task_id is None and before.write_baseline_fingerprint is None
    assert before.baseline_fingerprint == fp(root)                       # registration baseline (user.txt only)
    task = await new_task(h, project)
    assert await h.start(task.id) == ["COMPLETED"]

    during = seen["during"]
    assert during.write_owner_task_id == task.id and during.write_baseline_fingerprint == before.baseline_fingerprint
    assert during.write_epoch > before.write_epoch
    after = await project_row(h, project.id)
    assert after.write_owner_task_id is None and after.write_baseline_fingerprint is None      # released together
    assert after.workspace_state == "CLEAN" and after.baseline_fingerprint == fp(root)
    assert after.baseline_fingerprint != before.baseline_fingerprint     # the accepted baseline moved to the result
    assert (root / "calc.py").read_text(encoding="utf-8") == GOOD_CODE and (root / "user.txt").exists()


async def test_owner_and_write_baseline_always_come_together(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    for values in ({"write_owner_task_id": task.id}, {"write_baseline_fingerprint": "WORKSPACE_FINGERPRINT_V1:x"}):
        with pytest.raises(IntegrityError):
            async with db.transaction() as s:
                await s.execute(update(Project).where(Project.id == project.id).values(**values))
    async with db.transaction() as s:                                    # both together is fine
        await s.execute(update(Project).where(Project.id == project.id).values(
            write_owner_task_id=task.id, write_baseline_fingerprint="WORKSPACE_FINGERPRINT_V1:x"))


async def test_second_task_cannot_change_the_workspace_while_the_first_runs(db, tmp_path):
    in_dev, release = asyncio.Event(), asyncio.Event()

    async def dev(call):
        in_dev.set()
        await release.wait()
        return developer_good(call)

    h = Harness(db, tmp_path / "ws", claude_handlers={"DEVELOPER": dev})
    project, _root = await make_project(h, tmp_path)
    a, b = await new_task(h, project), await new_task(h, project, "second change here")
    first = asyncio.create_task(claim_and_process(h, a.id))
    await asyncio.wait_for(in_dev.wait(), 30)

    planner_calls = len(h.claude.calls)
    assert await claim_and_process(h, b.id) == "BLOCKED"                 # A is changing the workspace right now
    assert len(h.claude.calls) == planner_calls                          # B did not even start a CLI
    row = (await requests_of(h, b.id))[0]
    assert row.status == "PENDING" and row.defer_count == 1 and row.not_before is not None
    assert row.claim_token is None and row.runner_id is None and row.task_run_id is None
    assert await steps_of_key(h, b.id, "PLAN") == [] and (await h.task(b.id)).cli_invocations == 0

    release.set()
    assert await first == "COMPLETED"
    assert await claim_and_process(h, b.id) == "COMPLETED"               # after A: B runs normally
    assert (await project_row(h, project.id)).write_owner_task_id is None


async def test_waiting_for_the_lock_consumes_no_attempt_cli_or_iteration(db, tmp_path):
    h = Harness(db, tmp_path / "ws", claude_handlers={"TESTER": TESTER_GATE})
    project, _root = await make_project(h, tmp_path)
    a, b = await new_task(h, project), await new_task(h, project, "second change here")
    assert await claim_and_process(h, a.id) == "WAITING_APPROVAL"        # A parks at its TEST gate, still the owner
    owner = await project_row(h, project.id)
    assert owner.write_owner_task_id == a.id and owner.write_baseline_fingerprint is not None   # lock is kept
    assert await h.runs(a.id) and all(r.status != "RUNNING" for r in await h.runs(a.id))        # lease was released

    planner_before = len([c for c in h.claude.calls if c.role == "PLANNER"])
    assert await claim_and_process(h, b.id) == "BLOCKED"                 # B plans freely (A is parked) ...
    assert len([c for c in h.claude.calls if c.role == "PLANNER"]) == planner_before + 1   # ... then waits at DEVELOP
    tb = await h.task(b.id)
    assert tb.status == str(TaskState.DEVELOPING)
    assert await steps_of_key(h, b.id, "DEVELOP") == []                  # no step attempt exists
    cli_after_first_wait, iterations, replans = tb.cli_invocations, tb.workflow_iteration, tb.replan_count
    assert await claim_and_process(h, b.id) == "BLOCKED"                 # a second wait changes nothing
    tb = await h.task(b.id)
    assert (tb.cli_invocations, tb.workflow_iteration, tb.replan_count) == (cli_after_first_wait, iterations, replans)
    assert (await requests_of(h, b.id))[-1].defer_count == 2             # the persistent backoff keeps growing


async def test_a_parked_owner_that_is_cancelled_releases_the_lock_at_once_and_marks_dirty(db, tmp_path):
    h = Harness(db, tmp_path / "ws", claude_handlers={"TESTER": TESTER_GATE})
    project, root = await make_project(h, tmp_path)
    a = await new_task(h, project)
    assert await claim_and_process(h, a.id) == "WAITING_APPROVAL"
    await h.tasks.request_cancellation(a.id)
    assert await h.runner._cancel_idle_tasks() == 1
    assert (await h.task(a.id)).status == "CANCELLED"
    row = await project_row(h, project.id)
    assert row.write_owner_task_id is None and row.write_baseline_fingerprint is None   # same transaction
    assert row.workspace_state == "DIRTY_UNRESOLVED"                     # A had written calc.py; nothing was discarded
    assert (root / "calc.py").exists() and (root / "user.txt").exists()


# --- terminal paths: CLEAN vs DIRTY -------------------------------------------------------------------------------------
async def test_failure_after_taking_the_lock_without_any_change_is_clean(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project, limits={"max_cli_invocations": 2})   # PLAN + DESIGN_REVIEW, then the limit hits
    assert await h.start(task.id) == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "CLI_INVOCATIONS_EXCEEDED"
    row = await project_row(h, project.id)
    assert row.workspace_state == "CLEAN" and row.write_owner_task_id is None and row.write_baseline_fingerprint is None
    assert not (root / "calc.py").exists()


async def test_failure_after_changes_is_dirty_and_never_cleans_the_workspace(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project, limits={"max_cli_invocations": 3})   # PLAN, REVIEW, DEVELOP writes, TEST is refused
    assert await h.start(task.id) == ["FAILED"]
    row = await project_row(h, project.id)
    assert row.workspace_state == "DIRTY_UNRESOLVED" and row.write_owner_task_id is None
    assert (root / "calc.py").read_text(encoding="utf-8") == GOOD_CODE      # the user's tree is left exactly as is
    assert (root / "user.txt").exists()


async def test_user_changes_made_while_the_project_is_idle_are_accepted_at_the_next_first_lock(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    first = await new_task(h, project)
    assert await h.start(first.id) == ["COMPLETED"]
    (root / "user.txt").write_text("edited by the user between tasks\n", encoding="utf-8")   # idle: allowed
    second = await new_task(h, project, "another change please", limits={"max_cli_invocations": 2})
    assert await h.start(second.id) == ["FAILED"]                          # fails right after taking the lock
    row = await project_row(h, project.id)
    assert row.workspace_state == "CLEAN"                                  # the task itself changed nothing
    assert row.baseline_fingerprint == fp(root)                            # and the user's edit became the baseline


async def test_reentry_by_the_same_task_keeps_the_original_write_baseline(db, tmp_path):
    h = Harness(db, tmp_path / "ws", claude_handlers={"TESTER": TESTER_GATE})
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    assert await claim_and_process(h, task.id) == "WAITING_APPROVAL"
    first = await project_row(h, project.id)
    approval = (await h.approvals(task.id))[0]
    await h.decide(approval.id, "APPROVED", "A")
    # the approved gate resumes the TEST step (the scripted tester asks again, so the task parks a second time):
    # the same task re-enters the lock, and its write baseline is never overwritten
    assert await h.run_until_idle() == ["WAITING_APPROVAL"]
    mid = await project_row(h, project.id)
    assert mid.write_owner_task_id == task.id
    assert mid.write_baseline_fingerprint == first.write_baseline_fingerprint
    assert mid.baseline_fingerprint == first.baseline_fingerprint


# --- the PROJECT_DIRTY recovery gate ------------------------------------------------------------------------------------
async def dirty_project(h, tmp_path):
    """A project left DIRTY_UNRESOLVED by a failed task, plus a second task that now wants the lock."""
    project, root = await make_project(h, tmp_path)
    failed = await new_task(h, project, limits={"max_cli_invocations": 3})
    assert await h.start(failed.id) == ["FAILED"]
    assert (await project_row(h, project.id)).workspace_state == "DIRTY_UNRESOLVED"
    return project, root


async def test_dirty_gate_opens_before_any_step_attempt_and_consumes_nothing(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await dirty_project(h, tmp_path)
    waiting = await new_task(h, project, "wants the lock now")
    assert await h.start(waiting.id) == ["WAITING_APPROVAL"]
    task = await h.task(waiting.id)
    approval = (await h.approvals(waiting.id))[0]
    assert task.status == str(TaskState.WAITING_APPROVAL) and approval.type == "RECOVERY" and approval.step_id is None
    assert approval.target["reason_code"] == P.RECOVERY_REASON_PROJECT_DIRTY
    assert approval.request_payload["reason_code"] == P.RECOVERY_REASON_PROJECT_DIRTY
    assert [o["id"] for o in approval.request_payload["options"]] == ["continue", "abort"]
    assert approval.checkpoint_step_key == "DEVELOP" and task.resume_step_key == "DEVELOP"
    assert approval.target["project_id"] == str(project.id)
    assert await steps_of_key(h, waiting.id, "DEVELOP") == []               # the gate came BEFORE the attempt
    assert all(r.status != "RUNNING" for r in await h.runs(waiting.id))     # the lease was released
    assert task.workflow_iteration == 0 and task.replan_count == 0
    assert (await project_row(h, project.id)).write_owner_task_id is None   # nothing was acquired


async def test_only_one_pending_project_dirty_approval_per_task(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await dirty_project(h, tmp_path)
    waiting = await new_task(h, project, "wants the lock now")
    await h.start(waiting.id)
    with pytest.raises(IntegrityError):
        async with db.transaction() as s:
            task = await s.get(Task, waiting.id)
            await h.gates.open_recovery(
                s, task, None, reason="again", checkpoint_step_key="DEVELOP", reason_code=P.RECOVERY_REASON_PROJECT_DIRTY,
                options=P.PROJECT_DIRTY_OPTIONS, target_extra={"project_id": str(project.id), "dirty_epoch": 0})


async def test_continue_accepts_the_current_state_and_the_task_resumes_with_a_fresh_attempt(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await dirty_project(h, tmp_path)
    waiting = await new_task(h, project, "wants the lock now")
    await h.start(waiting.id)
    approval = (await h.approvals(waiting.id))[0]
    cli_before = (await h.task(waiting.id)).cli_invocations

    outcome = await h.decide(approval.id, "APPROVED", "continue", key="k-continue")
    assert outcome.created
    row = await project_row(h, project.id)
    assert row.workspace_state == "CLEAN" and row.baseline_fingerprint == fp(root)     # accepted as is
    assert [r.kind for r in await requests_of(h, waiting.id)][-1] == "RESUME"
    assert (await h.task(waiting.id)).cli_invocations == cli_before                    # waiting consumed nothing

    assert await h.run_until_idle() == ["COMPLETED"]
    develops = await steps_of_key(h, waiting.id, "DEVELOP")
    assert [s.step_attempt for s in develops] == [1] and develops[0].status == str(StepState.SUCCEEDED)
    assert (await project_row(h, project.id)).write_owner_task_id is None
    repeated = await h.decide(approval.id, "APPROVED", "continue", key="k-continue")    # idempotent
    assert not repeated.created


async def test_abort_and_reject_fail_the_task_but_leave_the_project_dirty_and_untouched(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await dirty_project(h, tmp_path)
    snapshot = fp(root)
    for decision, option, reason in (("APPROVED", "abort", "PROJECT_DIRTY_ABORTED"),
                                     ("REJECTED", None, "PROJECT_DIRTY_REJECTED")):
        waiting = await new_task(h, project, f"wants the lock: {reason}")
        await h.start(waiting.id)
        approval = (await h.approvals(waiting.id))[0]
        await h.decide(approval.id, decision, option, key=f"k-{reason}")
        task = await h.task(waiting.id)
        assert task.status == str(TaskState.FAILED) and task.failure_reason == reason
        row = await project_row(h, project.id)
        assert row.workspace_state == "DIRTY_UNRESOLVED" and row.write_owner_task_id is None
        assert fp(root) == snapshot and (root / "user.txt").exists()                      # nothing was reset or cleaned
        assert not [r for r in await requests_of(h, waiting.id) if r.kind == "RESUME"]


async def test_retry_is_not_an_offered_option_for_a_dirty_gate(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await dirty_project(h, tmp_path)
    waiting = await new_task(h, project, "wants the lock now")
    await h.start(waiting.id)
    approval = (await h.approvals(waiting.id))[0]
    with pytest.raises(ValidationFailed):
        await h.decide(approval.id, "APPROVED", "retry", key="k-retry")


async def test_a_second_waiting_task_resumes_without_touching_the_baseline_once_the_project_is_clean(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await dirty_project(h, tmp_path)
    first, second = await new_task(h, project, "first waiting task"), await new_task(h, project, "second waiting task")
    await h.start(first.id)
    await h.start(second.id)
    a1, a2 = (await h.approvals(first.id))[0], (await h.approvals(second.id))[0]
    await h.decide(a1.id, "APPROVED", "continue", key="k1")
    accepted = (await project_row(h, project.id)).baseline_fingerprint
    await h.decide(a2.id, "APPROVED", "continue", key="k2")                # already resolved: just resumes
    assert (await project_row(h, project.id)).baseline_fingerprint == accepted
    assert await h.run_until_idle() == ["COMPLETED", "COMPLETED"]


async def test_each_recovery_kind_runs_its_own_effect(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await dirty_project(h, tmp_path)
    waiting = await new_task(h, project, "wants the lock now")
    await h.start(waiting.id)
    dirty_gate = (await h.approvals(waiting.id))[0]
    async with db.transaction() as s:
        legacy = await h.tasks.create_task(requirements="legacy task needing recovery")
        await s.execute(update(Task).where(Task.id == legacy.id).values(status="PLANNING"))
        task = await s.get(Task, legacy.id)
        await s.refresh(task)
        uncertain = await h.gates.open_recovery(s, task, None, reason="cli unconfirmed", checkpoint_step_key="PLAN")
    assert uncertain.target["reason_code"] == P.RECOVERY_REASON_EXECUTION_UNCERTAIN
    assert uncertain.request_payload["reason_code"] == P.RECOVERY_REASON_EXECUTION_UNCERTAIN
    assert [o["id"] for o in uncertain.request_payload["options"]] == ["continue", "retry", "abort"]
    assert self_effect(h, dirty_gate) is not None and self_effect(h, uncertain) is None
    await h.decide(uncertain.id, "APPROVED", "continue", key="k-u")        # generic effect: a RESUME request, no project
    assert (await project_row(h, project.id)).workspace_state == "DIRTY_UNRESOLVED"     # the project was not touched


def self_effect(h, approval):
    return h.gates._effect_for(approval)


async def test_a_stale_proof_rolls_the_whole_decision_back_and_retries_as_one_decision(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await dirty_project(h, tmp_path)
    waiting = await new_task(h, project, "wants the lock now")
    await h.start(waiting.id)
    approval = (await h.approvals(waiting.id))[0]
    real, calls = h.locks.measure_project, {"n": 0}

    async def stale_once(project_id):
        calls["n"] += 1
        proof = await real(project_id)
        return dataclasses.replace(proof, write_epoch=proof.write_epoch - 1) if calls["n"] == 1 else proof

    monkeypatch.setattr(h.locks, "measure_project", stale_once)
    outcome = await h.decide(approval.id, "APPROVED", "continue", key="k-stale")
    assert calls["n"] == 2 and outcome.created
    async with db.transaction() as s:
        assert len((await s.execute(select(ApprovalDecision))).scalars().all()) == 1
    assert (await project_row(h, project.id)).workspace_state == "CLEAN"


async def test_an_unmeasurable_workspace_never_commits_a_decision(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, _root = await dirty_project(h, tmp_path)
    waiting = await new_task(h, project, "wants the lock now")
    await h.start(waiting.id)
    approval = (await h.approvals(waiting.id))[0]

    async def never(_project_id):
        return UNMEASURED

    monkeypatch.setattr(h.locks, "measure_project", never)
    with pytest.raises(ProofUnstable):
        await h.decide(approval.id, "APPROVED", "continue", key="k-unmeasured")
    async with db.transaction() as s:
        assert (await s.execute(select(ApprovalDecision))).scalars().all() == []
        assert (await s.get(Approval, approval.id)).status == "PENDING"            # not DECIDED
    assert [r.kind for r in await requests_of(h, waiting.id)] == ["START"]          # no RESUME was created
    assert (await h.task(waiting.id)).status == str(TaskState.WAITING_APPROVAL)
    assert (await project_row(h, project.id)).workspace_state == "DIRTY_UNRESOLVED"


# --- terminal proof protocol (finalize_task_with_proof) -------------------------------------------------------------------
async def test_finalize_cleans_up_when_nothing_changed(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    baseline = await make_owner(h, project, task, root)
    before = await project_row(h, project.id)
    await fail_with(h, task.id)
    row = await project_row(h, project.id)
    assert (await h.task(task.id)).status == "FAILED"
    assert row.write_owner_task_id is None and row.write_baseline_fingerprint is None
    assert row.workspace_state == "CLEAN" and row.baseline_fingerprint == baseline and row.write_epoch == before.write_epoch + 1


async def test_finalize_marks_dirty_when_the_workspace_differs_from_the_write_baseline(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)
    (root / "new.py").write_text("x = 1\n", encoding="utf-8")
    await fail_with(h, task.id)
    assert (await project_row(h, project.id)).workspace_state == "DIRTY_UNRESOLVED"


async def test_stale_proof_is_discarded_and_measured_again(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)
    real, calls = h.locks.measure_for_task, {"n": 0}

    async def stale_first(task_id):
        calls["n"] += 1
        proof = await real(task_id)
        return dataclasses.replace(proof, write_epoch=proof.write_epoch - 1) if calls["n"] == 1 else proof

    monkeypatch.setattr(h.locks, "measure_for_task", stale_first)
    await fail_with(h, task.id)
    row = await project_row(h, project.id)
    assert calls["n"] == 2 and row.workspace_state == "CLEAN" and row.write_owner_task_id is None


async def test_three_stale_proofs_end_in_one_unmeasured_transaction_that_is_dirty(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)
    real, calls = h.locks.measure_for_task, {"n": 0}

    async def always_stale(task_id):
        calls["n"] += 1
        proof = await real(task_id)
        return dataclasses.replace(proof, write_epoch=proof.write_epoch - 1) if proof else proof

    monkeypatch.setattr(h.locks, "measure_for_task", always_stale)
    await fail_with(h, task.id)
    row = await project_row(h, project.id)
    assert calls["n"] == P.PROOF_MAX_ATTEMPTS
    assert (await h.task(task.id)).status == "FAILED"
    assert row.workspace_state == "DIRTY_UNRESOLVED" and row.write_owner_task_id is None
    assert row.write_baseline_fingerprint is None                          # never CLEAN on unproven evidence


async def test_a_workspace_that_cannot_be_measured_is_dirty_not_clean(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)

    async def broken(_task_id):
        return UNMEASURED

    monkeypatch.setattr(h.locks, "measure_for_task", broken)
    await fail_with(h, task.id)
    assert (await project_row(h, project.id)).workspace_state == "DIRTY_UNRESOLVED"


async def test_if_the_owner_changed_the_other_tasks_lock_is_left_alone(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task, other = await new_task(h, project), await new_task(h, project, "the new owner task")
    await make_owner(h, project, task, root)
    real, done = h.locks.measure_for_task, {"flipped": False}

    async def hand_over(task_id):
        proof = await real(task_id)
        if not done["flipped"]:
            done["flipped"] = True
            async with db.transaction() as s:
                await s.execute(update(Project).where(Project.id == project.id).values(
                    write_owner_task_id=other.id, write_baseline_fingerprint="WORKSPACE_FINGERPRINT_V1:other"))
        return proof

    monkeypatch.setattr(h.locks, "measure_for_task", hand_over)
    await fail_with(h, task.id)
    row = await project_row(h, project.id)
    assert (await h.task(task.id)).status == "FAILED"
    assert row.write_owner_task_id == other.id and row.write_baseline_fingerprint == "WORKSPACE_FINGERPRINT_V1:other"


async def test_finalizing_an_already_terminal_task_is_idempotent(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)
    await fail_with(h, task.id)
    await fail_with(h, task.id)
    assert (await h.task(task.id)).status == "FAILED"


async def test_task_and_project_change_together_nothing_in_between_is_visible(db, tmp_path, monkeypatch):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)
    paused, resume = asyncio.Event(), asyncio.Event()
    original = ProjectRepository.release

    async def slow_release(self, project_id, **kw):
        paused.set()
        await resume.wait()
        return await original(self, project_id, **kw)

    monkeypatch.setattr(ProjectRepository, "release", slow_release)
    runner = asyncio.create_task(fail_with(h, task.id))
    await asyncio.wait_for(paused.wait(), 30)
    # mid-transaction: the task row was already updated inside it, but another session sees NEITHER change
    observed_task = await h.task(task.id)
    observed_project = await project_row(h, project.id)
    assert observed_task.status == str(TaskState.PENDING)
    assert observed_project.write_owner_task_id == task.id and observed_project.workspace_state == "CLEAN"
    resume.set()
    await runner
    assert (await h.task(task.id)).status == "FAILED"
    assert (await project_row(h, project.id)).write_owner_task_id is None


# --- reconcile -----------------------------------------------------------------------------------------------------------------
async def test_reconcile_releases_a_lock_left_by_a_finished_task_and_never_assumes_clean(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    await make_owner(h, project, task, root)
    async with db.transaction() as s:                                     # the crash window: task terminal, lock left
        await s.execute(update(Task).where(Task.id == task.id).values(status="FAILED"))
    assert await h.locks.reconcile() == 1
    row = await project_row(h, project.id)
    assert row.write_owner_task_id is None and row.write_baseline_fingerprint is None
    assert row.workspace_state == "CLEAN"                                  # nothing differs from the write baseline

    other = await new_task(h, project, "another one")
    await make_owner(h, project, other, root)
    (root / "left-behind.py").write_text("x = 1\n", encoding="utf-8")
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == other.id).values(status="CANCELLED"))
    await recover_for_test(h.runner)
    assert (await project_row(h, project.id)).workspace_state == "DIRTY_UNRESOLVED"


async def test_reconcile_with_an_old_fingerprint_version_is_dirty(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    async with db.transaction() as s:
        await s.execute(update(Project).where(Project.id == project.id).values(
            write_owner_task_id=task.id, write_baseline_fingerprint="WORKSPACE_FINGERPRINT_V0:old"))
        await s.execute(update(Task).where(Task.id == task.id).values(status="FAILED"))
    await h.locks.reconcile()
    assert (await project_row(h, project.id)).workspace_state == "DIRTY_UNRESOLVED"     # versions are never compared


# --- read-only steps and epochs ---------------------------------------------------------------------------------------------------
async def test_a_read_only_step_that_overlaps_a_project_change_is_not_reused_and_is_bounded(db, tmp_path):
    async def planner(call):
        async with db.transaction() as s:                                # somebody changed the project meanwhile
            await s.execute(update(Project).values(write_epoch=Project.write_epoch + 1))
        return structured(**__import__("tests.harness", fromlist=["PLAN"]).PLAN)

    h = Harness(db, tmp_path / "ws", claude_handlers={"PLANNER": planner})
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    outcomes = []
    for _ in range(P.PROJECT_EPOCH_RETRIES + 1):
        outcomes.append(await claim_and_process(h, task.id))
    assert outcomes == ["BLOCKED"] * P.PROJECT_EPOCH_RETRIES + ["FAILED"]
    done = await h.task(task.id)
    assert done.failure_reason == "PROJECT_WORKSPACE_UNSTABLE"
    plans = await steps_of_key(h, task.id, "PLAN")
    assert len(plans) == P.PROJECT_EPOCH_RETRIES + 1
    assert all(s.status == str(StepState.CANCELLED) and s.error == "PROJECT_CHANGED" for s in plans)   # never reused
    assert done.workflow_iteration == 0 and done.replan_count == 0 and done.cli_invocations == len(plans)
    assert (await project_row(h, project.id)).workspace_state == "CLEAN"        # read-only: nothing to mark dirty


async def test_an_unchanged_project_keeps_normal_checkpoint_reuse(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    project, _root = await make_project(h, tmp_path)
    task = await new_task(h, project)
    assert await h.start(task.id) == ["COMPLETED"]
    plans = await steps_of_key(h, task.id, "PLAN")
    assert len(plans) == 1 and plans[0].status == str(StepState.SUCCEEDED)


async def test_legacy_tasks_ignore_every_project_mechanism(db, tmp_path):
    h = Harness(db, tmp_path / "ws")
    legacy = await h.new_task()
    assert await h.start(legacy.id) == ["COMPLETED"]
    async with db.transaction() as s:
        assert (await s.execute(select(Project))).scalars().all() == []
