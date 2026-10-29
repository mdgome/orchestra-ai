from pathlib import Path

from sqlalchemy import select

from app.models.tables import AgentSession
from app.providers.base import ProviderResult
from app.models.states import CliOutcome
from tests.harness import BAD_CODE, GOOD_CODE, Harness, developer_good, review_pass, structured


def dev_bad_then_good(fix_on_call):
    def dev(call):
        code = GOOD_CODE if call.n >= fix_on_call else BAD_CODE
        (Path(call.cwd) / "calc.py").write_text(code, encoding="utf-8")
        return structured(summary=f"iteration {call.n}", files_changed=["calc.py"], human_gate_request=None)
    return dev


async def test_failing_tests_feed_back_into_a_new_iteration_and_then_pass(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev_bad_then_good(2)})
    task = await h.new_task()
    assert await h.start(task.id) == ["COMPLETED"]
    t = await h.task(task.id)
    assert t.workflow_iteration == 1 and t.plan_revision == 0 and t.replan_count == 0
    path = await h.state_path(task.id)
    assert path == ["PENDING", "PLANNING", "DESIGN_REVIEWING", "DEVELOPING", "TESTING", "RETRYING", "DEVELOPING",
                    "TESTING", "REVIEWING", "COMPLETED"]
    devs = [c for c in h.claude.calls if c.role == "DEVELOPER"]
    assert len(devs) == 2 and "tests" in devs[1].prompt and "test_add" in devs[1].prompt   # real failure id reached the dev
    # the code reviewer ran exactly once: only on code that passed
    assert h.roles_called(h.codex).count("CODE_REVIEWER") == 1
    # the developer session is resumed across iterations (same plan)
    assert devs[1].resume_id == h.claude.sessions["DEVELOPER"]


async def test_improvement_loop_is_bounded_to_three_and_then_fails_cleanly(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev_bad_then_good(99)})
    task = await h.new_task()
    assert await h.start(task.id) == ["FAILED"]
    t = await h.task(task.id)
    assert t.failure_reason == "MAX_ITERATIONS_EXCEEDED" and t.workflow_iteration == 3
    assert len([c for c in h.claude.calls if c.role == "DEVELOPER"]) == 4        # initial + 3 improvements
    assert "CODE_REVIEWER" not in h.roles_called(h.codex)                         # failing tests never reach review
    assert [r.status for r in await h.runs(task.id)] == ["FINISHED"]


async def test_repeated_identical_failure_is_recorded(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev_bad_then_good(99)})
    task = await h.new_task()
    await h.start(task.id)
    tests = [s for s in await h.steps_of(task.id) if s.step_key == "TEST"]
    assert [s.output["repeated_failure"] for s in tests] == [False, True, True, True]


async def test_reviewer_fail_triggers_improvement_with_issues_as_feedback(db, tmp_path):
    def reviewer(call):
        if call.n == 1:
            return structured(verdict="FAIL", summary="x", plan_defect=False, test_policy_concern=False,
                              human_gate_request=None,
                              issues=[{"severity": "MAJOR", "description": "controller contains business logic"}])
        return review_pass(call)
    h = Harness(db, tmp_path, codex_handlers={"CODE_REVIEWER": reviewer})
    task = await h.new_task()
    assert await h.start(task.id) == ["COMPLETED"]
    devs = [c for c in h.claude.calls if c.role == "DEVELOPER"]
    assert len(devs) == 2 and "controller contains business logic" in devs[1].prompt
    assert (await h.task(task.id)).workflow_iteration == 1


async def test_plan_defect_from_review_replans_instead_of_patching(db, tmp_path):
    def reviewer(call):
        if call.n == 1:
            return structured(verdict="FAIL", summary="x", plan_defect=True, test_policy_concern=False,
                              human_gate_request=None, issues=[{"severity": "MAJOR", "description": "wrong approach"}])
        return review_pass(call)
    h = Harness(db, tmp_path, codex_handlers={"CODE_REVIEWER": reviewer})
    task = await h.new_task()
    assert await h.start(task.id) == ["COMPLETED"]
    t = await h.task(task.id)
    assert (t.plan_revision, t.replan_count, t.workflow_iteration) == (1, 1, 1)
    assert h.roles_called(h.claude).count("PLANNER") == 2 and h.roles_called(h.codex).count("DESIGN_REVIEWER") == 2
    # the new plan has a new hash basis, so a Developer session for the old plan is not resumed
    devs = [c for c in h.claude.calls if c.role == "DEVELOPER"]
    assert devs[1].resume_id in (None, h.claude.sessions["DEVELOPER"])


async def test_tester_changing_application_code_is_a_policy_violation(db, tmp_path):
    def rogue_tester(call):
        (Path(call.cwd) / "calc.py").write_text("def add(a, b):\n    return 5\n")
        return structured(summary="x", tests_added=[], human_gate_request=None)
    h = Harness(db, tmp_path, claude_handlers={"TESTER": rogue_tester})
    task = await h.new_task()
    assert await h.start(task.id) == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "TESTER_MODIFIED_NON_TEST_FILES"
    assert "CODE_REVIEWER" not in h.roles_called(h.codex)


