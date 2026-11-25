"""POST /approvals/{id}/decisions for a PROJECT_DIRTY gate whose targets include a pid-less row: 409 OPTION_NOT_AVAILABLE."""
import httpx
from sqlalchemy import select

from app.config import Settings
from app.container import Container
from app.main import create_app
from app.models.tables import ExecutionRequest
from tests.harness import Harness
from tests.integration_pg.test_orphan_sweeper import add_row
from tests.integration_pg.test_pidless_unproven_resolution import UNPROVEN
from tests.integration_pg.test_process_schema import make_project_task
from tests.integration_pg.test_risk_options import gates_for, open_gate


async def test_a_direct_accept_for_a_pid_less_target_is_a_409_option_not_available_with_the_reason_and_nothing_written(db, tmp_path):
    h = Harness(db, tmp_path)
    project, task = await make_project_task(db)
    await add_row(db, task, detail=dict(UNPROVEN))
    gates = gates_for(db)
    approval = await open_gate(db, gates, project, task)
    container = Container(settings=Settings(), db=db, guard=h.guard, executor=None, events=h.events, tasks=h.tasks, gates=gates,
                          runner=h.runner, providers={"CLAUDE": h.claude, "CODEX": h.codex})
    app = create_app(container=container, run_preflight=False)
    app.state.container = container
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/approvals/{approval}/decisions", headers={"Idempotency-Key": "k-1"},
                                 json={"decision": "APPROVED", "selected_option": "accept_unproven_risk",
                                       "rationale": "the process is gone, I checked it"})
        assert resp.status_code == 409, resp.text
        error = resp.json()["error"]
        assert error["code"] == "OPTION_NOT_AVAILABLE" and error["details"] == {"reason_code": "TARGET_WITHOUT_PID", "retryable": False}
        assert (await client.get(f"/approvals/{approval}")).json()["status"] == "PENDING"
    async with db.transaction() as s:
        assert (await s.execute(select(ExecutionRequest).where(ExecutionRequest.task_id == task.id))).first() is None
