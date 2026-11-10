"""GET /projects/{id}/diagnostics and the extended GET /health, over HTTP against a real database."""
import httpx
import pytest

from app.config import Settings
from app.container import Container
from app.main import create_app
from app.providers.preflight import PreflightReport
from app.services.diagnostics_service import DiagnosticsService
from app.services.project_service import ProjectService
from app.services.runtime_status import StartupOutcome
from tests.api.test_api import assert_envelope
from tests.harness import Harness
from tests.unit.test_diagnostics_service import GOOD_PREFLIGHT, FakeExecutor

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"


def build(db, tmp_path, executor=None, with_diagnostics=True):
    h = Harness(db, tmp_path / "ws")
    projects = ProjectService(db, FakeExecutor(), clone_root=tmp_path / "clones", protected=(h.guard.root,))
    container = Container(settings=Settings(), db=db, guard=h.guard, executor=None, events=h.events, tasks=h.tasks,
                          gates=h.gates, runner=h.runner, providers={"CLAUDE": h.claude, "CODEX": h.codex},
                          projects=projects)
    container.preflight = GOOD_PREFLIGHT
    ex = executor or FakeExecutor()
    if with_diagnostics:
        container.diagnostics = DiagnosticsService(projects, ex, protected=projects.protected,
                                                   get_runtime=container.runtime, get_preflight=lambda: container.preflight)
    app = create_app(container=container, run_preflight=False)
    app.state.container = container
    return container, app, h, ex


async def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def env(db, tmp_path):
    container, app, h, ex = build(db, tmp_path)
    (tmp_path / "projects").mkdir()
    async with await client_for(app) as client:
        yield client, container, h, ex, tmp_path / "projects"


async def register(client, root, name="svc"):
    path = root / name
    path.mkdir()
    created = await client.post("/projects", json={"name": name, "workspace_path": str(path)})
    assert created.status_code == 201
    return created.json()["id"]


# --- diagnostics -----------------------------------------------------------------------------------------------------
async def test_diagnostics_report_items_with_codes_status_time_cause_and_the_blocking_flag(env):
    client, container, _h, ex, root = env
    project_id = await register(client, root)
    container.startup_outcome = StartupOutcome.PREFLIGHT_BLOCKED          # the Runner never started
    response = await client.get(f"/projects/{project_id}/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project_id and body["cached"] is False and "guarantee" in body["disclaimer"]
    assert body["can_start"] is False and body["blockers"] == ["RUNNER_STATE"]
    items = {i["code"]: i for i in body["items"]}
    assert items["RUNNER_STATE"]["status"] == "FAIL" and items["RUNNER_STATE"]["blocks_execution"] is True
    assert "PREFLIGHT_FAILED" in items["RUNNER_STATE"]["cause"] and items["RUNNER_STATE"]["recommendation"]
    assert items["CLI_AUTH"]["status"] == "UNKNOWN" and items["WORKSPACE_ACCESS"]["status"] == "PASS"
    for item in body["items"]:
        assert set(item) == {"code", "status", "checked_at", "cause", "recommendation", "blocks_execution"}
        assert item["status"] in {"PASS", "WARN", "FAIL", "UNKNOWN"}
    assert all(call[0] in ("git", "java") for call in ex.calls)            # nothing else was ever run


async def test_a_second_request_is_cached_and_refresh_recomputes(env):
    client, _c, _h, ex, root = env
    project_id = await register(client, root)
    first = (await client.get(f"/projects/{project_id}/diagnostics")).json()
    second = (await client.get(f"/projects/{project_id}/diagnostics")).json()
    refreshed = (await client.get(f"/projects/{project_id}/diagnostics", params={"refresh": "true"})).json()
    assert (first["cached"], second["cached"], refreshed["cached"]) == (False, True, False)
    assert len(ex.calls) == 2


async def test_an_unknown_project_is_a_404_and_a_bad_id_is_a_422(env):
    client, *_ = env
    assert_envelope(await client.get(f"/projects/{UNKNOWN_ID}/diagnostics"), 404, "NOT_FOUND")
    assert (await client.get("/projects/not-a-uuid/diagnostics")).status_code == 422


async def test_diagnostics_never_change_the_project(env):
    client, _c, _h, _ex, root = env
    project_id = await register(client, root)
    before = (await client.get(f"/projects/{project_id}")).json()
    await client.get(f"/projects/{project_id}/diagnostics")
    assert (await client.get(f"/projects/{project_id}")).json() == before


async def test_a_container_without_diagnostics_answers_with_the_error_envelope(db, tmp_path):
    _container, app, *_ = build(db, tmp_path, with_diagnostics=False)
    async with await client_for(app) as client:
        response = await client.get(f"/projects/{UNKNOWN_ID}/diagnostics")
    assert_envelope(response, 503, "SERVICE_NOT_CONFIGURED")


# --- /health ---------------------------------------------------------------------------------------------------------
async def test_health_keeps_its_original_keys_and_adds_readiness(env):
    client, container, *_ = env
    body = (await client.get("/health")).json()
    assert body["status"] == "ok" and body["runner_active"] is False       # the original contract
    assert body["ready"] is False
    assert body["runner"] == {"lifecycle": "STOPPED", "startup_outcome": "NOT_ATTEMPTED", "blockers": ["STOPPED"]}
    assert (await client.get("/health")).status_code == 200                # liveness: always 200 while the API answers


@pytest.mark.parametrize("outcome,blocker", [(StartupOutcome.PREFLIGHT_BLOCKED, "PREFLIGHT_FAILED"),
                                             (StartupOutcome.ALREADY_ACTIVE, "ADVISORY_LOCK_HELD")])
async def test_health_explains_why_the_runner_never_started(env, outcome, blocker):
    client, container, *_ = env
    container.startup_outcome = outcome
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["runner_active"] is False and body["ready"] is False and body["runner"]["blockers"] == [blocker]


async def test_runner_active_and_ready_follow_the_real_runner_lifecycle_including_after_a_stop(env):
    client, container, h, *_ = env
    await h.runner.start()
    try:
        container.startup_outcome = StartupOutcome.STARTED
        running = (await client.get("/health")).json()
        assert running["runner_active"] is True and running["ready"] is True and running["runner"]["blockers"] == []
        assert container.runner_active is True
    finally:
        await h.runner.stop()
    stopped = (await client.get("/health")).json()
    assert stopped["runner_active"] is False and stopped["ready"] is False        # no manual flag is left behind
    assert stopped["runner"]["lifecycle"] == "STOPPED" and stopped["runner"]["blockers"] == ["STOPPED"]