async def test_removing_a_test_is_reported_to_the_reviewer_not_treated_as_failure(db, tmp_path):
    two_tests = (Path(__file__).parent / "unused")  # placeholder to keep the module import-light

    def dev(call):
        (Path(call.cwd) / "calc.py").write_text(GOOD_CODE, encoding="utf-8")
        tests = Path(call.cwd) / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_extra.py").write_text("def test_extra():\n    assert True\n", encoding="utf-8")
        return structured(summary="impl", files_changed=[], human_gate_request=None)

    def tester(call):
        tests = Path(call.cwd) / "tests"
        (tests / "test_extra.py").unlink()                 # quietly deletes a test...
        (tests / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n")
        return structured(summary="x", tests_added=[], human_gate_request=None)
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev, "TESTER": tester})
    task = await h.new_task()
    assert await h.start(task.id) == ["COMPLETED"]          # ...which is not a failure by itself
    review_prompt = next(c.prompt for c in h.codex.calls if c.role == "CODE_REVIEWER")
    assert "test_extra" in review_prompt and "removed_test_ids" in review_prompt   # but the reviewer is told


async def test_reviewer_concern_about_weakened_tests_blocks_completion(db, tmp_path):
    def reviewer(call):
        return structured(verdict="PASS", summary="x", plan_defect=False, test_policy_concern=True,
                          human_gate_request=None, issues=[])
    h = Harness(db, tmp_path, codex_handlers={"CODE_REVIEWER": reviewer})
    task = await h.new_task(limits={"max_workflow_iterations": 1})
    assert await h.start(task.id) == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "MAX_ITERATIONS_EXCEEDED"


async def test_technical_retry_is_counted_separately_from_iterations(db, tmp_path):
    def flaky_dev(call):
        if call.n == 1:
            return ProviderResult(outcome=CliOutcome.ERROR, exit_code=1)
        return developer_good(call)
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": flaky_dev})
    task = await h.new_task()
    assert await h.start(task.id) == ["COMPLETED"]
    devs = [s for s in await h.steps_of(task.id) if s.step_key == "DEVELOP"]
    assert [(s.step_attempt, s.status) for s in devs] == [(1, "FAILED"), (2, "SUCCEEDED")]
    assert (await h.task(task.id)).workflow_iteration == 0


async def test_step_attempts_are_exhausted_after_the_limit(db, tmp_path):
    def broken(call):
        return ProviderResult(outcome=CliOutcome.TIMEOUT, exit_code=None, termination="TERMINATED")
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": broken})
    task = await h.new_task(limits={"max_step_attempts": 2})
    assert await h.start(task.id) == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "STEP_ATTEMPTS_EXHAUSTED"
    assert len([c for c in h.claude.calls if c.role == "DEVELOPER"]) == 2


async def test_cli_invocation_limit_stops_the_task(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task(limits={"max_cli_invocations": 3})
    assert await h.start(task.id) == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "CLI_INVOCATIONS_EXCEEDED"
    assert len(h.claude.calls) + len(h.codex.calls) == 3


async def test_wall_time_limit_stops_the_task(db, tmp_path):
    h = Harness(db, tmp_path)
    task = await h.new_task()
    from sqlalchemy import update

    from app.models.tables import Task
    async with db.transaction() as s:
        await s.execute(update(Task).where(Task.id == task.id).values(run_seconds=10_000.0))
    assert await h.start(task.id) == ["FAILED"]
    assert (await h.task(task.id)).failure_reason == "WALL_TIME_EXCEEDED"
    assert h.claude.calls == [] and h.codex.calls == []


async def test_missing_native_session_is_replaced_and_audited_without_the_id(db, tmp_path):
    h = Harness(db, tmp_path, claude_handlers={"DEVELOPER": dev_bad_then_good(2)})
    task = await h.new_task()
    # after the first developer run the CLI "forgets" its session (e.g. history purged)
    original = h.claude.handlers["DEVELOPER"]

    def forgetful(call):
        if call.n == 2:
            h.claude.live_ids.discard(call.resume_id)
        return original(call)
    h.claude.handlers["DEVELOPER"] = forgetful
    assert await h.start(task.id) == ["COMPLETED"]
    devs = [c for c in h.claude.calls if c.role == "DEVELOPER"]
    # call 2 tried to resume (session gone) -> SESSION_MISSING -> a fresh session without extra step attempt
    assert devs[1].resume_id is not None and devs[2].resume_id is None
    steps = [s for s in await h.steps_of(task.id) if s.step_key == "DEVELOP"]
    assert [s.step_attempt for s in steps] == [1, 1]            # replacement did not use a technical retry
    actions = [t.event["action"] for t in await h.transitions(task.id) if t.kind == "SESSION_EVENT"
               and t.event["role"] == "DEVELOPER"]
    assert "INVALIDATED" in actions and "REPLACED" in actions or "CREATED" in actions
    blob = str([t.event for t in await h.transitions(task.id)])
    assert not any(i in blob for i in h.claude.live_ids)
