from pathlib import Path

from sqlalchemy import text

from tests.harness import GATE, PLAN, Harness, design_pass, planner_ok, structured


def revise_then_pass(feedback_text="split the module"):
    def reviewer(call):
        if call.n == 1:
            return structured(verdict="NEEDS_REVISION", summary="x", human_gate_request=None,
                              issues=[{"severity": "MAJOR", "description": feedback_text}])
        return design_pass(call)
    return reviewer


async def test_needs_revision_resumes_the_planner_session_and_forces_a_re_review(db, tmp_path):
    h = Harness(db, tmp_path, codex_handlers={"DESIGN_REVIEWER": revise_then_pass()})
    task = await h.new_task()
    assert await h.start(task.id) == ["COMPLETED"]

    planner_calls = [c for c in h.claude.calls if c.role == "PLANNER"]
    assert len(planner_calls) == 2
    assert planner_calls[0].resume_id is None
    assert planner_calls[1].resume_id == h.claude.sessions["PLANNER"]          # the Plan Session was RESUMED
    assert "split the module" in planner_calls[1].prompt                       # feedback was passed on
    reviews = [c for c in h.codex.calls if c.role == "DESIGN_REVIEWER"]
    assert len(reviews) == 2                                                    # the revised plan was re-reviewed
    t = await h.task(task.id)
    assert (t.plan_revision, t.replan_count, t.workflow_iteration) == (1, 1, 0)
    # development only ever started after the second review passed
    order = h.log
    first_dev = order.index("DEVELOPER")
    assert order[:first_dev].count("DESIGN_REVIEWER") == 2       # both reviews happened before any development


async def test_development_never_starts_before_design_review_pass(db, tmp_path):
    def always_revise(call):
        return structured(verdict="NEEDS_REVISION", summary="x", human_gate_request=None,
                          issues=[{"severity": "MAJOR", "description": "no"}])
    h = Harness(db, tmp_path, codex_handlers={"DESIGN_REVIEWER": always_revise})
    task = await h.new_task(limits={"max_replans": 1})
    assert await h.start(task.id) == ["FAILED"]
    t = await h.task(task.id)
    assert t.failure_reason == "MAX_REPLANS_EXCEEDED"
    assert "DEVELOPER" not in h.roles_called(h.claude)                        # blocked the whole time
    assert [r.status for r in await h.runs(task.id)] == ["FINISHED"]           # clean, normal termination


async def test_max_replans_zero_fails_on_first_revision_request(db, tmp_path):
    h = Harness(db, tmp_path, codex_handlers={"DESIGN_REVIEWER": revise_then_pass()})
    task = await h.new_task(limits={"max_replans": 0})
    assert await h.start(task.id) == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "MAX_REPLANS_EXCEEDED"


async def test_critical_issue_can_never_pass_design_review(db, tmp_path):
    def pass_with_critical(call):
        if call.n == 1:
            return structured(verdict="PASS", summary="x", human_gate_request=None,
                              issues=[{"severity": "CRITICAL", "description": "uses DDD aggregates"}])
        return design_pass(call)
    h = Harness(db, tmp_path, codex_handlers={"DESIGN_REVIEWER": pass_with_critical})
    task = await h.new_task()
    await h.start(task.id)
    steps = [s for s in await h.steps_of(task.id) if s.step_key == "DESIGN_REVIEW"]
    assert steps[0].output["verdict"] == "NEEDS_REVISION"                      # downgraded by the Orchestrator
    assert (await h.task(task.id)).plan_revision == 1


