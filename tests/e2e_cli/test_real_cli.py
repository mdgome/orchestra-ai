"""Runs the REAL Codex and Claude CLIs (slow, costs tokens): `pytest -m e2e_cli`."""
import shutil

import pytest
from sqlalchemy import select

from app.config import Settings
from app.container import build_container
from app.models.tables import AgentSession, Execution
from app.providers.preflight import check_installed_cli, probe_write_blocked, scan_all_settings
from app.repositories.approval_repository import ExecutionRequestRepository

pytestmark = [
    pytest.mark.e2e_cli,
    pytest.mark.skipif(not (shutil.which("claude") and shutil.which("codex")), reason="CLIs not installed"),
]


def container_for(db, tmp_path):
    settings = Settings(workspace_root=tmp_path / "ws", claude_bin="claude", codex_bin="codex")
    return build_container(settings, db=db)


async def test_preflight_reports_supported_cli_versions_and_no_blocking_settings(db, tmp_path):
    c = container_for(db, tmp_path)
    report = await check_installed_cli(c.executor, c.guard.root)
    report.settings_findings = scan_all_settings(c.guard.root)
    print("versions:", report.versions, "findings:", report.settings_findings)
    assert report.missing_flags == {"claude": [], "codex": []}
    assert report.blocking == []


@pytest.mark.parametrize("role", ["PLANNER", "DESIGN_REVIEWER", "CODE_REVIEWER"])
async def test_read_only_roles_cannot_write_files_with_the_real_clis(db, tmp_path, role):
    c = container_for(db, tmp_path)
    provider = c.providers["CLAUDE" if role == "PLANNER" else "CODEX"]
    assert await probe_write_blocked(provider, role, c.guard.root / f"probe-{role}") is True


async def test_full_workflow_with_real_cli_plan_review_develop_test_review(db, tmp_path):
    c = container_for(db, tmp_path)
    task = await c.tasks.create_task(
        requirements=("Create calc.py with two pure functions: add(a, b) returning a + b and "
                      "subtract(a, b) returning a - b. No other files except tests."),
        limits={"max_cli_invocations": 20, "max_workflow_iterations": 2, "max_replans": 1})
    await c.tasks.request_execution(task.id)

    outcomes = []
    for _ in range(8):
        async with db.transaction() as s:
            request = await ExecutionRequestRepository(s).claim_next(c.runner.runner_id)
        if request is None:
            pending = [a for a in await c.tasks.approvals(task.id) if a.status == "PENDING" and a.type == "DESIGN"]
            if not pending:
                break
            # the "human": approve the offered option; the RESUME request is created in the same transaction
            opts = pending[0].request_payload["options"]
            print(" human approves", pending[0].request_payload.get("problem", "")[:200], "->", opts[0]["id"])
            await c.gates.decide(pending[0].id, decision="APPROVED", selected_option=opts[0]["id"],
                                 rationale="e2e auto-approval", idempotency_key=f"e2e-{pending[0].id}")
            continue
        outcomes.append(await c.runner.process(request))
    final = await c.tasks.get_task(task.id)
    transitions = [t.to_state for t in await c.tasks.transitions(task.id) if t.kind == "TASK_STATE"]
    print("outcomes:", outcomes, "final:", final.status, final.failure_reason, "path:", transitions)

    steps = []
    async with db.transaction() as s:
        from app.repositories.task_repository import TaskRepository
        steps = await TaskRepository(s).steps_for_task(task.id)
    for st in steps:
        print(" step", st.step_key, st.plan_revision, st.workflow_iteration, st.step_attempt, st.status, st.error)

    for a in await c.tasks.approvals(task.id):
        print(" approval", a.type, a.target, str(a.request_payload)[:600])
    assert final.status == "COMPLETED", (final.status, final.failure_reason, outcomes)
    workspace = c.guard.root / str(task.id)
    assert (workspace / "calc.py").exists()
    test_step = next(s for s in steps if s.step_key == "TEST" and s.status == "SUCCEEDED")
    assert test_step.output["test_result"]["passed"] and test_step.output["test_result"]["junit_ok"]
    assert next(s for s in steps if s.step_key == "DESIGN_REVIEW").output["verdict"] == "PASS"

    async with db.transaction() as s:
        sessions = list((await s.execute(select(AgentSession).where(AgentSession.task_id == task.id))).scalars())
        executions = list((await s.execute(select(Execution).where(Execution.task_id == task.id))).scalars())
    assert {x.agent_role for x in sessions} == {"PLANNER", "DESIGN_REVIEWER", "DEVELOPER", "TESTER", "CODE_REVIEWER"}
    ids = [x.native_session_id for x in sessions]
    assert len(set(ids)) == 5 and None not in ids                      # five independent native sessions
    blob = " ".join((e.stdout_tail or "") + (e.stderr_tail or "") + str(e.detail) for e in executions)
    assert not any(i in blob for i in ids)                              # no native id in stored logs
    assert all(e.finished_at is not None for e in executions)           # every CLI run has a recorded end
    await c.db.dispose() if False else None