async def test_design_pass_is_invalidated_when_the_basis_changes_before_development(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    # run PLAN + DESIGN_REVIEW only by stopping the developer from ever finishing: make DEVELOPER raise a gate
    # request, then change the decisions (a new approved decision changes design_review_basis)
    dev_gate = {"summary": "need decision", "files_changed": [], "human_gate_request": GATE}
    h.claude.handlers["DEVELOPER"] = lambda call: structured(**dev_gate)
    assert await h.start(task.id) == ["WAITING_APPROVAL"]
    approval = (await h.approvals(task.id))[0]
    h.claude.handlers["DEVELOPER"] = __import__("tests.harness", fromlist=["developer_good"]).developer_good
    await h.decide(approval.id)
    assert await h.run_until_idle() == ["COMPLETED"]

    reviews = [c for c in h.codex.calls if c.role == "DESIGN_REVIEWER"]
    assert len(reviews) == 2          # the approved decision changed the basis -> Design Review had to run again
    path = await h.state_path(task.id)
    assert path.count("DESIGN_REVIEWING") == 2 and path[-1] == "COMPLETED"
    t = await h.task(task.id)
    assert t.decisions_hash != ""


async def test_planner_human_gate_parks_releases_lease_and_resumes_after_approval(db, tmp_path):
    gate_plan = dict(PLAN, human_gate_request=GATE)
    h = Harness(db, tmp_path, claude_handlers={
        "PLANNER": lambda call: structured(**(gate_plan if call.n == 1 else PLAN))})
    task = await h.new_task()
    assert await h.start(task.id) == ["WAITING_APPROVAL"]

    t = await h.task(task.id)
    assert t.status == "WAITING_APPROVAL" and t.resume_step_key == "PLAN"
    steps = await h.steps_of(task.id)
    assert [(s.step_key, s.status) for s in steps] == [("PLAN", "WAITING_APPROVAL")]      # waiting != failed
    assert [r.status for r in await h.runs(task.id)] == ["FINISHED"]                   # no lease / heartbeat while waiting
    approvals = await h.approvals(task.id)
    assert len(approvals) == 1 and approvals[0].type == "DESIGN" and approvals[0].status == "PENDING"
    assert h.codex.calls == []                                                          # nothing proceeds un-approved

    # waiting does not consume any counter
    assert (t.plan_revision, t.workflow_iteration, t.replan_count) == (0, 0, 0)

    out = await h.decide(approvals[0].id, option="A")
    assert out.created
    assert await h.run_until_idle() == ["COMPLETED"]
    planner_calls = [c for c in h.claude.calls if c.role == "PLANNER"]
    assert len(planner_calls) == 2 and "APPROVED: A" in planner_calls[1].prompt       # the decision reached the planner
    t = await h.task(task.id)
    assert t.plan_revision == 1 and t.replan_count == 0                                # a human decision is not a replan
    # the plan changed after the gate, so Codex reviewed it afterwards
    assert h.roles_called(h.codex)[0] == "DESIGN_REVIEWER"


async def test_unapproved_gate_blocks_further_execution(db, tmp_path):
    gate_plan = dict(PLAN, human_gate_request=GATE)
    h = Harness(db, tmp_path, claude_handlers={"PLANNER": lambda call: structured(**gate_plan)})
    task = await h.new_task()
    await h.start(task.id)
    # forcing another execution request must not run anything while the gate is open
    import pytest

    from app.validation.exceptions import Conflict
    with pytest.raises(Conflict):
        await h.tasks.request_execution(task.id)
    calls = len(h.claude.calls)
    assert await h.run_until_idle() == []
    assert len(h.claude.calls) == calls


async def test_planner_that_modifies_files_cannot_succeed(db, tmp_path):
    def sneaky_planner(call):
        (Path(call.cwd) / "hacked.py").write_text("x = 1\n")
        return planner_ok(call)
    h = Harness(db, tmp_path, claude_handlers={"PLANNER": sneaky_planner})
    task = await h.new_task()
    assert await h.start(task.id) == ["FAILED"]
    t = await h.task(task.id)
    assert t.failure_reason == "PLANNER_MODIFIED_FILES"
    steps = await h.steps_of(task.id)
    assert steps[0].status == "FAILED" and "DESIGN_REVIEW" not in [s.step_key for s in steps]
    assert h.codex.calls == []


async def test_invalid_plan_artifact_is_a_technical_retry_not_a_gate(db, tmp_path):
    def planner(call):
        if call.n == 1:
            return structured(**dict(PLAN, implementation_steps=[], changed_files=["../etc/passwd"]))
        return planner_ok(call)
    h = Harness(db, tmp_path, claude_handlers={"PLANNER": planner})
    task = await h.new_task()
    assert await h.start(task.id) == ["COMPLETED"]
    plans = [s for s in await h.steps_of(task.id) if s.step_key == "PLAN"]
    assert [(s.step_attempt, s.status) for s in plans] == [(1, "FAILED"), (2, "SUCCEEDED")]
    t = await h.task(task.id)
    assert t.plan_revision == 0 and t.workflow_iteration == 0       # attempts are not iterations or replans
